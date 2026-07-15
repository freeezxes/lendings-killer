import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, desc, func, insert
from core.database import AsyncSessionLocal
from repositories.user_repo import user_repo
from repositories.site_repo import site_repo
from models.site import Site
from models.user import User
from models.payment import Payment, SupportInvoice
from models.promotion import PromotionSetup, PromotionCampaign
from models.log import DevCreditLog, PromoCreditLog, OnboardingSession, Notification
from models.analytics import AnalyticsEvent
from models.site_version import SiteVersion

from domain import (
    AnalyticsStatus,
    CampaignStatus,
    CAMPAIGN_MIN_CREDITS,
    CAMPAIGN_MIN_DURATION_HOURS,
    InvoiceStatus,
    PROMO_CREDIT_TENGE,
    PROMO_MIN_PURCHASE,
    PROMO_SETUP_COST,
    PromotionStatus,
    SUPPORT_GRACE_DAYS,
    SUPPORT_INCLUDED_DAYS,
    SUPPORT_MONTHLY_PRICE,
    SUPPORT_WARNING_DAYS,
    SupportStatus,
    VERSION_RESTORE_DEV_CREDITS,
)

def _now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)

def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None

def _site_data(site: Site | dict) -> dict:
    data = site.data if hasattr(site, 'data') else site.get("data")
    if isinstance(data, str):
        try:
            return json.loads(data or "{}")
        except json.JSONDecodeError:
            return {}
    return data or {}

def is_support_operational(status: str | None) -> bool:
    return status in {SupportStatus.ACTIVE.value, SupportStatus.EXPIRING_SOON.value}

def is_support_public(status: str | None) -> bool:
    return status in {
        SupportStatus.ACTIVE.value,
        SupportStatus.EXPIRING_SOON.value,
        SupportStatus.INVOICE_ISSUED.value,
    }

def _status_label(status: str) -> str:
    labels = {
        SupportStatus.ACTIVE.value: "Поддержка активна",
        SupportStatus.EXPIRING_SOON.value: "Скоро закончится",
        SupportStatus.INVOICE_ISSUED.value: "Ожидает оплаты",
        SupportStatus.SUSPENDED.value: "Сайт приостановлен",
        PromotionStatus.NOT_CONFIGURED.value: "Продвижение не настроено",
        PromotionStatus.CONFIGURED.value: "Готово к запуску",
        PromotionStatus.ACTIVE.value: "Продвижение идет",
        PromotionStatus.PAUSED.value: "Продвижение на паузе",
        PromotionStatus.STOPPED.value: "Продвижение остановлено",
        AnalyticsStatus.UNAVAILABLE.value: "Недоступна",
        AnalyticsStatus.ACTIVE.value: "Активна",
        AnalyticsStatus.OUTDATED.value: "Нужно обновить",
        AnalyticsStatus.BLOCKED.value: "Заблокирована",
        CampaignStatus.ACTIVE.value: "Активна",
        CampaignStatus.COMPLETED.value: "Завершена",
        CampaignStatus.PAUSED.value: "На паузе",
        CampaignStatus.STOPPED_SUPPORT_EXPIRED.value: "Остановлена: поддержка",
        CampaignStatus.STOPPED_SITE_CHANGED.value: "Остановлена: сайт изменен",
        CampaignStatus.FAILED.value: "Ошибка",
    }
    return labels.get(status, status)

