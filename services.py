import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

import db
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
    # now
    return datetime.utcnow().replace(microsecond=0)


def _fmt(dt: datetime) -> str:
    # fmt
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(value: str | None) -> datetime | None:
    # parse dt
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _rowdict(row: Any) -> dict | None:
    # rowdict
    return dict(row) if row else None


def _site_data(site: dict) -> dict:
    # site data
    data = site.get("data") or {}
    if isinstance(data, str):
        try:
            return json.loads(data or "{}")
        except json.JSONDecodeError:
            return {}
    return data


def is_support_operational(status: str | None) -> bool:
    # validate support state
    return status in {SupportStatus.ACTIVE.value, SupportStatus.EXPIRING_SOON.value}


def is_support_public(status: str | None) -> bool:
    # is support public
    return status in {
        SupportStatus.ACTIVE.value,
        SupportStatus.EXPIRING_SOON.value,
        SupportStatus.INVOICE_ISSUED.value,
    }


def _status_label(status: str) -> str:
    # status label
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
    # support service class
    @staticmethod
    def initial_paid_until() -> str:
        # initial paid until
        return _fmt(_now() + timedelta(days=SUPPORT_INCLUDED_DAYS))

    @staticmethod
    def compute_status(site: dict, now: datetime | None = None) -> str:
        # compute support status based on dates
        now = now or _now()
        paid_until = _parse_dt(site.get("support_paid_until"))
        if not paid_until:
            return SupportStatus.SUSPENDED.value
        if now <= paid_until:
            if paid_until - now <= timedelta(days=SUPPORT_WARNING_DAYS):
                return SupportStatus.EXPIRING_SOON.value
            return SupportStatus.ACTIVE.value
        if now <= paid_until + timedelta(days=SUPPORT_GRACE_DAYS):
            return SupportStatus.INVOICE_ISSUED.value
        return SupportStatus.SUSPENDED.value

    @staticmethod
    def refresh_site(site_id: int) -> dict | None:
        # refresh site support status
        now = _now()
        with db.get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            site = _rowdict(c.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone())
            if not site:
                return None

            status = SupportService.compute_status(site, now)
            paid_until = _parse_dt(site.get("support_paid_until"))
            if status in {SupportStatus.INVOICE_ISSUED.value, SupportStatus.SUSPENDED.value}:
                SupportService._ensure_invoice(c, site, paid_until or now)
                c.execute(
                    """UPDATE sites
                       SET analytics_status=?, promo_status=?, support_status=?, updated=datetime('now')
                       WHERE id=?""",
                    (
                        AnalyticsStatus.BLOCKED.value,
                        PromotionStatus.PAUSED.value,
                        status,
                        site_id,
                    ),
                )
                CampaignService._stop_active_for_site(
                    c,
                    site_id,
                    CampaignStatus.STOPPED_SUPPORT_EXPIRED.value,
                )
            else:
                c.execute(
                    "UPDATE sites SET support_status=?, updated=datetime('now') WHERE id=?",
                    (status, site_id),
                )

            row = c.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
            result = dict(row)
            result["data"] = _site_data(result)
            return result

    @staticmethod
    def _ensure_invoice(c, site: dict, expired_at: datetime):
        # ensure invoice
        existing = c.execute(
            """SELECT id FROM support_invoices
               WHERE site_id=? AND status=?
               ORDER BY created DESC LIMIT 1""",
            (site["id"], InvoiceStatus.PENDING.value),
        ).fetchone()
        if existing:
            return
        due_at = expired_at + timedelta(days=SUPPORT_GRACE_DAYS)
        c.execute(
            """INSERT INTO support_invoices
               (user_id, site_id, amount, months, status, due_at, created, updated)
               VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (
                site["user_id"],
                site["id"],
                SUPPORT_MONTHLY_PRICE,
                1,
                InvoiceStatus.PENDING.value,
                _fmt(due_at),
            ),
        )

    @staticmethod
    def refresh_user_sites(user_id: int) -> list[dict]:
        # refresh all user sites statuses
        sites = db.get_user_sites(user_id)
        refreshed = []
        for site in sites:
            updated = SupportService.refresh_site(site["id"]) or site
            CampaignService.refresh_site_campaigns(updated["id"])
            refreshed.append(db.get_site_by_id(updated["id"]) or updated)
        return refreshed

    @staticmethod
    def get_open_invoice(site_id: int) -> dict | None:
        # get open invoice
        with db.get_conn() as c:
            return _rowdict(c.execute(
                """SELECT * FROM support_invoices
                   WHERE site_id=? AND status=?
                   ORDER BY created DESC LIMIT 1""",
                (site_id, InvoiceStatus.PENDING.value),
            ).fetchone())

    @staticmethod
    def pay_invoice(user_id: int, site_id: int) -> dict:
        # process support invoice payment
        now = _now()
        with db.get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            site = _rowdict(c.execute(
                "SELECT * FROM sites WHERE id=? AND user_id=?",
                (site_id, user_id),
            ).fetchone())
            if not site:
                return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}

            status = SupportService.compute_status(site, now)
            if status == SupportStatus.ACTIVE.value:
                return {"ok": False, "error": "support_active", "message": "Поддержка уже активна."}

            paid_until = _parse_dt(site.get("support_paid_until")) or now
            if not c.execute(
                """SELECT id FROM support_invoices
                   WHERE site_id=? AND status=?
                   ORDER BY created DESC LIMIT 1""",
                (site_id, InvoiceStatus.PENDING.value),
            ).fetchone():
                SupportService._ensure_invoice(c, site, paid_until)

            invoice = _rowdict(c.execute(
                """SELECT * FROM support_invoices
                   WHERE site_id=? AND status=?
                   ORDER BY created DESC LIMIT 1""",
                (site_id, InvoiceStatus.PENDING.value),
            ).fetchone())
            if not invoice:
                return {"ok": False, "error": "invoice_unavailable", "message": "Счёт не найден."}

            extend_from = max(now, paid_until)
            new_paid_until = extend_from + timedelta(days=SUPPORT_INCLUDED_DAYS)
            order_id = f"support-{uuid.uuid4().hex[:12]}"
            c.execute(
                """UPDATE support_invoices
                   SET status=?, paid_at=datetime('now'), order_id=?, updated=datetime('now')
                   WHERE id=? AND status=?""",
                (InvoiceStatus.PAID.value, order_id, invoice["id"], InvoiceStatus.PENDING.value),
            )
            c.execute(
                """INSERT INTO payments
                   (user_id, order_id, invoice_id, amount, tokens, status,
                    payment_kind, site_id, support_invoice_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    order_id,
                    "",
                    int(invoice["amount"]),
                    0,
                    "paid",
                    "support_invoice",
                    site_id,
                    invoice["id"],
                ),
            )
            analytics_status = site.get("analytics_status") or AnalyticsStatus.UNAVAILABLE.value
            if analytics_status == AnalyticsStatus.BLOCKED.value and int(site.get("promo_setup_done") or 0):
                analytics_status = AnalyticsStatus.ACTIVE.value
            promo_status = site.get("promo_status") or PromotionStatus.NOT_CONFIGURED.value
            if promo_status == PromotionStatus.PAUSED.value and int(site.get("promo_setup_done") or 0):
                promo_status = PromotionStatus.CONFIGURED.value
            c.execute(
                """UPDATE sites
                   SET support_paid_until=?, support_status=?, analytics_status=?, promo_status=?, updated=datetime('now')
                   WHERE id=?""",
                (
                    _fmt(new_paid_until),
                    SupportStatus.ACTIVE.value,
                    analytics_status,
                    promo_status,
                    site_id,
                ),
            )
            return {
                "ok": True,
                "support_paid_until": _fmt(new_paid_until),
                "amount": SUPPORT_MONTHLY_PRICE,
            }

    @staticmethod
    def mark_invoice_paid(payment: dict) -> dict:
        # apply a paid support invoice from payment webhook
        invoice_id = payment.get("support_invoice_id")
        site_id = payment.get("site_id")
        user_id = payment.get("user_id")
        now = _now()
        if not invoice_id or not site_id or not user_id:
            return {"ok": False, "error": "invalid_support_payment"}
        with db.get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            invoice = _rowdict(c.execute(
                """SELECT * FROM support_invoices
                   WHERE id=? AND user_id=? AND site_id=?""",
                (invoice_id, user_id, site_id),
            ).fetchone())
            site = _rowdict(c.execute(
                "SELECT * FROM sites WHERE id=? AND user_id=?",
                (site_id, user_id),
            ).fetchone())
            if not invoice or not site:
                return {"ok": False, "error": "support_invoice_not_found"}
            if invoice.get("status") == InvoiceStatus.PAID.value:
                return {"ok": True, "already_paid": True}

            paid_until = _parse_dt(site.get("support_paid_until")) or now
            extend_from = max(now, paid_until)
            new_paid_until = extend_from + timedelta(days=SUPPORT_INCLUDED_DAYS)
            analytics_status = site.get("analytics_status") or AnalyticsStatus.UNAVAILABLE.value
            if analytics_status == AnalyticsStatus.BLOCKED.value and int(site.get("promo_setup_done") or 0):
                analytics_status = AnalyticsStatus.ACTIVE.value
            promo_status = site.get("promo_status") or PromotionStatus.NOT_CONFIGURED.value
            if promo_status == PromotionStatus.PAUSED.value and int(site.get("promo_setup_done") or 0):
                promo_status = PromotionStatus.CONFIGURED.value
            c.execute(
                """UPDATE support_invoices
                   SET status=?, paid_at=datetime('now'), order_id=?, updated=datetime('now')
                   WHERE id=?""",
                (InvoiceStatus.PAID.value, payment.get("order_id"), invoice_id),
            )
            c.execute(
                """UPDATE sites
                   SET support_paid_until=?, support_status=?, analytics_status=?, promo_status=?, updated=datetime('now')
                   WHERE id=?""",
                (
                    _fmt(new_paid_until),
                    SupportStatus.ACTIVE.value,
                    analytics_status,
                    promo_status,
                    site_id,
                ),
            )
            return {"ok": True, "support_paid_until": _fmt(new_paid_until)}


