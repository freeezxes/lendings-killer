import db


def balance(user_id: int) -> int:
    # marketing credit balance
    user = db.get_user_by_id(user_id)
    return int((user or {}).get("promo_credits") or 0)


def add_credits(user_id: int, credits: int, reason: str, site_id: int | None = None,
                campaign_id: int | None = None) -> dict:
    # add marketing credits
    credits = int(credits or 0)
    if credits <= 0:
        return {"ok": False, "error": "invalid_amount", "message": "Введите количество кредитов."}
    with db.get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE users SET promo_credits=promo_credits+? WHERE id=?", (credits, user_id))
        balance_after = c.execute("SELECT promo_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
        legacy = c.execute(
            """INSERT INTO promo_credit_log
               (user_id, site_id, campaign_id, delta, reason, balance_after, created)
               VALUES (?,?,?,?,?,?,datetime('now'))""",
            (user_id, site_id, campaign_id, credits, reason, balance_after),
        )
        c.execute(
            """INSERT OR IGNORE INTO marketing_credit_logs
               (user_id, site_id, campaign_id, delta, reason, balance_after,
                legacy_promo_credit_log_id, created)
               VALUES (?,?,?,?,?,?,?,datetime('now'))""",
            (user_id, site_id, campaign_id, credits, reason, balance_after, legacy.lastrowid),
        )
    return {"ok": True, "credits": credits, "balance": balance_after}


def deduct_credits(user_id: int, amount: int, reason: str, site_id: int | None = None,
                   campaign_id: int | None = None, content_id: int | None = None,
                   claude_in: int = 0, claude_out: int = 0, cache_read: int = 0,
                   cost_usd: float = 0.0) -> dict:
    # deduct marketing credits
    amount = int(amount or 0)
    if amount <= 0:
        return {"ok": False, "error": "invalid_amount", "message": "Введите количество кредитов."}
    with db.get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        updated = c.execute(
            """UPDATE users
               SET promo_credits=promo_credits-?
               WHERE id=? AND promo_credits>=?""",
            (amount, user_id, amount),
        )
        if updated.rowcount != 1:
            return {
                "ok": False,
                "error": "insufficient_marketing_credits",
                "message": "Недостаточно маркетинговых кредитов.",
            }
        balance_after = c.execute("SELECT promo_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
        legacy = c.execute(
            """INSERT INTO promo_credit_log
               (user_id, site_id, campaign_id, delta, reason, balance_after, created)
               VALUES (?,?,?,?,?,?,datetime('now'))""",
            (user_id, site_id, campaign_id, -amount, reason, balance_after),
        )
        c.execute(
            """INSERT INTO marketing_credit_logs
               (user_id, site_id, campaign_id, content_id, delta, reason, balance_after,
                claude_in, claude_out, cache_read, cost_usd, legacy_promo_credit_log_id, created)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (
                user_id,
                site_id,
                campaign_id,
                content_id,
                -amount,
                reason,
                balance_after,
                int(claude_in or 0),
                int(claude_out or 0),
                int(cache_read or 0),
                float(cost_usd or 0.0),
                legacy.lastrowid,
            ),
        )
    return {"ok": True, "spent": amount, "balance": balance_after}


def apply_promo_payment(payment: dict) -> dict:
    # apply paid marketing credit payment
    credits = int(payment.get("promo_credits") or 0)
    user_id = int(payment.get("user_id") or 0)
    if credits <= 0 or user_id <= 0:
        return {"ok": False, "error": "invalid_promo_payment"}
    return add_credits(user_id, credits, f"promo_credit_purchase:{payment.get('order_id')}")


def logs(user_id: int, limit: int = 50) -> list[dict]:
    # marketing credit logs
    return db.get_marketing_credit_log(user_id, limit)