class SupportService:
    @staticmethod
    def initial_paid_until() -> str:
        return _fmt(_now() + timedelta(days=SUPPORT_INCLUDED_DAYS))

    @staticmethod
    def compute_status(site: Site | dict, now: datetime | None = None) -> str:
        now = now or _now()
        paid_until_str = site.support_paid_until if hasattr(site, 'support_paid_until') else site.get("support_paid_until")
        paid_until = _parse_dt(paid_until_str)
        if not paid_until:
            return SupportStatus.SUSPENDED.value
        if now <= paid_until:
            if paid_until - now <= timedelta(days=SUPPORT_WARNING_DAYS):
                return SupportStatus.EXPIRING_SOON.value
            return SupportStatus.ACTIVE.value
        if now <= paid_until + timedelta(days=SUPPORT_GRACE_DAYS):
            return SupportStatus.INVOICE_ISSUED.value
        return SupportStatus.SUSPENDED.value

    @classmethod
    async def refresh_site(cls, site_id: int) -> Site | None:
        now = _now()
        async with AsyncSessionLocal() as session:
            site = await site_repo.get(session, site_id)
            if not site:
                return None

            status = cls.compute_status(site, now)
            paid_until = _parse_dt(site.support_paid_until)
            if status in {SupportStatus.INVOICE_ISSUED.value, SupportStatus.SUSPENDED.value}:
                await cls._ensure_invoice(session, site, paid_until or now)
                site.analytics_status = AnalyticsStatus.BLOCKED.value
                site.promo_status = PromotionStatus.PAUSED.value
                site.support_status = status
                site.updated = now
                session.add(site)
                
                await CampaignService._stop_active_for_site(
                    session,
                    site_id,
                    CampaignStatus.STOPPED_SUPPORT_EXPIRED.value,
                )
            else:
                site.support_status = status
                site.updated = now
                session.add(site)

            await session.commit()
            await session.refresh(site)
            return site

    @classmethod
    async def _ensure_invoice(cls, session, site: Site, expired_at: datetime):
        result = await session.execute(
            select(SupportInvoice).filter_by(site_id=site.id, status=InvoiceStatus.PENDING.value)
            .order_by(desc(SupportInvoice.created)).limit(1)
        )
        existing = result.scalars().first()
        if existing:
            return
        due_at = expired_at + timedelta(days=SUPPORT_GRACE_DAYS)
        invoice = SupportInvoice(
            user_id=site.user_id,
            site_id=site.id,
            amount=SUPPORT_MONTHLY_PRICE,
            months=1,
            status=InvoiceStatus.PENDING.value,
            due_at=_fmt(due_at),
            created=datetime.utcnow(),
            updated=datetime.utcnow(),
        )
        session.add(invoice)

    @classmethod
    async def refresh_user_sites(cls, user_id: int) -> list[Site]:
        async with AsyncSessionLocal() as session:
            sites = await site_repo.get_multi_by_user(session, user_id)
            refreshed = []
            for site in sites:
                updated = await cls.refresh_site(site.id) or site
                await CampaignService.refresh_site_campaigns(updated.id)
                refreshed.append(await site_repo.get(session, updated.id) or updated)
            return refreshed

    @classmethod
    async def get_open_invoice(cls, site_id: int) -> SupportInvoice | None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(SupportInvoice).filter_by(site_id=site_id, status=InvoiceStatus.PENDING.value)
                .order_by(desc(SupportInvoice.created)).limit(1)
            )
            return result.scalars().first()

    @classmethod
    async def pay_invoice(cls, user_id: int, site_id: int) -> dict:
        now = _now()
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Site).filter_by(id=site_id, user_id=user_id))
            site = result.scalars().first()
            if not site:
                return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}

            status = cls.compute_status(site, now)
            if status == SupportStatus.ACTIVE.value:
                return {"ok": False, "error": "support_active", "message": "Поддержка уже активна."}

            paid_until = _parse_dt(site.support_paid_until) or now
            
            inv_res = await session.execute(
                select(SupportInvoice).filter_by(site_id=site_id, status=InvoiceStatus.PENDING.value)
                .order_by(desc(SupportInvoice.created)).limit(1)
            )
            invoice = inv_res.scalars().first()
            if not invoice:
                await cls._ensure_invoice(session, site, paid_until)
                await session.flush()
                inv_res = await session.execute(
                    select(SupportInvoice).filter_by(site_id=site_id, status=InvoiceStatus.PENDING.value)
                    .order_by(desc(SupportInvoice.created)).limit(1)
                )
                invoice = inv_res.scalars().first()

            if not invoice:
                return {"ok": False, "error": "invoice_unavailable", "message": "Счёт не найден."}

            extend_from = max(now, paid_until)
            new_paid_until = extend_from + timedelta(days=SUPPORT_INCLUDED_DAYS)
            order_id = f"support-{uuid.uuid4().hex[:12]}"
            
            invoice.status = InvoiceStatus.PAID.value
            invoice.paid_at = _fmt(now)
            invoice.order_id = order_id
            invoice.updated = datetime.utcnow()
            
            payment = Payment(
                user_id=user_id,
                order_id=order_id,
                invoice_id="",
                amount=invoice.amount,
                tokens=0,
                status="paid",
                payment_kind="support_invoice",
                site_id=site_id,
                support_invoice_id=invoice.id,
                created=datetime.utcnow(),
                updated=datetime.utcnow()
            )
            session.add(payment)

            analytics_status = site.analytics_status or AnalyticsStatus.UNAVAILABLE.value
            if analytics_status == AnalyticsStatus.BLOCKED.value and site.promo_setup_done:
                analytics_status = AnalyticsStatus.ACTIVE.value
            promo_status = site.promo_status or PromotionStatus.NOT_CONFIGURED.value
            if promo_status == PromotionStatus.PAUSED.value and site.promo_setup_done:
                promo_status = PromotionStatus.CONFIGURED.value
                
            site.support_paid_until = _fmt(new_paid_until)
            site.support_status = SupportStatus.ACTIVE.value
            site.analytics_status = analytics_status
            site.promo_status = promo_status
            site.updated = datetime.utcnow()
            
            await session.commit()
            return {
                "ok": True,
                "support_paid_until": _fmt(new_paid_until),
                "amount": SUPPORT_MONTHLY_PRICE,
            }

    @classmethod
    async def mark_invoice_paid(cls, payment_data: dict) -> dict:
        invoice_id = payment_data.get("support_invoice_id")
        site_id = payment_data.get("site_id")
        user_id = payment_data.get("user_id")
        now = _now()
        if not invoice_id or not site_id or not user_id:
            return {"ok": False, "error": "invalid_support_payment"}
            
        async with AsyncSessionLocal() as session:
            inv_res = await session.execute(select(SupportInvoice).filter_by(id=invoice_id, user_id=user_id, site_id=site_id))
            invoice = inv_res.scalars().first()
            site_res = await session.execute(select(Site).filter_by(id=site_id, user_id=user_id))
            site = site_res.scalars().first()
            
            if not invoice or not site:
                return {"ok": False, "error": "support_invoice_not_found"}
            if invoice.status == InvoiceStatus.PAID.value:
                return {"ok": True, "already_paid": True}

            paid_until = _parse_dt(site.support_paid_until) or now
            extend_from = max(now, paid_until)
            new_paid_until = extend_from + timedelta(days=SUPPORT_INCLUDED_DAYS)
            
            analytics_status = site.analytics_status or AnalyticsStatus.UNAVAILABLE.value
            if analytics_status == AnalyticsStatus.BLOCKED.value and site.promo_setup_done:
                analytics_status = AnalyticsStatus.ACTIVE.value
            promo_status = site.promo_status or PromotionStatus.NOT_CONFIGURED.value
            if promo_status == PromotionStatus.PAUSED.value and site.promo_setup_done:
                promo_status = PromotionStatus.CONFIGURED.value
                
            invoice.status = InvoiceStatus.PAID.value
            invoice.paid_at = _fmt(now)
            invoice.order_id = payment_data.get("order_id")
            invoice.updated = datetime.utcnow()
            
            site.support_paid_until = _fmt(new_paid_until)
            site.support_status = SupportStatus.ACTIVE.value
            site.analytics_status = analytics_status
            site.promo_status = promo_status
            site.updated = datetime.utcnow()
            
            await session.commit()
            return {"ok": True, "support_paid_until": _fmt(new_paid_until)}