class CreditsService:
    # credits service class
    @staticmethod
    def purchase_promo_credits(user_id: int, credits: int) -> dict:
        # process promo credits purchase
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
        with db.get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                """INSERT INTO payments
                   (user_id, order_id, invoice_id, amount, tokens, status,
                    catalog_item_id, payment_kind, promo_credits, dev_credits)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    order_id,
                    "",
                    amount,
                    0,
                    "paid",
                    "",
                    "promo_credits",
                    credits,
                    0,
                ),
            )
            c.execute("UPDATE users SET promo_credits=promo_credits+? WHERE id=?", (credits, user_id))
            c.execute(
                """INSERT INTO promo_credit_log
                   (user_id, delta, reason, balance_after, created)
                   VALUES (?,?,?,?,datetime('now'))""",
                (
                    user_id,
                    credits,
                    f"promo_credit_purchase:{order_id}",
                    c.execute("SELECT promo_credits FROM users WHERE id=?", (user_id,)).fetchone()[0],
                ),
            )
            balance = c.execute("SELECT promo_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
        return {"ok": True, "credits": credits, "amount": amount, "balance": balance, "order_id": order_id}

    @staticmethod
    def apply_promo_payment(payment: dict) -> dict:
        # add promotion credits after external payment succeeds
        credits = int(payment.get("promo_credits") or 0)
        user_id = int(payment.get("user_id") or 0)
        if credits <= 0 or user_id <= 0:
            return {"ok": False, "error": "invalid_promo_payment"}
        with db.get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE users SET promo_credits=promo_credits+? WHERE id=?", (credits, user_id))
            balance = c.execute("SELECT promo_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
            c.execute(
                """INSERT INTO promo_credit_log
                   (user_id, delta, reason, balance_after, created)
                   VALUES (?,?,?,?,datetime('now'))""",
                (user_id, credits, f"promo_credit_purchase:{payment.get('order_id')}", balance),
            )
        return {"ok": True, "credits": credits, "balance": balance}

    @staticmethod
    def logs(user_id: int, limit: int = 50) -> dict:
        # logs
        return {
            "dev": db.get_dev_credit_log(user_id, limit),
            "promo": db.get_promo_credit_log(user_id, limit),
        }


class ForecastService:
    # forecast service class
    @staticmethod
    def build(site: dict, credits: int, duration_hours: int) -> dict:
        # generate promotion forecast metrics
        credits = int(credits)
        duration_hours = int(duration_hours)
        if credits < CAMPAIGN_MIN_CREDITS:
            raise ValueError(f"Минимум {CAMPAIGN_MIN_CREDITS} кредитов.")
        if duration_hours < CAMPAIGN_MIN_DURATION_HOURS:
            raise ValueError(f"Минимум {CAMPAIGN_MIN_DURATION_HOURS} часа.")

        data = _site_data(site)
        niche_text = " ".join([
            str(site.get("title") or ""),
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
    # promotion service class
    @staticmethod
    def setup(user_id: int, site_id: int) -> dict:
        # configure promotion setup for site
        site = SupportService.refresh_site(site_id)
        if not site or site["user_id"] != user_id:
            return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}
        if not is_support_operational(site.get("support_status")):
            return {"ok": False, "error": "support_inactive", "message": "Сначала оплатите поддержку сайта."}
        if int(site.get("promo_setup_done") or 0):
            return {"ok": False, "error": "already_configured", "message": "Продвижение уже настроено."}

        with db.get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            updated = c.execute(
                """UPDATE users
                   SET promo_credits=promo_credits-?
                   WHERE id=? AND promo_credits>=?""",
                (PROMO_SETUP_COST, user_id, PROMO_SETUP_COST),
            )
            if updated.rowcount != 1:
                return {
                    "ok": False,
                    "error": "insufficient_promo_credits",
                    "message": f"Нужно {PROMO_SETUP_COST} кредитов продвижения.",
                }
            balance = c.execute("SELECT promo_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
            c.execute(
                """INSERT INTO promo_credit_log
                   (user_id, site_id, delta, reason, balance_after, created)
                   VALUES (?,?,?,?,?,datetime('now'))""",
                (user_id, site_id, -PROMO_SETUP_COST, "promotion_setup", balance),
            )
            c.execute(
                """INSERT INTO promotion_setups
                   (user_id, site_id, credits_spent, status, created, updated)
                   VALUES (?,?,?,?,datetime('now'),datetime('now'))""",
                (user_id, site_id, PROMO_SETUP_COST, "completed"),
            )
            c.execute(
                """UPDATE sites
                   SET promo_setup_done=1,
                       promo_status=?,
                       analytics_status=?,
                       updated=datetime('now')
                   WHERE id=?""",
                (PromotionStatus.CONFIGURED.value, AnalyticsStatus.ACTIVE.value, site_id),
            )
            return {"ok": True, "promo_credits": balance}

    @staticmethod
    def validate_business_change(site: dict, edit_summary: str) -> dict:
        # prevent changing business niche
        text = (edit_summary or "").lower()
        data = _site_data(site)
        current = " ".join([
            str(site.get("title") or ""),
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
    # campaign service class
    @staticmethod
    def _stop_active_for_site(c, site_id: int, status: str):
        # stop active for site
        c.execute(
            """UPDATE promotion_campaigns
               SET status=?, stopped_reason=?, updated=datetime('now')
               WHERE site_id=? AND status=?""",
            (status, status, site_id, CampaignStatus.ACTIVE.value),
        )

    @staticmethod
    def refresh_site_campaigns(site_id: int):
        # refresh site campaign statuses
        now = _now()
        with db.get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            site = _rowdict(c.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone())
            if not site:
                return
            if not is_support_operational(site.get("support_status")):
                CampaignService._stop_active_for_site(
                    c,
                    site_id,
                    CampaignStatus.STOPPED_SUPPORT_EXPIRED.value,
                )
                return
            c.execute(
                """UPDATE promotion_campaigns
                   SET status=?, updated=datetime('now')
                   WHERE site_id=? AND status=? AND ends_at<=?""",
                (
                    CampaignStatus.COMPLETED.value,
                    site_id,
                    CampaignStatus.ACTIVE.value,
                    _fmt(now),
                ),
            )
            active = c.execute(
                "SELECT id FROM promotion_campaigns WHERE site_id=? AND status=? LIMIT 1",
                (site_id, CampaignStatus.ACTIVE.value),
            ).fetchone()
            next_status = PromotionStatus.ACTIVE.value if active else (
                PromotionStatus.CONFIGURED.value if int(site.get("promo_setup_done") or 0) else PromotionStatus.NOT_CONFIGURED.value
            )
            c.execute(
                "UPDATE sites SET promo_status=?, updated=datetime('now') WHERE id=?",
                (next_status, site_id),
            )

    @staticmethod
    def forecast(user_id: int, site_id: int, credits: int, duration_hours: int) -> dict:
        # calculate promotion forecast
        site = SupportService.refresh_site(site_id)
        if not site or site["user_id"] != user_id:
            return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}
        if not is_support_operational(site.get("support_status")):
            return {"ok": False, "error": "support_inactive", "message": "Сначала оплатите поддержку сайта."}
        if not int(site.get("promo_setup_done") or 0):
            return {"ok": False, "error": "promo_not_configured", "message": "Сначала настройте продвижение."}
        if site.get("analytics_status") != AnalyticsStatus.ACTIVE.value:
            return {"ok": False, "error": "analytics_outdated", "message": "Сначала восстановите аналитику."}
        try:
            return {"ok": True, "forecast": ForecastService.build(site, credits, duration_hours)}
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": "invalid_campaign", "message": str(exc)}

    @staticmethod
    def launch(user_id: int, site_id: int, credits: int, duration_hours: int) -> dict:
        # launch promotion campaign
        site = SupportService.refresh_site(site_id)
        if not site or site["user_id"] != user_id:
            return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}
        if not is_support_operational(site.get("support_status")):
            return {"ok": False, "error": "support_inactive", "message": "Сначала оплатите поддержку сайта."}
        if not int(site.get("promo_setup_done") or 0):
            return {"ok": False, "error": "promo_not_configured", "message": "Сначала настройте продвижение."}
        if site.get("analytics_status") != AnalyticsStatus.ACTIVE.value:
            return {"ok": False, "error": "analytics_outdated", "message": "Сначала восстановите аналитику."}
        try:
            forecast = ForecastService.build(site, credits, duration_hours)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": "invalid_campaign", "message": str(exc)}

        starts_at = _now()
        ends_at = starts_at + timedelta(hours=int(duration_hours))
        with db.get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            active = c.execute(
                """SELECT id FROM promotion_campaigns
                   WHERE site_id=? AND status=?
                   LIMIT 1""",
                (site_id, CampaignStatus.ACTIVE.value),
            ).fetchone()
            if active:
                return {"ok": False, "error": "active_campaign_exists", "message": "Кампания уже запущена."}
            updated = c.execute(
                """UPDATE users
                   SET promo_credits=promo_credits-?
                   WHERE id=? AND promo_credits>=?""",
                (int(credits), user_id, int(credits)),
            )
            if updated.rowcount != 1:
                return {
                    "ok": False,
                    "error": "insufficient_promo_credits",
                    "message": "Недостаточно кредитов продвижения.",
                }
            balance = c.execute("SELECT promo_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
            c.execute(
                """INSERT INTO promotion_campaigns
                   (user_id, site_id, credits_spent, duration_hours, status,
                    forecast_json, starts_at, ends_at, created, updated)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
                (
                    user_id,
                    site_id,
                    int(credits),
                    int(duration_hours),
                    CampaignStatus.ACTIVE.value,
                    json.dumps(forecast, ensure_ascii=False),
                    _fmt(starts_at),
                    _fmt(ends_at),
                ),
            )
            campaign_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute(
                """INSERT INTO promo_credit_log
                   (user_id, site_id, campaign_id, delta, reason, balance_after, created)
                   VALUES (?,?,?,?,?,?,datetime('now'))""",
                (
                    user_id,
                    site_id,
                    campaign_id,
                    -int(credits),
                    f"campaign_launch:{campaign_id}",
                    balance,
                ),
            )
            c.execute(
                "UPDATE sites SET promo_status=?, updated=datetime('now') WHERE id=?",
                (PromotionStatus.ACTIVE.value, site_id),
            )
        return {"ok": True, "campaign_id": campaign_id, "promo_credits": balance, "forecast": forecast}

    @staticmethod
    def site_changed(site_id: int, reason: str):
        # pause campaigns after site edit
        with db.get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            site = _rowdict(c.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone())
            if not site:
                return
            analytics_status = (
                AnalyticsStatus.OUTDATED.value
                if int(site.get("promo_setup_done") or 0)
                else AnalyticsStatus.UNAVAILABLE.value
            )
            promo_status = (
                PromotionStatus.PAUSED.value
                if int(site.get("promo_setup_done") or 0)
                else PromotionStatus.NOT_CONFIGURED.value
            )
            CampaignService._stop_active_for_site(c, site_id, CampaignStatus.STOPPED_SITE_CHANGED.value)
            c.execute(
                """UPDATE sites
                   SET analytics_status=?, promo_status=?, updated=datetime('now')
                   WHERE id=?""",
                (analytics_status, promo_status, site_id),
            )
            c.execute(
                """INSERT INTO analytics_events
                   (site_id, event_type, payload_json, created)
                   VALUES (?,?,?,datetime('now'))""",
                (site_id, "site_changed", json.dumps({"reason": reason}, ensure_ascii=False)),
            )

    @staticmethod
    def history(site_id: int) -> list[dict]:
        # retrieve site campaign history
        CampaignService.refresh_site_campaigns(site_id)
        with db.get_conn() as c:
            rows = c.execute(
                """SELECT * FROM promotion_campaigns
                   WHERE site_id=?
                   ORDER BY created DESC LIMIT 20""",
                (site_id,),
            ).fetchall()
            return [CampaignService.present_campaign(dict(r)) for r in rows]

    @staticmethod
    def present_campaign(campaign: dict) -> dict:
        # present campaign
        starts_at = _parse_dt(campaign.get("starts_at"))
        ends_at = _parse_dt(campaign.get("ends_at"))
        now = _now()
        progress = 0
        if starts_at and ends_at and ends_at > starts_at:
            progress = int(min(100, max(0, (now - starts_at).total_seconds() / (ends_at - starts_at).total_seconds() * 100)))
        try:
            forecast = json.loads(campaign.get("forecast_json") or "{}")
        except json.JSONDecodeError:
            forecast = {}
        campaign["forecast"] = forecast
        campaign["progress"] = 100 if campaign.get("status") == CampaignStatus.COMPLETED.value else progress
        campaign["status_label"] = _status_label(campaign.get("status") or "")
        return campaign