class CreditsService:
    @classmethod
    async def purchase_promo_credits(cls, user_id: int, credits: int) -> dict:
        try:
            credits = int(credits)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_amount", "message": "Введите количество кредитов."}
        if credits < PROMO_MIN_PURCHASE:
            return {
                "ok": False,
                "error": "min_amount",
                "message": f"Минимальное пополнение - {PROMO_MIN_PURCHASE} кредитов продвижения.",
            }

        amount = credits * PROMO_CREDIT_TENGE
        order_id = f"promo-{uuid.uuid4().hex[:12]}"
        
        async with AsyncSessionLocal() as session:
            payment = Payment(
                user_id=user_id,
                order_id=order_id,
                invoice_id="",
                amount=amount,
                tokens=0,
                status="paid",
                payment_kind="promo_credits",
                promo_credits=credits,
                dev_credits=0,
                created=datetime.utcnow(),
                updated=datetime.utcnow()
            )
            session.add(payment)
            
            user = await user_repo.get(session, user_id)
            user.promo_credits += credits
            session.add(user)
            await session.flush()
            
            log = PromoCreditLog(
                user_id=user_id,
                delta=credits,
                reason=f"promo_credit_purchase:{order_id}",
                balance_after=user.promo_credits,
                created=datetime.utcnow()
            )
            session.add(log)
            balance = user.promo_credits
            await session.commit()
            
        return {"ok": True, "credits": credits, "amount": amount, "balance": balance, "order_id": order_id}

    @classmethod
    async def apply_promo_payment(cls, payment_data: dict) -> dict:
        credits = int(payment_data.get("promo_credits") or 0)
        user_id = int(payment_data.get("user_id") or 0)
        if credits <= 0 or user_id <= 0:
            return {"ok": False, "error": "invalid_promo_payment"}
            
        async with AsyncSessionLocal() as session:
            user = await user_repo.get(session, user_id)
            user.promo_credits += credits
            session.add(user)
            await session.flush()
            
            log = PromoCreditLog(
                user_id=user_id,
                delta=credits,
                reason=f"promo_credit_purchase:{payment_data.get('order_id')}",
                balance_after=user.promo_credits,
                created=datetime.utcnow()
            )
            session.add(log)
            balance = user.promo_credits
            await session.commit()
            
        return {"ok": True, "credits": credits, "balance": balance}

    @classmethod
    async def logs(cls, user_id: int, limit: int = 50) -> dict:
        async with AsyncSessionLocal() as session:
            dev_logs = await session.execute(
                select(DevCreditLog).filter_by(user_id=user_id).order_by(desc(DevCreditLog.created)).limit(limit)
            )
            promo_logs = await session.execute(
                select(PromoCreditLog).filter_by(user_id=user_id).order_by(desc(PromoCreditLog.created)).limit(limit)
            )
            
            return {
                "dev": [
                    {
                        "id": l.id, "user_id": l.user_id, "site_id": l.site_id, "delta": l.delta,
                        "reason": l.reason, "balance_after": l.balance_after, "created": _fmt(l.created)
                    } for l in dev_logs.scalars().all()
                ],
                "promo": [
                    {
                        "id": l.id, "user_id": l.user_id, "site_id": l.site_id, "delta": l.delta,
                        "reason": l.reason, "balance_after": l.balance_after, "created": _fmt(l.created)
                    } for l in promo_logs.scalars().all()
                ],
            }


class ForecastService:
    @staticmethod
    def build(site: Site | dict, credits: int, duration_hours: int) -> dict:
        credits = int(credits)
        duration_hours = int(duration_hours)
        if credits < CAMPAIGN_MIN_CREDITS:
            raise ValueError(f"Минимум {CAMPAIGN_MIN_CREDITS} кредитов.")
        if duration_hours < CAMPAIGN_MIN_DURATION_HOURS:
            raise ValueError(f"Минимум {CAMPAIGN_MIN_DURATION_HOURS} часа.")

        data = _site_data(site)
        title = site.title if hasattr(site, 'title') else site.get("title")
        niche_text = " ".join([
            str(title or ""),
            str(data.get("name") or ""),
            str(data.get("services") or ""),
        ]).lower()
        
        factor = 1.0
        if re.search(r"маникюр|бров|ресниц|макияж|beauty|salon", niche_text):
            factor = 1.22
        elif re.search(r"барбер|стриж|hair|волос", niche_text):
            factor = 1.16
        elif re.search(r"репетитор|курс|обуч|english|математ", niche_text):
            factor = 0.94
        elif re.search(r"массаж|spa|фитнес|тренер", niche_text):
            factor = 1.08

        pacing = min(1.2, max(0.75, duration_hours / 24))
        estimated_views = int(max(credits * 9, credits * 12 * factor / pacing))
        estimated_clicks = int(max(5, estimated_views * 0.055 * factor))
        estimated_contact_clicks = int(max(1, estimated_clicks * 0.22))
        return {
            "credits": credits,
            "duration_hours": duration_hours,
            "amount_kzt": credits * PROMO_CREDIT_TENGE,
            "estimated_views": estimated_views,
            "estimated_clicks": estimated_clicks,
            "estimated_contact_clicks": estimated_contact_clicks,
            "disclaimer": "Forecast only. Results are not guaranteed.",
        }


class PromotionService:
    @classmethod
    async def setup(cls, user_id: int, site_id: int) -> dict:
        site = await SupportService.refresh_site(site_id)
        if not site or site.user_id != user_id:
            return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}
        if not is_support_operational(site.support_status):
            return {"ok": False, "error": "support_inactive", "message": "Сначала оплатите поддержку сайта."}
        if site.promo_setup_done:
            return {"ok": False, "error": "already_configured", "message": "Продвижение уже настроено."}

        async with AsyncSessionLocal() as session:
            user = await user_repo.get(session, user_id)
            if user.promo_credits < PROMO_SETUP_COST:
                return {
                    "ok": False,
                    "error": "insufficient_promo_credits",
                    "message": f"Нужно {PROMO_SETUP_COST} кредитов продвижения.",
                }
            user.promo_credits -= PROMO_SETUP_COST
            session.add(user)
            await session.flush()
            balance = user.promo_credits
            
            log = PromoCreditLog(
                user_id=user_id,
                site_id=site_id,
                delta=-PROMO_SETUP_COST,
                reason="promotion_setup",
                balance_after=balance,
                created=datetime.utcnow()
            )
            session.add(log)
            
            setup_record = PromotionSetup(
                user_id=user_id,
                site_id=site_id,
                credits_spent=PROMO_SETUP_COST,
                status="completed",
                created=datetime.utcnow(),
                updated=datetime.utcnow()
            )
            session.add(setup_record)
            
            site = await site_repo.get(session, site_id)
            site.promo_setup_done = 1
            site.promo_status = PromotionStatus.CONFIGURED.value
            site.analytics_status = AnalyticsStatus.ACTIVE.value
            site.updated = datetime.utcnow()
            session.add(site)
            
            await session.commit()
            return {"ok": True, "promo_credits": balance}

    @staticmethod
    def validate_business_change(site: Site | dict, edit_summary: str) -> dict:
        text = (edit_summary or "").lower()
        data = _site_data(site)
        title = site.title if hasattr(site, 'title') else site.get("title")
        current = " ".join([
            str(title or ""),
            str(data.get("name") or ""),
            str(data.get("services") or ""),
        ]).lower()
        
        blocked = [
            "другой бизнес",
            "новый бизнес",
            "сменить нишу",
            "переделай под",
            "теперь это",
            "сделай сайт для другого",
            "замени бизнес",
            "другая ниша",
            "другое направление",
        ]
        if any(phrase in text for phrase in blocked):
            return {
                "ok": False,
                "message": "Один сайт привязан к одному направлению бизнеса. Для нового направления создайте отдельный сайт.",
            }
        niches = {
            "beauty": ["маникюр", "ногт", "бров", "ресниц", "косметолог", "макияж"],
            "barber": ["барбер", "стриж", "волос", "бород"],
            "food": ["кафе", "кофе", "ресторан", "еда", "доставка", "пицц", "суши"],
            "auto": ["авто", "машин", "аренда авто", "такси"],
            "education": ["репетитор", "курс", "обуч", "математ", "англий"],
            "massage": ["массаж", "spa", "спа"],
            "realty": ["недвиж", "аренда квартир", "риэлтор"],
        }
        current_hits = {name for name, words in niches.items() if any(w in current for w in words)}
        requested_hits = {name for name, words in niches.items() if any(w in text for w in words)}
        if current_hits and requested_hits and current_hits.isdisjoint(requested_hits):
            return {
                "ok": False,
                "message": "Похоже, запрос меняет направление бизнеса. Для новой ниши нужно создать отдельный сайт.",
            }
        prohibited = [
            "казино", "ставки", "букмекер", "adult", "18+", "порно", "наркот",
            "пирамид", "инвест гарант", "обнал", "поддель", "политическ",
        ]
        if any(word in text for word in prohibited):
            return {
                "ok": False,
                "message": "Мы не создаём и не продвигаем сайты для запрещённых или рискованных тематик.",
            }
        return {"ok": True}

class CampaignService:
    @classmethod
    async def _stop_active_for_site(cls, session, site_id: int, status: str):
        result = await session.execute(
            select(PromotionCampaign)
            .filter_by(site_id=site_id, status=CampaignStatus.ACTIVE.value)
        )
        for camp in result.scalars().all():
            camp.status = status
            camp.stopped_reason = status
            camp.updated = datetime.utcnow()
            session.add(camp)

    @classmethod
    async def refresh_site_campaigns(cls, site_id: int):
        now = _now()
        async with AsyncSessionLocal() as session:
            site = await site_repo.get(session, site_id)
            if not site:
                return
            if not is_support_operational(site.support_status):
                await cls._stop_active_for_site(
                    session,
                    site_id,
                    CampaignStatus.STOPPED_SUPPORT_EXPIRED.value,
                )
                await session.commit()
                return
                
            active_camps = await session.execute(
                select(PromotionCampaign)
                .filter_by(site_id=site_id, status=CampaignStatus.ACTIVE.value)
            )
            active_camps = active_camps.scalars().all()
            for camp in active_camps:
                if camp.ends_at and camp.ends_at <= now:
                    camp.status = CampaignStatus.COMPLETED.value
                    camp.updated = datetime.utcnow()
                    session.add(camp)
            
            await session.flush()
            
            active = await session.execute(
                select(PromotionCampaign)
                .filter_by(site_id=site_id, status=CampaignStatus.ACTIVE.value).limit(1)
            )
            has_active = active.scalars().first()
            
            next_status = PromotionStatus.ACTIVE.value if has_active else (
                PromotionStatus.CONFIGURED.value if site.promo_setup_done else PromotionStatus.NOT_CONFIGURED.value
            )
            site.promo_status = next_status
            site.updated = datetime.utcnow()
            session.add(site)
            await session.commit()

    @classmethod
    async def forecast(cls, user_id: int, site_id: int, credits: int, duration_hours: int) -> dict:
        site = await SupportService.refresh_site(site_id)
        if not site or site.user_id != user_id:
            return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}
        if not is_support_operational(site.support_status):
            return {"ok": False, "error": "support_inactive", "message": "Сначала оплатите поддержку сайта."}
        if not site.promo_setup_done:
            return {"ok": False, "error": "promo_not_configured", "message": "Сначала настройте продвижение."}
        if site.analytics_status != AnalyticsStatus.ACTIVE.value:
            return {"ok": False, "error": "analytics_outdated", "message": "Сначала восстановите аналитику."}
        try:
            return {"ok": True, "forecast": ForecastService.build(site, credits, duration_hours)}
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": "invalid_campaign", "message": str(exc)}

    @classmethod
    async def launch(cls, user_id: int, site_id: int, credits: int, duration_hours: int) -> dict:
        site = await SupportService.refresh_site(site_id)
        if not site or site.user_id != user_id:
            return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}
        if not is_support_operational(site.support_status):
            return {"ok": False, "error": "support_inactive", "message": "Сначала оплатите поддержку сайта."}
        if not site.promo_setup_done:
            return {"ok": False, "error": "promo_not_configured", "message": "Сначала настройте продвижение."}
        if site.analytics_status != AnalyticsStatus.ACTIVE.value:
            return {"ok": False, "error": "analytics_outdated", "message": "Сначала восстановите аналитику."}
        try:
            forecast = ForecastService.build(site, credits, duration_hours)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": "invalid_campaign", "message": str(exc)}

        starts_at = _now()
        ends_at = starts_at + timedelta(hours=int(duration_hours))
        
        async with AsyncSessionLocal() as session:
            active = await session.execute(
                select(PromotionCampaign)
                .filter_by(site_id=site_id, status=CampaignStatus.ACTIVE.value).limit(1)
            )
            if active.scalars().first():
                return {"ok": False, "error": "active_campaign_exists", "message": "Кампания уже запущена."}
                
            user = await user_repo.get(session, user_id)
            if user.promo_credits < int(credits):
                return {
                    "ok": False,
                    "error": "insufficient_promo_credits",
                    "message": "Недостаточно кредитов продвижения.",
                }
            user.promo_credits -= int(credits)
            session.add(user)
            await session.flush()
            balance = user.promo_credits
            
            campaign = PromotionCampaign(
                user_id=user_id,
                site_id=site_id,
                credits_spent=int(credits),
                duration_hours=int(duration_hours),
                status=CampaignStatus.ACTIVE.value,
                forecast_json=json.dumps(forecast, ensure_ascii=False),
                starts_at=starts_at,
                ends_at=ends_at,
                created=datetime.utcnow(),
                updated=datetime.utcnow()
            )
            session.add(campaign)
            await session.flush()
            campaign_id = campaign.id
            
            log = PromoCreditLog(
                user_id=user_id,
                site_id=site_id,
                campaign_id=campaign_id,
                delta=-int(credits),
                reason=f"campaign_launch:{campaign_id}",
                balance_after=balance,
                created=datetime.utcnow()
            )
            session.add(log)
            
            site = await site_repo.get(session, site_id)
            site.promo_status = PromotionStatus.ACTIVE.value
            site.updated = datetime.utcnow()
            session.add(site)
            
            await session.commit()
            
        return {"ok": True, "campaign_id": campaign_id, "promo_credits": balance, "forecast": forecast}

    @classmethod
    async def site_changed(cls, site_id: int, reason: str):
        async with AsyncSessionLocal() as session:
            site = await site_repo.get(session, site_id)
            if not site:
                return
                
            analytics_status = (
                AnalyticsStatus.OUTDATED.value
                if site.promo_setup_done
                else AnalyticsStatus.UNAVAILABLE.value
            )
            promo_status = (
                PromotionStatus.PAUSED.value
                if site.promo_setup_done
                else PromotionStatus.NOT_CONFIGURED.value
            )
            await cls._stop_active_for_site(session, site_id, CampaignStatus.STOPPED_SITE_CHANGED.value)
            
            site.analytics_status = analytics_status
            site.promo_status = promo_status
            site.updated = datetime.utcnow()
            session.add(site)
            
            event = AnalyticsEvent(
                site_id=site_id,
                event_type="site_changed",
                payload_json=json.dumps({"reason": reason}, ensure_ascii=False),
                created=datetime.utcnow()
            )
            session.add(event)
            await session.commit()

    @classmethod
    async def history(cls, site_id: int) -> list[dict]:
        await cls.refresh_site_campaigns(site_id)
        async with AsyncSessionLocal() as session:
            rows = await session.execute(
                select(PromotionCampaign).filter_by(site_id=site_id).order_by(desc(PromotionCampaign.created)).limit(20)
            )
            return [cls.present_campaign(r) for r in rows.scalars().all()]

    @staticmethod
    def present_campaign(campaign: PromotionCampaign | dict) -> dict:
        is_orm = hasattr(campaign, 'starts_at')
        starts_at = campaign.starts_at if is_orm else _parse_dt(campaign.get("starts_at"))
        ends_at = campaign.ends_at if is_orm else _parse_dt(campaign.get("ends_at"))
        status = campaign.status if is_orm else campaign.get("status")
        forecast_json = campaign.forecast_json if is_orm else campaign.get("forecast_json")
        now = _now()
        progress = 0
        if starts_at and ends_at and ends_at > starts_at:
            progress = int(min(100, max(0, (now - starts_at).total_seconds() / (ends_at - starts_at).total_seconds() * 100)))
        try:
            forecast = json.loads(forecast_json or "{}")
        except json.JSONDecodeError:
            forecast = {}
            
        c_dict = {
            "id": campaign.id if is_orm else campaign.get("id"),
            "status": status,
            "starts_at": _fmt(starts_at) if starts_at else None,
            "ends_at": _fmt(ends_at) if ends_at else None,
            "duration_hours": campaign.duration_hours if is_orm else campaign.get("duration_hours"),
            "credits_spent": campaign.credits_spent if is_orm else campaign.get("credits_spent"),
            "stopped_reason": campaign.stopped_reason if is_orm else campaign.get("stopped_reason"),
            "created": _fmt(campaign.created) if (is_orm and getattr(campaign, 'created', None)) else (campaign.get("created") if not is_orm else None),
        }
            
        c_dict["forecast"] = forecast
        c_dict["progress"] = 100 if status == CampaignStatus.COMPLETED.value else progress
        c_dict["status_label"] = _status_label(status or "")
        return c_dict