class AnalyticsService:
    # analytics service class
    @staticmethod
    def restore(user_id: int, site_id: int) -> dict:
        # restore site analytics
        site = SupportService.refresh_site(site_id)
        if not site or site["user_id"] != user_id:
            return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}
        if not is_support_operational(site.get("support_status")):
            return {"ok": False, "error": "support_inactive", "message": "Сначала оплатите поддержку сайта."}
        if not int(site.get("promo_setup_done") or 0):
            return {"ok": False, "error": "promo_not_configured", "message": "Сначала настройте продвижение."}
        with db.get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            spent = c.execute(
                """UPDATE users
                   SET dev_credits=dev_credits-?, tokens=MAX(tokens-?,0)
                   WHERE id=? AND dev_credits>=?""",
                (
                    VERSION_RESTORE_DEV_CREDITS,
                    VERSION_RESTORE_DEV_CREDITS,
                    user_id,
                    VERSION_RESTORE_DEV_CREDITS,
                ),
            )
            if spent.rowcount != 1:
                return {"ok": False, "error": "insufficient_dev_credits", "message": "Недостаточно кредитов разработки."}
            balance = c.execute("SELECT dev_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
            c.execute(
                """INSERT INTO dev_credit_log
                   (user_id, site_id, delta, reason, balance_after, created)
                   VALUES (?,?,?,?,?,datetime('now'))""",
                (user_id, site_id, -VERSION_RESTORE_DEV_CREDITS, "analytics_restore", balance),
            )
            c.execute(
                """INSERT INTO token_log (user_id, site_id, delta, reason)
                   VALUES (?,?,?,?)""",
                (user_id, site_id, -VERSION_RESTORE_DEV_CREDITS, "analytics_restore"),
            )
            c.execute(
                """UPDATE sites
                   SET analytics_status=?, promo_status=?, updated=datetime('now')
                   WHERE id=?""",
                (AnalyticsStatus.ACTIVE.value, PromotionStatus.CONFIGURED.value, site_id),
            )
            c.execute(
                """INSERT INTO analytics_events (site_id, event_type, payload_json, created)
                   VALUES (?,?,?,datetime('now'))""",
                (site_id, "analytics_restored", "{}"),
            )
        return {"ok": True, "dev_credits": balance}

    @staticmethod
    def record_event(site_id: int, event_type: str, payload: dict | None = None) -> dict:
        # record a public site analytics event
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
        with db.get_conn() as c:
            c.execute(
                """INSERT INTO analytics_events
                   (site_id, event_type, payload_json, created)
                   VALUES (?,?,?,datetime('now'))""",
                (site_id, event_type, json.dumps(payload or {}, ensure_ascii=False)[:2000]),
            )
        return {"ok": True}

    @staticmethod
    def metrics(site_id: int) -> dict:
        # aggregate analytics events for dashboard
        with db.get_conn() as c:
            rows = c.execute(
                """SELECT event_type, COUNT(*) as cnt
                   FROM analytics_events
                   WHERE site_id=?
                   GROUP BY event_type""",
                (site_id,),
            ).fetchall()
        counts = {r["event_type"]: int(r["cnt"]) for r in rows}
        return {
            "visits": counts.get("page_view", 0),
            "cta_clicks": counts.get("cta_click", 0) + counts.get("service_click", 0),
            "whatsapp_clicks": counts.get("whatsapp_click", 0),
            "telegram_clicks": counts.get("telegram_click", 0),
            "instagram_clicks": counts.get("instagram_click", 0),
            "phone_clicks": counts.get("phone_click", 0),
        }


class VersionService:
    # version service class
    @staticmethod
    def create_snapshot(site_id: int, html: str, data: dict, reason: str):
        # create snapshot
        db.create_site_version(site_id, html, data, reason)

    @staticmethod
    def list_versions(user_id: int, site_id: int) -> list[dict]:
        # list versions
        site = db.get_site_by_id(site_id)
        if not site or site["user_id"] != user_id:
            return []
        return db.get_site_versions(site_id)

    @staticmethod
    def restore(user_id: int, site_id: int, version_id: int) -> dict:
        # restore site from version snapshot
        site = SupportService.refresh_site(site_id)
        if not site or site["user_id"] != user_id:
            return {"ok": False, "error": "site_not_found", "message": "Сайт не найден."}
        if not is_support_operational(site.get("support_status")):
            return {"ok": False, "error": "support_inactive", "message": "Сначала оплатите поддержку сайта."}

        with db.get_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            version = _rowdict(c.execute(
                "SELECT * FROM site_versions WHERE id=? AND site_id=?",
                (version_id, site_id),
            ).fetchone())
            if not version:
                return {"ok": False, "error": "version_not_found", "message": "Версия не найдена."}
            spent = c.execute(
                """UPDATE users
                   SET dev_credits=dev_credits-?, tokens=MAX(tokens-?,0)
                   WHERE id=? AND dev_credits>=?""",
                (
                    VERSION_RESTORE_DEV_CREDITS,
                    VERSION_RESTORE_DEV_CREDITS,
                    user_id,
                    VERSION_RESTORE_DEV_CREDITS,
                ),
            )
            if spent.rowcount != 1:
                return {"ok": False, "error": "insufficient_dev_credits", "message": "Недостаточно кредитов разработки."}
            balance = c.execute("SELECT dev_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
            c.execute(
                """INSERT INTO dev_credit_log
                   (user_id, site_id, delta, reason, balance_after, created)
                   VALUES (?,?,?,?,?,datetime('now'))""",
                (user_id, site_id, -VERSION_RESTORE_DEV_CREDITS, f"version_restore:{version_id}", balance),
            )
            c.execute(
                """INSERT INTO token_log (user_id, site_id, delta, reason)
                   VALUES (?,?,?,?)""",
                (user_id, site_id, -VERSION_RESTORE_DEV_CREDITS, f"version_restore:{version_id}"),
            )
            c.execute(
                """UPDATE sites
                   SET data=?, updated=datetime('now')
                   WHERE id=?""",
                (version["data"], site_id),
            )
        CampaignService.site_changed(site_id, "version_restore")
        return {
            "ok": True,
            "html": version["html"],
            "data": json.loads(version["data"] or "{}"),
            "dev_credits": balance,
        }


def build_dashboard_context(user: dict) -> dict:
    # build data context for dashboard
    sites = SupportService.refresh_user_sites(user["id"])
    enriched = []
    for site in sites:
        invoice = SupportService.get_open_invoice(site["id"])
        campaigns = CampaignService.history(site["id"])
        active_campaign = next((c for c in campaigns if c.get("status") == CampaignStatus.ACTIVE.value), None)
        paid_until = _parse_dt(site.get("support_paid_until"))
        support_status = site.get("support_status") or SupportService.compute_status(site)
        site["support_label"] = _status_label(support_status)
        site["promotion_label"] = _status_label(site.get("promo_status") or PromotionStatus.NOT_CONFIGURED.value)
        site["analytics_label"] = _status_label(site.get("analytics_status") or AnalyticsStatus.UNAVAILABLE.value)
        site["support_operational"] = is_support_operational(support_status)
        site["support_public"] = is_support_public(support_status)
        site["support_paid_until_display"] = paid_until.strftime("%d.%m.%Y") if paid_until else "не задана"
        site["support_invoice"] = invoice
        site["campaigns"] = campaigns
        site["active_campaign"] = active_campaign
        site["campaign_history_count"] = len(campaigns)
        site["needs_analytics_restore"] = site.get("analytics_status") == AnalyticsStatus.OUTDATED.value
        enriched.append(site)
    user = db.get_user_by_id(user["id"]) or user
    notifications = NotificationService.for_user(user["id"])
    return {
        "user": user,
        "sites": enriched,
        "notifications": notifications,
        "unread_notifications": sum(1 for n in notifications if not int(n.get("is_read") or 0)),
        "active_onboarding": db.get_active_onboarding_session(user["id"]),
        "promo_setup_cost": PROMO_SETUP_COST,
        "promo_min_purchase": PROMO_MIN_PURCHASE,
        "promo_credit_tenge": PROMO_CREDIT_TENGE,
        "campaign_min_credits": CAMPAIGN_MIN_CREDITS,
        "campaign_min_duration_hours": CAMPAIGN_MIN_DURATION_HOURS,
        "support_monthly_price": SUPPORT_MONTHLY_PRICE,
        "version_restore_dev_credits": VERSION_RESTORE_DEV_CREDITS,
    }


class NotificationService:
    # notification service class
    @staticmethod
    def sync_user(user_id: int):
        # sync user
        sites = db.get_user_sites(user_id)
        existing = db.get_notifications(user_id, 50)
        keys = {(n.get("type"), n.get("site_id")) for n in existing}
        for site in sites:
            status = site.get("support_status")
            if status == SupportStatus.EXPIRING_SOON.value and ("support_expiring", site["id"]) not in keys:
                db.create_notification(
                    user_id,
                    "support_expiring",
                    "Поддержка скоро закончится",
                    f"Страница «{site.get('title') or site.get('slug')}» скоро потребует продления.",
                    site["id"],
                )
            if status == SupportStatus.SUSPENDED.value and ("support_suspended", site["id"]) not in keys:
                db.create_notification(
                    user_id,
                    "support_suspended",
                    "Страница приостановлена",
                    "Правки, продвижение и аналитика заблокированы до продления поддержки.",
                    site["id"],
                )
            if site.get("analytics_status") == AnalyticsStatus.OUTDATED.value and ("analytics_outdated", site["id"]) not in keys:
                db.create_notification(
                    user_id,
                    "analytics_outdated",
                    "Аналитика устарела",
                    "После правок нужно восстановить аналитику перед продвижением.",
                    site["id"],
                )

    @staticmethod
    def for_user(user_id: int) -> list[dict]:
        # for user
        NotificationService.sync_user(user_id)
        return db.get_notifications(user_id)


class OnboardingService:
    # onboarding service class
    REQUIRED_KEYS = ("name", "services", "city", "vibe")
    ALLOWED_STATUSES = {"draft", "ready", "generating", "failed", "completed"}

    @staticmethod
    def _safe_history(value) -> list:
        # safe history
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
        # safe collected
        if not isinstance(value, dict):
            return {}
        return {
            key: str(value.get(key) or "").strip()[:2000]
            for key in OnboardingService.REQUIRED_KEYS
            if value.get(key)
        }

    @staticmethod
    def _safe_photo_urls(value) -> list:
        # safe photo urls
        if not isinstance(value, list):
            return []
        return [
            str(url)[:500]
            for url in value[:12]
            if isinstance(url, str) and url.startswith("/static/uploads/")
        ]

    @staticmethod
    def _safe_status(value) -> str:
        # safe status
        status = str(value or "draft")
        return status if status in OnboardingService.ALLOWED_STATUSES else "draft"

    @staticmethod
    def _safe_int(value) -> int:
        # safe int
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def current(user_id: int) -> dict:
        # current
        session = db.get_active_onboarding_session(user_id) or db.create_onboarding_session(user_id)
        return OnboardingService.present(session)

    @staticmethod
    def present(session: dict | None) -> dict:
        # present
        if not session:
            return {"session": None, "summary": [], "progress": 0, "missing": list(OnboardingService.REQUIRED_KEYS)}
        collected = session.get("collected") or {}
        done = [key for key in OnboardingService.REQUIRED_KEYS if collected.get(key)]
        summary = [
            {"key": "name", "label_key": "create_summary_business", "value": collected.get("name") or ""},
            {"key": "services", "label_key": "create_summary_services", "value": collected.get("services") or ""},
            {"key": "city", "label_key": "create_summary_contacts", "value": collected.get("city") or ""},
            {"key": "vibe", "label_key": "create_summary_style", "value": collected.get("vibe") or ""},
            {
                "key": "photos",
                "label_key": "create_summary_photos",
                "value": f"{len(session.get('photo_urls') or [])} фото" if session.get("photo_urls") else "",
            },
        ]
        return {
            "session": session,
            "summary": summary,
            "progress": int(len(done) / len(OnboardingService.REQUIRED_KEYS) * 100),
            "missing": [key for key in OnboardingService.REQUIRED_KEYS if key not in done],
        }

    @staticmethod
    def autosave(user_id: int, payload: dict) -> dict:
        # autosave
        session = db.upsert_onboarding_session(
            user_id,
            payload.get("session_id"),
            status=OnboardingService._safe_status(payload.get("status")),
            history=OnboardingService._safe_history(payload.get("history")),
            collected=OnboardingService._safe_collected(payload.get("collected")),
            photo_urls=OnboardingService._safe_photo_urls(payload.get("photo_urls")),
            chat_in=OnboardingService._safe_int(payload.get("chat_in")),
            chat_out=OnboardingService._safe_int(payload.get("chat_out")),
            chat_cr=OnboardingService._safe_int(payload.get("chat_cr")),
        )
        return OnboardingService.present(session)

    @staticmethod
    def reset(user_id: int, payload: dict | None = None) -> dict:
        # reset
        payload = payload or {}
        keep_session_ids = payload.get("keep_session_ids") if isinstance(payload.get("keep_session_ids"), list) else None
        session = db.create_onboarding_session(user_id, keep_session_ids=keep_session_ids)
        return OnboardingService.present(session)

    @staticmethod
    def delete(user_id: int, session_id: int) -> dict:
        # delete
        deleted = db.delete_onboarding_session(session_id, user_id)
        return {"deleted": deleted}

    @staticmethod
    def rename(user_id: int, session_id: int, title: str) -> dict:
        # rename
        session = db.rename_onboarding_session(session_id, user_id, title)
        return OnboardingService.present(session)

    @staticmethod
    def reorder(user_id: int, session_ids: list[int]) -> dict:
        # reorder
        db.reorder_onboarding_sessions(user_id, session_ids)
        return {"reordered": True}


def build_site_workspace_context(user: dict, site_id: int) -> dict | None:
    # build data context for site workspace
    site = db.get_user_site_by_id(user["id"], site_id)
    if not site:
        return None
    site = SupportService.refresh_site(site["id"]) or site
    CampaignService.refresh_site_campaigns(site["id"])
    site = db.get_user_site_by_id(user["id"], site_id) or site
    paid_until = _parse_dt(site.get("support_paid_until"))
    campaigns = CampaignService.history(site["id"])
    site["support_label"] = _status_label(site.get("support_status") or "")
    site["promotion_label"] = _status_label(site.get("promo_status") or "")
    site["analytics_label"] = _status_label(site.get("analytics_status") or "")
    site["support_operational"] = is_support_operational(site.get("support_status"))
    site["support_paid_until_display"] = paid_until.strftime("%d.%m.%Y") if paid_until else "не задана"
    site["support_invoice"] = SupportService.get_open_invoice(site["id"])
    site["campaigns"] = campaigns
    site["active_campaign"] = next((c for c in campaigns if c.get("status") == CampaignStatus.ACTIVE.value), None)
    site["needs_analytics_restore"] = site.get("analytics_status") == AnalyticsStatus.OUTDATED.value
    versions = VersionService.list_versions(user["id"], site["id"])
    analytics_metrics = (
        AnalyticsService.metrics(site["id"])
        if site.get("analytics_status") == AnalyticsStatus.ACTIVE.value and int(site.get("promo_setup_done") or 0)
        else {
            "visits": 0,
            "cta_clicks": 0,
            "whatsapp_clicks": 0,
            "telegram_clicks": 0,
            "instagram_clicks": 0,
            "phone_clicks": 0,
        }
    )
    return {
        **build_dashboard_context(user),
        "selected_site": site,
        "versions": versions,
        "analytics_metrics": analytics_metrics,
    }


def maintenance_page() -> str:
    # maintenance page
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