class AnalyticsService:
    @classmethod
    async def restore(cls, user_id: int, site_id: int) -> dict:
        site = await SupportService.refresh_site(site_id)
        if not site or site.user_id != user_id:
            return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}
        if not is_support_operational(site.support_status):
            return {"ok": False, "error": "support_inactive", "message": "Сначала оплатите поддержку сайта."}
        if not site.promo_setup_done:
            return {"ok": False, "error": "promo_not_configured", "message": "Сначала настройте продвижение."}
            
        async with AsyncSessionLocal() as session:
            user = await user_repo.get(session, user_id)
            if user.dev_credits < VERSION_RESTORE_DEV_CREDITS:
                return {"ok": False, "error": "insufficient_dev_credits", "message": "Недостаточно кредитов разработки."}
                
            user.dev_credits -= VERSION_RESTORE_DEV_CREDITS
            user.tokens = max(user.tokens - VERSION_RESTORE_DEV_CREDITS, 0)
            session.add(user)
            await session.flush()
            balance = user.dev_credits
            
            log = DevCreditLog(
                user_id=user_id,
                site_id=site_id,
                delta=-VERSION_RESTORE_DEV_CREDITS,
                reason="analytics_restore",
                balance_after=balance,
                created=datetime.utcnow()
            )
            session.add(log)
            
            # Note: The original code inserts into token_log but we don't have its ORM explicitly,
            # we'll use raw execute for token_log if needed, but the original codebase mostly uses DevCreditLog.
            await session.execute(
                insert(Payment.metadata.tables['token_log']).values(
                    user_id=user_id, site_id=site_id, delta=-VERSION_RESTORE_DEV_CREDITS, reason="analytics_restore"
                )
            )
            
            site = await site_repo.get(session, site_id)
            site.analytics_status = AnalyticsStatus.ACTIVE.value
            site.promo_status = PromotionStatus.CONFIGURED.value
            site.updated = datetime.utcnow()
            session.add(site)
            
            event = AnalyticsEvent(
                site_id=site_id,
                event_type="analytics_restored",
                payload_json="{}",
                created=datetime.utcnow()
            )
            session.add(event)
            await session.commit()
            
        return {"ok": True, "dev_credits": balance}

    @classmethod
    async def record_event(cls, site_id: int, event_type: str, payload: dict | None = None) -> dict:
        allowed = {
            "page_view",
            "cta_click",
            "whatsapp_click",
            "telegram_click",
            "instagram_click",
            "phone_click",
            "service_click",
        }
        event_type = (event_type or "").strip()
        if event_type not in allowed:
            event_type = "cta_click"
            
        async with AsyncSessionLocal() as session:
            event = AnalyticsEvent(
                site_id=site_id,
                event_type=event_type,
                payload_json=json.dumps(payload or {}, ensure_ascii=False)[:2000],
                created=datetime.utcnow()
            )
            session.add(event)
            await session.commit()
        return {"ok": True}

    @classmethod
    async def metrics(cls, site_id: int) -> dict:
        async with AsyncSessionLocal() as session:
            site = await site_repo.get(session, site_id)
            if not site:
                return {"visits": 0, "clicks": {}}
            slug = site.slug
            
            rows = await session.execute(
                select(AnalyticsEvent.event_type, func.count(AnalyticsEvent.id).label('cnt'))
                .filter_by(site_id=site_id)
                .group_by(AnalyticsEvent.event_type)
            )
            counts = {r.event_type: r.cnt for r in rows.all()}
            
        legacy_map = {
            "whatsapp_click": "WhatsApp",
            "telegram_click": "Telegram",
            "instagram_click": "Instagram",
            "phone_click": "Телефон",
            "service_click": "Прайс",
            "cta_click": "Действие"
        }
        
        clicks = {}
        from pathlib import Path
        GENERATED_DIR = Path("generated_sites")
        html_path = GENERATED_DIR / f"{slug}.html"
        
        if html_path.exists():
            html_content = html_path.read_text(encoding="utf-8")
            matches = re.findall(r'data-track="([^"]+)"', html_content)
            for m in matches:
                clicks[m] = 0
            if not matches:
                if "wa.me" in html_content or "whatsapp" in html_content.lower():
                    clicks["WhatsApp"] = 0
                if "t.me" in html_content or "telegram" in html_content.lower():
                    clicks["Telegram"] = 0
                if "instagram.com" in html_content:
                    clicks["Instagram"] = 0
                clicks["Действие"] = 0

        for event_type, cnt in counts.items():
            if event_type == "page_view":
                continue
            
            label = event_type
            if event_type.startswith("click:"):
                label = event_type.split(":", 1)[1]
            elif event_type in legacy_map:
                label = legacy_map[event_type]
            
            clicks[label] = clicks.get(label, 0) + cnt
            
        return {
            "visits": counts.get("page_view", 0),
            "clicks": clicks,
        }


class VersionService:
    @classmethod
    async def create_snapshot(cls, site_id: int, html: str, data: dict, reason: str):
        async with AsyncSessionLocal() as session:
            # find max version
            result = await session.execute(
                select(func.max(SiteVersion.version_no)).filter_by(site_id=site_id)
            )
            max_v = result.scalar() or 0
            sv = SiteVersion(
                site_id=site_id,
                version_no=max_v + 1,
                html=html,
                data=json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else (data or "{}"),
                reason=reason,
                created=datetime.utcnow()
            )
            session.add(sv)
            await session.commit()

    @classmethod
    async def list_versions(cls, user_id: int, site_id: int) -> list[dict]:
        async with AsyncSessionLocal() as session:
            site = await site_repo.get(session, site_id)
            if not site or site.user_id != user_id:
                return []
            result = await session.execute(
                select(SiteVersion).filter_by(site_id=site_id).order_by(desc(SiteVersion.created))
            )
            return [
                {
                    "id": v.id, "site_id": v.site_id, "version_no": v.version_no,
                    "reason": v.reason, "created": _fmt(v.created)
                } for v in result.scalars().all()
            ]

    @classmethod
    async def restore(cls, user_id: int, site_id: int, version_id: int) -> dict:
        site = await SupportService.refresh_site(site_id)
        if not site or site.user_id != user_id:
            return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}
        if not is_support_operational(site.support_status):
            return {"ok": False, "error": "support_inactive", "message": "Сначала оплатите поддержку сайта."}

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(SiteVersion).filter_by(id=version_id, site_id=site_id))
            version = result.scalars().first()
            if not version:
                return {"ok": False, "error": "version_not_found", "message": "Версия не найдена."}
                
            user = await user_repo.get(session, user_id)
            if user.dev_credits < VERSION_RESTORE_DEV_CREDITS:
                return {"ok": False, "error": "insufficient_dev_credits", "message": "Недостаточно кредитов разработки."}
                
            user.dev_credits -= VERSION_RESTORE_DEV_CREDITS
            user.tokens = max(user.tokens - VERSION_RESTORE_DEV_CREDITS, 0)
            session.add(user)
            await session.flush()
            balance = user.dev_credits
            
            log = DevCreditLog(
                user_id=user_id,
                site_id=site_id,
                delta=-VERSION_RESTORE_DEV_CREDITS,
                reason=f"version_restore:{version_id}",
                balance_after=balance,
                created=datetime.utcnow()
            )
            session.add(log)
            
            await session.execute(
                insert(Payment.metadata.tables['token_log']).values(
                    user_id=user_id, site_id=site_id, delta=-VERSION_RESTORE_DEV_CREDITS, reason=f"version_restore:{version_id}"
                )
            )
            
            site = await site_repo.get(session, site_id)
            site.data = version.data
            site.updated = datetime.utcnow()
            session.add(site)
            await session.commit()
            
        await CampaignService.site_changed(site_id, "version_restore")
        return {
            "ok": True,
            "html": version.html,
            "data": json.loads(version.data or "{}"),
            "dev_credits": balance,
        }

async def build_dashboard_context(user: User) -> dict:
    sites = await SupportService.refresh_user_sites(user.id)
    enriched = []
    for site in sites:
        invoice = await SupportService.get_open_invoice(site.id)
        campaigns = await CampaignService.history(site.id)
        active_campaign = next((c for c in campaigns if c.get("status") == CampaignStatus.ACTIVE.value), None)
        paid_until = _parse_dt(site.support_paid_until)
        support_status = site.support_status or SupportService.compute_status(site)
        
        s_dict = {
            "id": site.id,
            "slug": site.slug,
            "title": site.title,
            "support_status": support_status,
            "promo_status": site.promo_status,
            "analytics_status": site.analytics_status,
            "support_label": _status_label(support_status),
            "promotion_label": _status_label(site.promo_status or PromotionStatus.NOT_CONFIGURED.value),
            "analytics_label": _status_label(site.analytics_status or AnalyticsStatus.UNAVAILABLE.value),
            "support_operational": is_support_operational(support_status),
            "support_public": is_support_public(support_status),
            "support_paid_until_display": paid_until.strftime("%d.%m.%Y") if paid_until else "не задана",
            "support_invoice": {"id": invoice.id, "amount": invoice.amount} if invoice else None,
            "campaigns": campaigns,
            "active_campaign": active_campaign,
            "campaign_history_count": len(campaigns),
            "needs_analytics_restore": site.analytics_status == AnalyticsStatus.OUTDATED.value,
        }
        site_data = _site_data(site)
        if site_data.get("analytics_purchased"):
            s_dict["analytics_label"] = "Подключена"
            s_dict["analytics_metrics"] = await AnalyticsService.metrics(site.id)
            
        enriched.append(s_dict)
        
    async with AsyncSessionLocal() as session:
        user_db = await user_repo.get(session, user.id)
        notifications = await NotificationService.for_user(user.id)
        
        active_onboarding = await session.execute(
            select(OnboardingSession).filter(OnboardingSession.user_id == user.id, OnboardingSession.status.notin_(["completed"]))
            .order_by(desc(OnboardingSession.updated)).limit(1)
        )
        active_onboarding = active_onboarding.scalars().first()
        
    return {
        "user": user_db or user,
        "sites": enriched,
        "notifications": notifications,
        "unread_notifications": sum(1 for n in notifications if not n.get("is_read")),
        "active_onboarding": active_onboarding,
        "promo_setup_cost": PROMO_SETUP_COST,
        "promo_min_purchase": PROMO_MIN_PURCHASE,
        "promo_credit_tenge": PROMO_CREDIT_TENGE,
        "campaign_min_credits": CAMPAIGN_MIN_CREDITS,
        "campaign_min_duration_hours": CAMPAIGN_MIN_DURATION_HOURS,
        "support_monthly_price": SUPPORT_MONTHLY_PRICE,
        "version_restore_dev_credits": VERSION_RESTORE_DEV_CREDITS,
    }


class NotificationService:
    @classmethod
    async def sync_user(cls, user_id: int):
        async with AsyncSessionLocal() as session:
            sites = await site_repo.get_multi_by_user(session, user_id)
            existing_res = await session.execute(
                select(Notification).filter_by(user_id=user_id).order_by(desc(Notification.created)).limit(50)
            )
            existing = existing_res.scalars().all()
            keys = {(n.type, n.site_id) for n in existing}
            
            for site in sites:
                status = site.support_status
                if status == SupportStatus.EXPIRING_SOON.value and ("support_expiring", site.id) not in keys:
                    session.add(Notification(
                        user_id=user_id, type="support_expiring",
                        title="Поддержка скоро закончится",
                        body=f"Страница «{site.title or site.slug}» скоро потребует продления.",
                        site_id=site.id, created=datetime.utcnow()
                    ))
                if status == SupportStatus.SUSPENDED.value and ("support_suspended", site.id) not in keys:
                    session.add(Notification(
                        user_id=user_id, type="support_suspended",
                        title="Страница приостановлена",
                        body="Правки, продвижение и аналитика заблокированы до продления поддержки.",
                        site_id=site.id, created=datetime.utcnow()
                    ))
                if site.analytics_status == AnalyticsStatus.OUTDATED.value and ("analytics_outdated", site.id) not in keys:
                    session.add(Notification(
                        user_id=user_id, type="analytics_outdated",
                        title="Аналитика устарела",
                        body="После правок нужно восстановить аналитику перед продвижением.",
                        site_id=site.id, created=datetime.utcnow()
                    ))
            await session.commit()

    @classmethod
    async def for_user(cls, user_id: int) -> list[dict]:
        await cls.sync_user(user_id)
        async with AsyncSessionLocal() as session:
            rows = await session.execute(
                select(Notification).filter_by(user_id=user_id).order_by(desc(Notification.created))
            )
            return [
                {
                    "id": n.id, "user_id": n.user_id, "site_id": n.site_id,
                    "type": n.type, "title": n.title, "body": n.body,
                    "is_read": n.is_read, "created": _fmt(n.created)
                } for n in rows.scalars().all()
            ]

class OnboardingService:
    REQUIRED_KEYS = ("name", "services", "city", "vibe")
    ALLOWED_STATUSES = {"draft", "ready", "generating", "failed", "completed"}

    @staticmethod
    def _safe_history(value) -> list:
        if not isinstance(value, list):
            return []
        safe = []
        for msg in value[-80:]:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = str(msg.get("content") or "").strip()
            if content:
                safe.append({"role": role, "content": content[:4000]})
        return safe

    @staticmethod
    def _safe_collected(value) -> dict:
        if not isinstance(value, dict):
            return {}
        return {
            key: str(value.get(key) or "").strip()[:2000]
            for key in OnboardingService.REQUIRED_KEYS
            if value.get(key)
        }

    @staticmethod
    def _safe_photo_urls(value) -> list:
        if not isinstance(value, list):
            return []
        return [
            str(url)[:500]
            for url in value[:12]
            if isinstance(url, str) and url.startswith("/static/uploads/")
        ]

    @staticmethod
    def _safe_status(value) -> str:
        status = str(value or "draft")
        return status if status in OnboardingService.ALLOWED_STATUSES else "draft"

    @staticmethod
    def _safe_int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    async def current(cls, user_id: int) -> dict:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(OnboardingSession).filter(OnboardingSession.user_id == user_id, OnboardingSession.status.notin_(["completed"]))
                .order_by(desc(OnboardingSession.updated)).limit(1)
            )
            onb_session = result.scalars().first()
            if not onb_session:
                onb_session = OnboardingSession(
                    user_id=user_id,
                    status="draft",
                    history="[]",
                    collected="{}",
                    photo_urls="[]",
                    created=datetime.utcnow(),
                    updated=datetime.utcnow()
                )
                session.add(onb_session)
                await session.commit()
                await session.refresh(onb_session)
                
        return cls.present(onb_session)

    @classmethod
    def present(cls, onb_session: OnboardingSession | dict | None) -> dict:
        if not onb_session:
            return {"session": None, "summary": [], "progress": 0, "missing": list(cls.REQUIRED_KEYS)}
            
        is_orm = hasattr(onb_session, 'collected')
        col_str = onb_session.collected if is_orm else onb_session.get("collected")
        try:
            collected = json.loads(col_str) if isinstance(col_str, str) else col_str
        except:
            collected = {}
            
        photo_urls_str = onb_session.photo_urls if is_orm else onb_session.get("photo_urls")
        try:
            photo_urls = json.loads(photo_urls_str) if isinstance(photo_urls_str, str) else photo_urls_str
        except:
            photo_urls = []
            
        done = [key for key in cls.REQUIRED_KEYS if collected.get(key)]
        summary = [
            {"key": "name", "label_key": "create_summary_business", "value": collected.get("name") or ""},
            {"key": "services", "label_key": "create_summary_services", "value": collected.get("services") or ""},
            {"key": "city", "label_key": "create_summary_contacts", "value": collected.get("city") or ""},
            {"key": "vibe", "label_key": "create_summary_style", "value": collected.get("vibe") or ""},
            {
                "key": "photos",
                "label_key": "create_summary_photos",
                "value": f"{len(photo_urls or [])} фото" if photo_urls else "",
            },
        ]
        
        session_dict = {
            "id": onb_session.id if is_orm else onb_session.get("id"),
            "status": onb_session.status if is_orm else onb_session.get("status"),
            "draft_title": onb_session.draft_title if is_orm else onb_session.get("draft_title"),
            "history": json.loads(onb_session.history) if is_orm and isinstance(onb_session.history, str) else (onb_session.history if is_orm else onb_session.get("history", [])),
            "collected": collected,
            "photo_urls": photo_urls,
            "error": onb_session.error if is_orm else onb_session.get("error"),
        }
        
        return {
            "session": session_dict,
            "summary": summary,
            "progress": int(len(done) / len(cls.REQUIRED_KEYS) * 100),
            "missing": [key for key in cls.REQUIRED_KEYS if key not in done],
        }

    @classmethod
    async def autosave(cls, user_id: int, payload: dict) -> dict:
        session_id = payload.get("session_id")
        async with AsyncSessionLocal() as db_session:
            onb_session = None
            if session_id:
                res = await db_session.execute(select(OnboardingSession).filter_by(id=session_id, user_id=user_id))
                onb_session = res.scalars().first()
                
            if not onb_session:
                onb_session = OnboardingSession(
                    user_id=user_id,
                    created=datetime.utcnow()
                )
                db_session.add(onb_session)
                
            onb_session.status = cls._safe_status(payload.get("status"))
            onb_session.history = json.dumps(cls._safe_history(payload.get("history")), ensure_ascii=False)
            onb_session.collected = json.dumps(cls._safe_collected(payload.get("collected")), ensure_ascii=False)
            onb_session.photo_urls = json.dumps(cls._safe_photo_urls(payload.get("photo_urls")), ensure_ascii=False)
            onb_session.chat_in = cls._safe_int(payload.get("chat_in"))
            onb_session.chat_out = cls._safe_int(payload.get("chat_out"))
            onb_session.chat_cr = cls._safe_int(payload.get("chat_cr"))
            onb_session.updated = datetime.utcnow()
            
            await db_session.commit()
            await db_session.refresh(onb_session)
            return cls.present(onb_session)

    @classmethod
    async def reset(cls, user_id: int) -> dict:
        async with AsyncSessionLocal() as session:
            onb_session = OnboardingSession(
                user_id=user_id,
                status="draft",
                history="[]",
                collected="{}",
                photo_urls="[]",
                created=datetime.utcnow(),
                updated=datetime.utcnow()
            )
            session.add(onb_session)
            await session.commit()
            await session.refresh(onb_session)
        return cls.present(onb_session)

    @classmethod
    async def delete(cls, user_id: int, session_id: int) -> dict:
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(OnboardingSession).filter_by(id=session_id, user_id=user_id))
            onb = res.scalars().first()
            if onb:
                await session.delete(onb)
                await session.commit()
                return {"deleted": True}
        return {"deleted": False}

    @classmethod
    async def rename(cls, user_id: int, session_id: int, title: str) -> dict:
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(OnboardingSession).filter_by(id=session_id, user_id=user_id))
            onb = res.scalars().first()
            if onb:
                onb.draft_title = title
                onb.updated = datetime.utcnow()
                await session.commit()
                await session.refresh(onb)
            return cls.present(onb)

    @classmethod
    async def reorder(cls, user_id: int, session_ids: list[int]) -> dict:
        async with AsyncSessionLocal() as session:
            for i, sid in enumerate(session_ids):
                res = await session.execute(select(OnboardingSession).filter_by(id=sid, user_id=user_id))
                onb = res.scalars().first()
                if onb:
                    onb.sort_order = i
                    onb.updated = datetime.utcnow()
            await session.commit()
        return {"reordered": True}


async def build_site_workspace_context(user: User, site_id: int) -> dict | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Site).filter_by(id=site_id, user_id=user.id))
        site = result.scalars().first()
        
    if not site:
        return None
        
    site = await SupportService.refresh_site(site.id) or site
    await CampaignService.refresh_site_campaigns(site.id)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Site).filter_by(id=site_id, user_id=user.id))
        site = result.scalars().first() or site
        
    paid_until = _parse_dt(site.support_paid_until)
    campaigns = await CampaignService.history(site.id)
    
    s_dict = {
        "id": site.id,
        "slug": site.slug,
        "title": site.title,
        "support_status": site.support_status,
        "promo_status": site.promo_status,
        "analytics_status": site.analytics_status,
        "support_label": _status_label(site.support_status or ""),
        "promotion_label": _status_label(site.promo_status or ""),
        "analytics_label": _status_label(site.analytics_status or ""),
        "support_operational": is_support_operational(site.support_status),
        "support_paid_until_display": paid_until.strftime("%d.%m.%Y") if paid_until else "не задана",
        "support_invoice": await SupportService.get_open_invoice(site.id),
        "campaigns": campaigns,
        "active_campaign": next((c for c in campaigns if c.get("status") == CampaignStatus.ACTIVE.value), None),
        "needs_analytics_restore": site.analytics_status == AnalyticsStatus.OUTDATED.value,
        "data": _site_data(site),
    }
    
    versions = await VersionService.list_versions(user.id, site.id)
    site_data = _site_data(site)
    analytics_metrics = (
        await AnalyticsService.metrics(site.id)
        if site_data.get("analytics_purchased")
        else {
            "visits": 0,
            "cta_clicks": 0,
            "whatsapp_clicks": 0,
            "telegram_clicks": 0,
            "instagram_clicks": 0,
            "phone_clicks": 0,
        }
    )
    
    dashboard_ctx = await build_dashboard_context(user)
    
    return {
        **dashboard_ctx,
        "selected_site": s_dict,
        "versions": versions,
        "analytics_metrics": analytics_metrics,
    }


def maintenance_page() -> str:
    return """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Сайт временно недоступен</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0a0a0f;color:#f4f0ff;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.box{max-width:420px;padding:34px 24px;text-align:center}
.logo{font-weight:800;font-size:1.05rem;margin-bottom:30px}.logo span{color:#7c5cff}
.badge{display:inline-flex;padding:8px 12px;border:1px solid #7c5cff44;border-radius:999px;color:#a995ff;background:#7c5cff18;font-size:.78rem;font-weight:700;margin-bottom:16px}
h1{font-size:1.45rem;margin:0 0 10px;letter-spacing:-.03em}
p{margin:0;color:#8d88a8;line-height:1.6;font-size:.95rem}
</style>
</head>
<body><main class="box"><div class="logo">lendings<span>.kz</span></div><div class="badge">Обслуживание сайта</div><h1>Site temporarily unavailable</h1><p>Страница временно недоступна. Владелец сайта скоро восстановит поддержку.</p></main></body></html>"""

