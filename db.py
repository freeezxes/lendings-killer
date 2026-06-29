import sqlite3, json, re, secrets, hashlib, hmac, time, unicodedata, bcrypt as _bcrypt
from pathlib import Path
from datetime import datetime, timedelta
from domain import (
    ACTIVE_DRAFT_STATUSES,
    AnalyticsStatus,
    DraftValidationError,
    MAX_DRAFTS,
    PromotionStatus,
    SupportStatus,
    SUPPORT_INCLUDED_DAYS,
    is_active_draft_status,
    normalize_draft_title,
)

class _BcryptWrapper:
    # bcrypt wrapper class
    def hash(self, password: str) -> str:
        # hash
        return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=12)).decode()
    def verify(self, password: str, hashed: str) -> bool:
        # verify
        try:
            return _bcrypt.checkpw(password.encode(), hashed.encode())
        except (TypeError, ValueError):
            return False

bcrypt = _BcryptWrapper()

DB_PATH = Path("lendings.db")
ACTIVE_DRAFT_STATUS_SQL = ",".join("?" for _ in ACTIVE_DRAFT_STATUSES)


class DraftLimitError(RuntimeError):
    # draft limit error
    pass


class DraftConflictError(RuntimeError):
    # draft sync conflict error
    pass

def get_conn():
    # get database connection
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    # initialize database schema and apply migrations
    with get_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT UNIQUE,
            password    TEXT,
            password_hash TEXT,
            email       TEXT UNIQUE,
            email_verified INTEGER DEFAULT 0,
            email_verify_token TEXT,
            email_verify_expires INTEGER,
            verification_sent_at INTEGER,
            google_id   TEXT UNIQUE,
            auth_provider TEXT DEFAULT 'local',
            avatar_url  TEXT,
            name        TEXT,
            tokens      INTEGER DEFAULT 0,
            dev_credits INTEGER DEFAULT 0,
            promo_credits INTEGER DEFAULT 0,
            site_slots  INTEGER DEFAULT 0,
            created     TEXT DEFAULT (datetime('now')),
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now')),
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sites (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            slug        TEXT UNIQUE NOT NULL,
            title       TEXT,
            data        TEXT,
            html_path   TEXT,
            tokens_used INTEGER DEFAULT 0,
            support_paid_until TEXT,
            support_status TEXT DEFAULT 'active',
            promo_status TEXT DEFAULT 'not_configured',
            analytics_status TEXT DEFAULT 'unavailable',
            promo_setup_done INTEGER DEFAULT 0,
            chat_in     INTEGER DEFAULT 0,
            chat_out    INTEGER DEFAULT 0,
            gen_in      INTEGER DEFAULT 0,
            gen_out     INTEGER DEFAULT 0,
            cache_read  INTEGER DEFAULT 0,
            cost_usd    REAL DEFAULT 0,
            created     TEXT DEFAULT (datetime('now')),
            updated     TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS token_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER REFERENCES users(id),
            site_id     INTEGER REFERENCES sites(id),
            delta       INTEGER NOT NULL,
            reason      TEXT,
            claude_in   INTEGER DEFAULT 0,
            claude_out  INTEGER DEFAULT 0,
            cache_read  INTEGER DEFAULT 0,
            cost_usd    REAL DEFAULT 0,
            ts          TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS payments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            order_id   TEXT UNIQUE NOT NULL,
            invoice_id TEXT,
            amount     INTEGER NOT NULL,
            tokens     INTEGER NOT NULL,
            payment_kind TEXT DEFAULT 'legacy',
            promo_credits INTEGER DEFAULT 0,
            dev_credits INTEGER DEFAULT 0,
            site_id    INTEGER REFERENCES sites(id),
            support_invoice_id INTEGER,
            status     TEXT DEFAULT 'pending',
            created    TEXT DEFAULT (datetime('now')),
            updated    TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id       TEXT PRIMARY KEY,
            user_id  INTEGER NOT NULL REFERENCES users(id),
            expires  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name        TEXT,
            created     TEXT DEFAULT (datetime('now')),
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS admin_sessions (
            id          TEXT PRIMARY KEY,
            admin_id    INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
            expires     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token       TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at  TEXT NOT NULL,
            used        INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now')),
            used_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS site_versions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            version_no  INTEGER NOT NULL,
            html        TEXT NOT NULL,
            data        TEXT,
            reason      TEXT,
            created     TEXT DEFAULT (datetime('now')),
            UNIQUE(site_id, version_no)
        );

        CREATE TABLE IF NOT EXISTS support_invoices (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            amount      INTEGER NOT NULL,
            months      INTEGER DEFAULT 1,
            status      TEXT NOT NULL DEFAULT 'pending',
            due_at      TEXT,
            paid_at     TEXT,
            order_id    TEXT UNIQUE,
            created     TEXT DEFAULT (datetime('now')),
            updated     TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS dev_credit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            site_id     INTEGER REFERENCES sites(id) ON DELETE SET NULL,
            delta       INTEGER NOT NULL,
            reason      TEXT,
            claude_in   INTEGER DEFAULT 0,
            claude_out  INTEGER DEFAULT 0,
            cache_read  INTEGER DEFAULT 0,
            cost_usd    REAL DEFAULT 0,
            balance_after INTEGER,
            legacy_token_log_id INTEGER UNIQUE,
            created     TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS promo_credit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            site_id     INTEGER REFERENCES sites(id) ON DELETE SET NULL,
            campaign_id INTEGER,
            delta       INTEGER NOT NULL,
            reason      TEXT,
            balance_after INTEGER,
            created     TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS promotion_setups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            credits_spent INTEGER NOT NULL,
            status      TEXT NOT NULL,
            created     TEXT DEFAULT (datetime('now')),
            updated     TEXT DEFAULT (datetime('now')),
            UNIQUE(site_id)
        );

        CREATE TABLE IF NOT EXISTS promotion_campaigns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            credits_spent INTEGER NOT NULL,
            duration_hours INTEGER NOT NULL,
            status      TEXT NOT NULL,
            forecast_json TEXT,
            starts_at   TEXT,
            ends_at     TEXT,
            stopped_reason TEXT,
            created     TEXT DEFAULT (datetime('now')),
            updated     TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS analytics_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            event_type  TEXT NOT NULL,
            payload_json TEXT,
            created     TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS onboarding_sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            draft_title TEXT,
            sort_order  INTEGER DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'draft',
            history     TEXT NOT NULL DEFAULT '[]',
            collected   TEXT NOT NULL DEFAULT '{}',
            photo_urls  TEXT NOT NULL DEFAULT '[]',
            chat_in     INTEGER DEFAULT 0,
            chat_out    INTEGER DEFAULT 0,
            chat_cr     INTEGER DEFAULT 0,
            generation_started_at TEXT,
            generated_site_id INTEGER REFERENCES sites(id) ON DELETE SET NULL,
            error       TEXT,
            created     TEXT DEFAULT (datetime('now')),
            updated     TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            site_id     INTEGER REFERENCES sites(id) ON DELETE CASCADE,
            type        TEXT NOT NULL,
            title       TEXT NOT NULL,
            body        TEXT,
            is_read     INTEGER DEFAULT 0,
            created     TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_support_invoices_site_status ON support_invoices(site_id, status);
        CREATE INDEX IF NOT EXISTS idx_password_reset_user_created ON password_reset_tokens(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_password_reset_expires ON password_reset_tokens(expires_at);
        CREATE INDEX IF NOT EXISTS idx_promotion_campaigns_site_status ON promotion_campaigns(site_id, status);
        CREATE INDEX IF NOT EXISTS idx_dev_credit_log_user_created ON dev_credit_log(user_id, created DESC);
        CREATE INDEX IF NOT EXISTS idx_promo_credit_log_user_created ON promo_credit_log(user_id, created DESC);
        CREATE INDEX IF NOT EXISTS idx_site_versions_site_created ON site_versions(site_id, created DESC);
        CREATE INDEX IF NOT EXISTS idx_onboarding_user_status_updated ON onboarding_sessions(user_id, status, updated DESC);
        CREATE INDEX IF NOT EXISTS idx_notifications_user_read_created ON notifications(user_id, is_read, created DESC);
        CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin_expires ON admin_sessions(admin_id, expires);
        """)
    # migrate users table with named balances
    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        for col, defn in [
            ("site_slots", "INTEGER DEFAULT 0"),
            ("dev_credits", "INTEGER DEFAULT 0"),
            ("promo_credits", "INTEGER DEFAULT 0"),
            ("password_hash", "TEXT"),
            ("created_at", "TEXT"),
            ("updated_at", "TEXT"),
            ("last_login_at", "TEXT"),
        ]:
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {defn}")
        c.execute("UPDATE users SET dev_credits=COALESCE(NULLIF(dev_credits, 0), tokens, 0)")
        c.execute("UPDATE users SET password_hash=password WHERE password_hash IS NULL AND password IS NOT NULL")
        c.execute("UPDATE users SET created_at=COALESCE(created_at, created, datetime('now'))")
        c.execute("UPDATE users SET updated_at=COALESCE(updated_at, created_at, created, datetime('now'))")

    # migrate existing sites table
    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(sites)").fetchall()}
        for col, defn in [
            ("support_paid_until", "TEXT"),
            ("support_status", f"TEXT DEFAULT '{SupportStatus.ACTIVE.value}'"),
            ("promo_status", f"TEXT DEFAULT '{PromotionStatus.NOT_CONFIGURED.value}'"),
            ("analytics_status", f"TEXT DEFAULT '{AnalyticsStatus.UNAVAILABLE.value}'"),
            ("promo_setup_done", "INTEGER DEFAULT 0"),
            ("chat_in",   "INTEGER DEFAULT 0"),
            ("chat_out",  "INTEGER DEFAULT 0"),
            ("gen_in",    "INTEGER DEFAULT 0"),
            ("gen_out",   "INTEGER DEFAULT 0"),
            ("cache_read","INTEGER DEFAULT 0"),
            ("cost_usd",  "REAL DEFAULT 0"),
        ]:
            if col not in cols:
                c.execute(f"ALTER TABLE sites ADD COLUMN {col} {defn}")
        default_paid_until = (datetime.utcnow() + timedelta(days=SUPPORT_INCLUDED_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            """UPDATE sites
               SET support_paid_until=COALESCE(support_paid_until, ?),
                   support_status=COALESCE(NULLIF(support_status, ''), ?),
                   promo_status=COALESCE(NULLIF(promo_status, ''), ?),
                   analytics_status=COALESCE(NULLIF(analytics_status, ''), ?),
                   promo_setup_done=COALESCE(promo_setup_done, 0)""",
            (
                default_paid_until,
                SupportStatus.ACTIVE.value,
                PromotionStatus.NOT_CONFIGURED.value,
                AnalyticsStatus.UNAVAILABLE.value,
            ),
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_sites_user_status ON sites(user_id, support_status, promo_status)"
        )

    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(payments)").fetchall()}
        for col, defn in [
            ("catalog_item_id", "TEXT DEFAULT ''"),
            ("payment_kind", "TEXT DEFAULT 'legacy'"),
            ("promo_credits", "INTEGER DEFAULT 0"),
            ("dev_credits", "INTEGER DEFAULT 0"),
            ("site_id", "INTEGER REFERENCES sites(id)"),
            ("support_invoice_id", "INTEGER"),
        ]:
            if col not in cols:
                c.execute(f"ALTER TABLE payments ADD COLUMN {col} {defn}")

    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(onboarding_sessions)").fetchall()}
        for col, defn in [
            ("draft_title", "TEXT"),
            ("sort_order", "INTEGER DEFAULT 0"),
        ]:
            if col not in cols:
                c.execute(f"ALTER TABLE onboarding_sessions ADD COLUMN {col} {defn}")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_onboarding_user_sort "
            "ON onboarding_sessions(user_id, sort_order, updated DESC)"
        )

    migrate_credit_logs()
    migrate_add_oauth_columns()
    normalize_site_versions()

def _make_user_columns_nullable(columns: list[str]):
    # relax legacy not null constraints
    with get_conn() as c:
        info = {r[1]: r for r in c.execute("PRAGMA table_info(users)").fetchall()}
        targets = [col for col in columns if col in info and info[col][3]]
        if not targets:
            return

        row = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if not row or not row["sql"]:
            raise RuntimeError("Cannot inspect users table schema for OAuth migration")

        new_sql = row["sql"]
        for col in targets:
            new_sql = re.sub(
                rf"(\b{re.escape(col)}\b\s+[^,\n)]+?)\s+NOT\s+NULL",
                r"\1",
                new_sql,
                count=1,
                flags=re.IGNORECASE,
            )

        if new_sql == row["sql"]:
            raise RuntimeError("Cannot relax users table NOT NULL constraint safely")

        try:
            c.execute("PRAGMA writable_schema=ON")
            c.execute(
                "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='users'",
                (new_sql,),
            )
            schema_version = c.execute("PRAGMA schema_version").fetchone()[0]
            c.execute(f"PRAGMA schema_version = {int(schema_version) + 1}")
        finally:
            c.execute("PRAGMA writable_schema=OFF")

    with get_conn() as c:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed after OAuth migration: {integrity}")

def migrate_add_oauth_columns():
    # add google oauth columns without touching rows
    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        for col, defn in [
            ("email", "TEXT"),
            ("email_verified", "INTEGER DEFAULT 0"),
            ("email_verify_token", "TEXT"),
            ("email_verify_expires", "INTEGER"),
            ("verification_sent_at", "INTEGER"),
            ("google_id", "TEXT"),
            ("auth_provider", "TEXT DEFAULT 'local'"),
            ("avatar_url", "TEXT"),
        ]:
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {defn}")

        # use partial unique indexes for sqlite
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique "
            "ON users(email) WHERE email IS NOT NULL"
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id_unique "
            "ON users(google_id) WHERE google_id IS NOT NULL"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_email_verify_token "
            "ON users(email_verify_token) WHERE email_verify_token IS NOT NULL"
        )
        c.execute(
            "UPDATE users SET auth_provider='local' "
            "WHERE auth_provider IS NULL OR auth_provider=''"
        )

    _make_user_columns_nullable(["password", "phone"])

def migrate_credit_logs():
    # copy legacy token_log rows for continuity
    with get_conn() as c:
        c.execute(
            """INSERT OR IGNORE INTO dev_credit_log
               (user_id, site_id, delta, reason, claude_in, claude_out,
                cache_read, cost_usd, legacy_token_log_id, created)
               SELECT user_id, site_id, delta, reason, claude_in, claude_out,
                      cache_read, cost_usd, id, ts
               FROM token_log"""
        )

# users
def _hash_email_verify_token(token: str) -> str:
    # hash email verification token
    return hashlib.sha256(token.encode()).hexdigest()

def generate_email_verify_token() -> str:
    # generate email verify token
    return secrets.token_urlsafe(32)

def create_user(phone: str, password: str, name: str = "", email: str = "") -> dict | None:
    # create new user account
    email = normalize_email(email)
    phone = re.sub(r"[^\d]", "", phone or "") or None
    try:
        hashed = bcrypt.hash(password)
        with get_conn() as c:
            cur = c.execute(
                """INSERT INTO users
                   (phone, password, password_hash, email, email_verified, name,
                    tokens, dev_credits, promo_credits, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,0,0,0,datetime('now'),datetime('now'))""",
                (phone, hashed, hashed, email or None, 0, name)
            )
            row = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row) if row else None
    except sqlite3.IntegrityError:
        return None

def create_user_with_email(email: str, password: str, name: str = "") -> dict | None:
    # create user with email
    return create_user("", password, name, email)

def normalize_email(email: str | None) -> str:
    # normalize email
    return unicodedata.normalize("NFKC", email or "").strip().lower()

def get_user_by_phone(phone: str) -> dict | None:
    # get user by phone
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        return dict(row) if row else None

def get_user_by_email(email: str) -> dict | None:
    # get user by email
    email = normalize_email(email)
    if not email:
        return None
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return dict(row) if row else None

def get_user_by_google_id(google_id: str) -> dict | None:
    # get user by google id
    google_id = (google_id or "").strip()
    if not google_id:
        return None
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE google_id=?", (google_id,)).fetchone()
        return dict(row) if row else None

def get_user_by_id(uid: int) -> dict | None:
    # get user by id
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

def verify_password(phone: str, password: str) -> dict | None:
    # verify user password by phone
    user = get_user_by_phone(phone)
    stored = (user.get("password_hash") or user.get("password")) if user else None
    if user and stored and bcrypt.verify(password, stored):
        return user
    return None

def verify_password_identity(identity: str, password: str) -> dict | None:
    # verify user password by phone or email
    identity = (identity or "").strip()
    user = get_user_by_email(identity) if "@" in identity else get_user_by_phone(re.sub(r"[^\d]", "", identity))
    stored = (user.get("password_hash") or user.get("password")) if user else None
    if user and stored and bcrypt.verify(password, stored):
        return user
    return None

def update_user_password(user_id: int, password: str) -> dict | None:
    # update user password
    hashed = bcrypt.hash(password)
    with get_conn() as c:
        c.execute(
            """UPDATE users
               SET password=?, password_hash=?, auth_provider=CASE
                   WHEN auth_provider='google' THEN 'hybrid'
                   ELSE COALESCE(auth_provider, 'local')
               END,
               updated_at=datetime('now')
               WHERE id=?""",
            (hashed, hashed, user_id),
        )
    return get_user_by_id(user_id)

def mark_user_login(user_id: int):
    # record user login timestamp
    with get_conn() as c:
        c.execute(
            "UPDATE users SET last_login_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
            (user_id,),
        )

def create_password_reset_token(user_id: int, token_hash: str, expires_at: int) -> dict:
    # create password reset token
    expires_text = datetime.utcfromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """UPDATE password_reset_tokens
               SET used=1, used_at=datetime('now')
               WHERE user_id=? AND used=0""",
            (user_id,),
        )
        c.execute(
            """INSERT INTO password_reset_tokens
               (token, user_id, expires_at, used, created_at)
               VALUES (?,?,?,0,datetime('now'))""",
            (token_hash, user_id, expires_text),
        )
        row = c.execute(
            "SELECT * FROM password_reset_tokens WHERE token=?",
            (token_hash,),
        ).fetchone()
        return dict(row)

def get_password_reset_token(token_hash: str) -> dict | None:
    # get password reset token
    with get_conn() as c:
        row = c.execute(
            """SELECT prt.*, u.email, u.name
               FROM password_reset_tokens prt
               JOIN users u ON u.id=prt.user_id
               WHERE prt.token=?""",
            (token_hash,),
        ).fetchone()
        return dict(row) if row else None

def mark_password_reset_token_used(token_hash: str):
    # mark password reset token used
    with get_conn() as c:
        c.execute(
            """UPDATE password_reset_tokens
               SET used=1, used_at=datetime('now')
               WHERE token=?""",
            (token_hash,),
        )

def count_recent_password_resets(user_id: int, since_ts: int) -> int:
    # count recent password resets
    since_text = datetime.utcfromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        return c.execute(
            """SELECT COUNT(*) FROM password_reset_tokens
               WHERE user_id=? AND created_at>=?""",
            (user_id, since_text),
        ).fetchone()[0]

def set_email_verification_token(user_id: int, token: str | None = None,
                                 expires_at: int | None = None,
                                 sent_at: int | None = None) -> str | None:
    # set email verification token
    token = token or generate_email_verify_token()
    token_hash = _hash_email_verify_token(token)
    expires_at = int(expires_at or (time.time() + 3600))
    sent_at = int(sent_at or time.time())
    with get_conn() as c:
        cur = c.execute(
            """UPDATE users
               SET email_verify_token=?, email_verify_expires=?, verification_sent_at=?
               WHERE id=? AND email IS NOT NULL AND COALESCE(email_verified,0)=0""",
            (token_hash, expires_at, sent_at, user_id),
        )
        return token if cur.rowcount else None

def clear_email_verification(user_id: int):
    # clear email verification
    with get_conn() as c:
        c.execute(
            """UPDATE users
               SET email_verify_token=NULL,
                   email_verify_expires=NULL,
                   verification_sent_at=NULL
               WHERE id=?""",
            (user_id,),
        )

def mark_email_verified(user_id: int) -> dict | None:
    # mark email verified
    with get_conn() as c:
        c.execute(
            """UPDATE users
               SET email_verified=1,
                   email_verify_token=NULL,
                   email_verify_expires=NULL,
                   verification_sent_at=NULL
               WHERE id=?""",
            (user_id,),
        )
    return get_user_by_id(user_id)

def verify_email_token(token: str) -> dict:
    # verify email token
    token = (token or "").strip()
    if not token:
        return {"ok": False, "error": "invalid_token"}

    token_hash = _hash_email_verify_token(token)
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE email_verify_token=?",
            (token_hash,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "invalid_token"}

        user = dict(row)
        stored = user.get("email_verify_token") or ""
        if not hmac.compare_digest(stored, token_hash):
            return {"ok": False, "error": "invalid_token"}

        expires_at = int(user.get("email_verify_expires") or 0)
        if expires_at < int(time.time()):
            clear_email_verification(user["id"])
            return {"ok": False, "error": "expired_token", "user": user}

        return {"ok": True, "user": user}

def resend_verification_email(user_id: int, cooldown_seconds: int = 60,
                              expires_seconds: int = 3600) -> dict:
    # resend verification email
    now = int(time.time())
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "email_not_found"}

        user = dict(row)
        if not user.get("email"):
            return {"ok": False, "error": "email_not_found", "user": user}
        if int(user.get("email_verified") or 0):
            return {"ok": False, "error": "email_already_verified", "user": user}

        sent_at = int(user.get("verification_sent_at") or 0)
        if sent_at and now - sent_at < cooldown_seconds:
            return {
                "ok": False,
                "error": "resend_cooldown",
                "retry_after": cooldown_seconds - (now - sent_at),
                "user": user,
            }

    token = set_email_verification_token(
        user_id,
        expires_at=now + expires_seconds,
        sent_at=now,
    )
    if not token:
        return {"ok": False, "error": "verification_failed"}

    user = get_user_by_id(user_id)
    return {
        "ok": True,
        "token": token,
        "expires_at": now + expires_seconds,
        "retry_after": cooldown_seconds,
        "user": user,
    }

def update_user_email_for_verification(user_id: int, email: str) -> dict | None:
    # update user email for verification
    email = normalize_email(email)
    if not email:
        return None
    try:
        with get_conn() as c:
            c.execute(
                """UPDATE users
                   SET email=?,
                       email_verified=0,
                       email_verify_token=NULL,
                       email_verify_expires=NULL,
                       verification_sent_at=NULL
                   WHERE id=?""",
                (email, user_id),
            )
        return get_user_by_id(user_id)
    except sqlite3.IntegrityError:
        return None

def create_google_user(email: str, google_id: str, name: str = "", avatar_url: str = "",
                       email_verified: bool = True) -> dict | None:
    # create new user via google oauth
    email = normalize_email(email)
    google_id = (google_id or "").strip()
    if not email or not google_id:
        return None

    display_name = (name or "").strip() or email.split("@", 1)[0]
    try:
        with get_conn() as c:
            cur = c.execute(
                """INSERT INTO users
                   (phone, password, password_hash, email, email_verified, google_id,
                    auth_provider, avatar_url, name, tokens, dev_credits, promo_credits,
                    created_at, updated_at, last_login_at)
                   VALUES (NULL, NULL, NULL, ?, ?, ?, 'google', ?, ?, 0, 0, 0,
                           datetime('now'), datetime('now'), datetime('now'))""",
                (email, 1 if email_verified else 0, google_id, avatar_url or "", display_name),
            )
            row = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row) if row else None
    except sqlite3.IntegrityError:
        return None

def link_google_to_existing_user(user_id: int, email: str, google_id: str, avatar_url: str = "",
                                 email_verified: bool = True) -> dict | None:
    # link google account to existing user
    email = normalize_email(email)
    google_id = (google_id or "").strip()
    if not user_id or not email or not google_id:
        return None

    try:
        with get_conn() as c:
            user = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                return None

            conflict = c.execute(
                "SELECT id FROM users WHERE google_id=? AND id<>?",
                (google_id, user_id),
            ).fetchone()
            if conflict:
                return None

            conflict = c.execute(
                "SELECT id FROM users WHERE email=? AND id<>?",
                (email, user_id),
            ).fetchone()
            if conflict:
                return None

            current_provider = user["auth_provider"] or "local"
            if user["password"]:
                auth_provider = "hybrid" if current_provider == "local" else current_provider
            else:
                auth_provider = "google"

            c.execute(
                """UPDATE users
                   SET email=?,
                       email_verified=?,
                       email_verify_token=NULL,
                       email_verify_expires=NULL,
                       verification_sent_at=NULL,
                       google_id=?,
                       avatar_url=?,
                       auth_provider=?,
                       updated_at=datetime('now')
                   WHERE id=?""",
                (
                    email,
                    1 if email_verified else int(user["email_verified"] or 0),
                    google_id,
                    avatar_url or user["avatar_url"] or "",
                    auth_provider,
                    user_id,
                ),
            )
        return get_user_by_id(user_id)
    except sqlite3.IntegrityError:
        return None

def deduct_tokens(user_id: int, amount: int, reason: str, site_id=None,
                  claude_in=0, claude_out=0, cache_read=0, cost_usd=0.0) -> bool:
    # deduct credits from user balance
    with get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        user = c.execute("SELECT dev_credits FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["dev_credits"] < amount:
            return False
        c.execute("UPDATE users SET tokens=MAX(tokens-?,0), dev_credits=dev_credits-? WHERE id=?", (amount, amount, user_id))
        legacy = c.execute(
            "INSERT INTO token_log (user_id,site_id,delta,reason,claude_in,claude_out,cache_read,cost_usd) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, site_id, -amount, reason, claude_in, claude_out, cache_read, cost_usd)
        )
        balance = c.execute("SELECT dev_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
        c.execute(
            """INSERT INTO dev_credit_log
               (user_id,site_id,delta,reason,claude_in,claude_out,cache_read,cost_usd,balance_after,legacy_token_log_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user_id, site_id, -amount, reason, claude_in, claude_out, cache_read, cost_usd, balance, legacy.lastrowid),
        )
        return True

def add_tokens(user_id: int, amount: int, reason: str):
    # add tokens
    with get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE users SET tokens=tokens+?, dev_credits=dev_credits+? WHERE id=?", (amount, amount, user_id))
        legacy = c.execute("INSERT INTO token_log (user_id,delta,reason) VALUES (?,?,?)",
                           (user_id, amount, reason))
        balance = c.execute("SELECT dev_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
        c.execute(
            "INSERT INTO dev_credit_log (user_id,delta,reason,balance_after,legacy_token_log_id) VALUES (?,?,?,?,?)",
            (user_id, amount, reason, balance, legacy.lastrowid),
        )

def add_site_slots_only(user_id: int, amount: int, reason: str):
    # explicitly add site slots (admin)
    with get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE users SET site_slots=site_slots+? WHERE id=?", (amount, user_id))
        c.execute("INSERT INTO token_log (user_id,delta,reason) VALUES (?,?,?)", (user_id, 0, reason))

def add_site_slot(user_id: int, credits: int, reason: str):
    # give user +1 site slot and credits
    with get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE users SET site_slots=site_slots+1, tokens=tokens+?, dev_credits=dev_credits+? WHERE id=?",
                  (credits, credits, user_id))
        legacy = c.execute("INSERT INTO token_log (user_id,delta,reason) VALUES (?,?,?)",
                           (user_id, credits, reason))
        balance = c.execute("SELECT dev_credits FROM users WHERE id=?", (user_id,)).fetchone()[0]
        c.execute(
            "INSERT INTO dev_credit_log (user_id,delta,reason,balance_after,legacy_token_log_id) VALUES (?,?,?,?,?)",
            (user_id, credits, reason, balance, legacy.lastrowid),
        )

# sites
def create_site(user_id: int, slug: str, title: str, data: dict, html_path: str,
                tokens_used: int, chat_in: int = 0, chat_out: int = 0,
                gen_in: int = 0, gen_out: int = 0,
                cache_read: int = 0, cost_usd: float = 0.0) -> dict:
    # create new site record
    support_paid_until = (datetime.utcnow() + timedelta(days=SUPPORT_INCLUDED_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO sites
               (user_id,slug,title,data,html_path,tokens_used,
                support_paid_until,support_status,promo_status,analytics_status,promo_setup_done,
                chat_in,chat_out,gen_in,gen_out,cache_read,cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, slug, title, json.dumps(data, ensure_ascii=False), html_path, tokens_used,
             support_paid_until, SupportStatus.ACTIVE.value, PromotionStatus.NOT_CONFIGURED.value,
             AnalyticsStatus.UNAVAILABLE.value, 0,
             chat_in, chat_out, gen_in, gen_out, cache_read, cost_usd)
        )
        site_id = cur.lastrowid
    return get_site_by_id(site_id)

def get_site_by_id(sid: int) -> dict | None:
    # get site by id
    with get_conn() as c:
        row = c.execute("SELECT * FROM sites WHERE id=?", (sid,)).fetchone()
        if not row: return None
        d = dict(row)
        d["data"] = json.loads(d["data"] or "{}")
        return d

def get_user_site_by_id(user_id: int, sid: int) -> dict | None:
    # get user site by id
    with get_conn() as c:
        row = c.execute("SELECT * FROM sites WHERE id=? AND user_id=?", (sid, user_id)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["data"] = json.loads(d["data"] or "{}")
        return d

def get_site_by_slug(slug: str) -> dict | None:
    # retrieve site by url slug
    with get_conn() as c:
        row = c.execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()
        if not row: return None
        d = dict(row)
        d["data"] = json.loads(d["data"] or "{}")
        return d

def get_user_sites(user_id: int) -> list:
    # retrieve all sites for user
    with get_conn() as c:
        rows = c.execute("SELECT * FROM sites WHERE user_id=? ORDER BY created DESC", (user_id,)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["data"] = json.loads(d["data"] or "{}")
            result.append(d)
        return result

def get_user_sites_count(user_id: int) -> int:
    # retrieve site count for user
    with get_conn() as c:
        row = c.execute("SELECT COUNT(*) FROM sites WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else 0

def update_site_html(site_id: int, html_path: str, tokens_used: int):
    # update site html
    with get_conn() as c:
        c.execute("UPDATE sites SET html_path=?,tokens_used=?,updated=datetime('now') WHERE id=?",
                  (html_path, tokens_used, site_id))

def update_site_data(site_id: int, data: dict):
    # update site data
    with get_conn() as c:
        c.execute("UPDATE sites SET data=?,updated=datetime('now') WHERE id=?",
                  (json.dumps(data, ensure_ascii=False), site_id))

def delete_site(site_id: int, user_id: int) -> bool:
    # delete site
    with get_conn() as c:
        c.execute("UPDATE payments SET site_id=NULL WHERE site_id=?", (site_id,))
        cur = c.execute("DELETE FROM sites WHERE id=? AND user_id=?", (site_id, user_id))
        return cur.rowcount > 0

def get_token_log(user_id: int, limit: int = 20) -> list:
    # get token log
    return get_dev_credit_log(user_id, limit)

def get_dev_credit_log(user_id: int, limit: int = 20) -> list:
    # get dev credit log
    with get_conn() as c:
        rows = c.execute(
            """SELECT delta, reason, cost_usd, created as ts
               FROM dev_credit_log
               WHERE user_id=?
               ORDER BY created DESC LIMIT ?""",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

def get_promo_credit_log(user_id: int, limit: int = 20) -> list:
    # get promo credit log
    with get_conn() as c:
        rows = c.execute(
            """SELECT delta, reason, balance_after, created as ts
               FROM promo_credit_log
               WHERE user_id=?
               ORDER BY created DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

def _canonical_version_data(data) -> str:
    # stable JSON lets us detect duplicate version states even if key order changed
    if isinstance(data, str):
        try:
            data = json.loads(data or "{}")
        except json.JSONDecodeError:
            data = {}
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_site_versions():
    # remove duplicated version states and keep version numbers compact per site
    with get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        site_ids = [r[0] for r in c.execute("SELECT DISTINCT site_id FROM site_versions").fetchall()]
        for site_id in site_ids:
            rows = c.execute(
                """SELECT id, html, data, version_no
                   FROM site_versions
                   WHERE site_id=?
                   ORDER BY version_no ASC, id ASC""",
                (site_id,),
            ).fetchall()
            seen = {}
            delete_ids = []
            keep_ids = []
            for row in rows:
                key = row["html"]
                data_json = _canonical_version_data(row["data"])
                if key in seen:
                    keep = seen[key]
                    if len(data_json) > keep["data_len"]:
                        c.execute("UPDATE site_versions SET data=? WHERE id=?", (data_json, keep["id"]))
                        keep["data_len"] = len(data_json)
                    delete_ids.append(row["id"])
                    continue
                seen[key] = {"id": row["id"], "data_len": len(data_json)}
                keep_ids.append(row["id"])

            if delete_ids:
                placeholders = ",".join("?" for _ in delete_ids)
                c.execute(f"DELETE FROM site_versions WHERE id IN ({placeholders})", delete_ids)

            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                c.execute(
                    f"UPDATE site_versions SET version_no=-id WHERE id IN ({placeholders})",
                    keep_ids,
                )
                for version_no, version_id in enumerate(keep_ids, start=1):
                    c.execute(
                        "UPDATE site_versions SET version_no=? WHERE id=?",
                        (version_no, version_id),
                    )


def create_site_version(site_id: int, html: str, data: dict, reason: str):
    # create site version
    data_json = _canonical_version_data(data)
    with get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        existing_rows = c.execute(
            "SELECT id, html, data FROM site_versions WHERE site_id=?",
            (site_id,),
        ).fetchall()
        for row in existing_rows:
            if row["html"] == html:
                if len(data_json) > len(_canonical_version_data(row["data"])):
                    c.execute("UPDATE site_versions SET data=? WHERE id=?", (data_json, row["id"]))
                return row["id"]

        version_no = c.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 FROM site_versions WHERE site_id=?",
            (site_id,),
        ).fetchone()[0]
        cur = c.execute(
            """INSERT INTO site_versions (site_id, version_no, html, data, reason)
               VALUES (?,?,?,?,?)""",
            (site_id, version_no, html, data_json, reason),
        )
        c.execute(
            """DELETE FROM site_versions
               WHERE site_id=? AND id NOT IN (
                   SELECT id FROM site_versions
                   WHERE site_id=?
                   ORDER BY version_no DESC
                   LIMIT 10
               )""",
            (site_id, site_id),
        )
        return cur.lastrowid

def get_site_versions(site_id: int, limit: int = 10) -> list:
    # get site versions
    with get_conn() as c:
        rows = c.execute(
            """SELECT id, site_id, version_no, reason, created
               FROM site_versions
               WHERE site_id=?
               ORDER BY version_no DESC LIMIT ?""",
            (site_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

# onboarding sessions
def _json_load(value: str, fallback):
    # json load
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback

def _present_onboarding_session(row) -> dict | None:
    # present onboarding session
    if not row:
        return None
    d = dict(row)
    d["history"] = _json_load(d.get("history"), [])
    d["collected"] = _json_load(d.get("collected"), {})
    d["photo_urls"] = _json_load(d.get("photo_urls"), [])
    return d

def get_active_onboarding_session(user_id: int) -> dict | None:
    # get active onboarding session
    with get_conn() as c:
        row = c.execute(
            """SELECT * FROM onboarding_sessions
               WHERE user_id=? AND status IN ('draft','ready','generating','failed')
               ORDER BY sort_order ASC, updated DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        return _present_onboarding_session(row)

def count_active_onboarding_sessions(user_id: int, conn=None) -> int:
    # count active onboarding sessions
    c = conn or get_conn()
    try:
        return c.execute(
            f"""SELECT COUNT(*) FROM onboarding_sessions
                WHERE user_id=? AND status IN ({ACTIVE_DRAFT_STATUS_SQL})""",
            (user_id, *ACTIVE_DRAFT_STATUSES),
        ).fetchone()[0]
    finally:
        if conn is None:
            c.close()

def get_onboarding_session(session_id: int, user_id: int) -> dict | None:
    # get onboarding session
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM onboarding_sessions WHERE id=? AND user_id=?",
            (session_id, user_id),
        ).fetchone()
        return _present_onboarding_session(row)

def create_onboarding_session(user_id: int) -> dict:
    # create onboarding session
    with get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        active_count = count_active_onboarding_sessions(user_id, c)
        if active_count >= MAX_DRAFTS:
            raise DraftLimitError("Draft limit reached")
        sort_order = c.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM onboarding_sessions WHERE user_id=?",
            (user_id,),
        ).fetchone()[0]
        cur = c.execute(
            """INSERT INTO onboarding_sessions
               (user_id, sort_order, status, history, collected, photo_urls, created, updated)
               VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (user_id, int(sort_order or 0), "draft", "[]", "{}", "[]"),
        )
    return get_onboarding_session(cur.lastrowid, user_id)

def upsert_onboarding_session(
    user_id: int,
    session_id: int | None = None,
    *,
    status: str = "draft",
    history: list | None = None,
    collected: dict | None = None,
    photo_urls: list | None = None,
    chat_in: int = 0,
    chat_out: int = 0,
    chat_cr: int = 0,
    error: str | None = None,
) -> dict:
    # upsert onboarding session
    current = get_onboarding_session(session_id, user_id) if session_id else get_active_onboarding_session(user_id)
    if session_id and not current:
        raise DraftConflictError("Draft not found")
    if not current:
        current = create_onboarding_session(user_id)
    with get_conn() as c:
        c.execute(
            """UPDATE onboarding_sessions
               SET status=?,
                   history=?,
                   collected=?,
                   photo_urls=?,
                   chat_in=?,
                   chat_out=?,
                   chat_cr=?,
                   error=?,
                   updated=datetime('now')
               WHERE id=? AND user_id=?""",
            (
                status,
                json.dumps(history if history is not None else current.get("history", []), ensure_ascii=False),
                json.dumps(collected if collected is not None else current.get("collected", {}), ensure_ascii=False),
                json.dumps(photo_urls if photo_urls is not None else current.get("photo_urls", []), ensure_ascii=False),
                int(chat_in if chat_in is not None else current.get("chat_in") or 0),
                int(chat_out if chat_out is not None else current.get("chat_out") or 0),
                int(chat_cr if chat_cr is not None else current.get("chat_cr") or 0),
                error,
                current["id"],
                user_id,
            ),
        )
    return get_onboarding_session(current["id"], user_id)

def delete_onboarding_session(session_id: int, user_id: int) -> bool:
    # delete onboarding session
    with get_conn() as c:
        cur = c.execute(
            """DELETE FROM onboarding_sessions
               WHERE id=? AND user_id=? AND status IN ('draft','ready','failed')""",
            (session_id, user_id),
        )
        return cur.rowcount == 1

def rename_onboarding_session(session_id: int, user_id: int, title: str) -> dict:
    # rename onboarding session
    normalized = normalize_draft_title(title)
    with get_conn() as c:
        cur = c.execute(
            """UPDATE onboarding_sessions
               SET draft_title=?, updated=datetime('now')
               WHERE id=? AND user_id=? AND status IN ('draft','ready','failed')""",
            (normalized, session_id, user_id),
        )
        if cur.rowcount != 1:
            raise DraftConflictError("Draft not found")
    return get_onboarding_session(session_id, user_id)

def reorder_onboarding_sessions(user_id: int, session_ids: list[int]) -> None:
    # reorder onboarding sessions
    clean_ids = []
    seen = set()
    for raw_id in session_ids[:MAX_DRAFTS]:
        try:
            sid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if sid > 0 and sid not in seen:
            clean_ids.append(sid)
            seen.add(sid)
    if not clean_ids:
        return
    with get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute(
            f"""SELECT id FROM onboarding_sessions
                WHERE user_id=? AND status IN ({ACTIVE_DRAFT_STATUS_SQL})""",
            (user_id, *ACTIVE_DRAFT_STATUSES),
        ).fetchall()
        owned = {int(r["id"]) for r in rows}
        for idx, sid in enumerate(clean_ids):
            if sid in owned:
                c.execute(
                    "UPDATE onboarding_sessions SET sort_order=?, updated=datetime('now') WHERE id=? AND user_id=?",
                    (idx, sid, user_id),
                )

def mark_onboarding_generating(session_id: int, user_id: int) -> bool:
    # mark onboarding generating
    with get_conn() as c:
        cur = c.execute(
            """UPDATE onboarding_sessions
               SET status='generating', generation_started_at=datetime('now'), error=NULL, updated=datetime('now')
               WHERE id=? AND user_id=? AND status IN ('draft','ready','failed','generating')""",
            (session_id, user_id),
        )
        return cur.rowcount == 1

def complete_onboarding_session(session_id: int, user_id: int, site_id: int):
    # complete onboarding session
    with get_conn() as c:
        c.execute(
            """UPDATE onboarding_sessions
               SET status='completed', generated_site_id=?, updated=datetime('now')
               WHERE id=? AND user_id=?""",
            (site_id, session_id, user_id),
        )

def fail_onboarding_session(session_id: int, user_id: int, error: str):
    # fail onboarding session
    with get_conn() as c:
        c.execute(
            """UPDATE onboarding_sessions
               SET status='failed', error=?, updated=datetime('now')
               WHERE id=? AND user_id=?""",
            (error[:500], session_id, user_id),
        )

# notifications
def create_notification(user_id: int, type_: str, title: str, body: str = "", site_id: int | None = None):
    # create notification
    with get_conn() as c:
        c.execute(
            """INSERT INTO notifications (user_id, site_id, type, title, body, created)
               VALUES (?,?,?,?,?,datetime('now'))""",
            (user_id, site_id, type_, title, body),
        )

def get_notifications(user_id: int, limit: int = 12) -> list[dict]:
    # get notifications
    with get_conn() as c:
        rows = c.execute(
            """SELECT * FROM notifications
               WHERE user_id=?
               ORDER BY created DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

def unread_notification_count(user_id: int) -> int:
    # unread notification count
    with get_conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
            (user_id,),
        ).fetchone()[0]

def update_user_name(user_id: int, name: str):
    # update user name
    with get_conn() as c:
        c.execute("UPDATE users SET name=? WHERE id=?", (name.strip(), user_id))

def update_user_avatar(user_id: int, avatar_url: str):
    # update user avatar
    with get_conn() as c:
        c.execute("UPDATE users SET avatar_url=? WHERE id=?", (avatar_url.strip(), user_id))

# sessions
import uuid as _uuid

def create_session(user_id: int) -> str:
    # create session
    sid = _uuid.uuid4().hex
    expires = datetime.utcnow().replace(year=datetime.utcnow().year + 1).isoformat()
    with get_conn() as c:
        c.execute("INSERT INTO sessions VALUES (?,?,?)", (sid, user_id, expires))
    return sid

def get_session_user(sid: str) -> dict | None:
    # get session user
    with get_conn() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.expires > datetime('now')",
            (sid,)
        ).fetchone()
        return dict(row) if row else None

def delete_session(sid: str):
    # delete session
    with get_conn() as c:
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))

# admin auth
def admin_count() -> int:
    # count registered admin accounts
    with get_conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0])

def create_admin_user(email: str, password: str, name: str = "") -> dict | None:
    # create separate admin account
    email = normalize_email(email)
    if not email or not password:
        return None
    try:
        hashed = bcrypt.hash(password)
        with get_conn() as c:
            cur = c.execute(
                """INSERT INTO admin_users (email, password_hash, name, created)
                   VALUES (?,?,?,datetime('now'))""",
                (email, hashed, (name or "").strip() or email),
            )
            row = c.execute(
                "SELECT id,email,name,created,last_login_at FROM admin_users WHERE id=?",
                (cur.lastrowid,),
            ).fetchone()
            return dict(row) if row else None
    except sqlite3.IntegrityError:
        return None

def get_admin_by_email(email: str) -> dict | None:
    # get admin by email
    email = normalize_email(email)
    if not email:
        return None
    with get_conn() as c:
        row = c.execute("SELECT * FROM admin_users WHERE email=?", (email,)).fetchone()
        return dict(row) if row else None

def get_admin_by_id(admin_id: int) -> dict | None:
    # get admin by id
    with get_conn() as c:
        row = c.execute(
            "SELECT id,email,name,created,last_login_at FROM admin_users WHERE id=?",
            (admin_id,),
        ).fetchone()
        return dict(row) if row else None

def verify_admin_password(email: str, password: str) -> dict | None:
    # verify separate admin credentials
    admin = get_admin_by_email(email)
    if admin and bcrypt.verify(password or "", admin.get("password_hash") or ""):
        mark_admin_login(admin["id"])
        return get_admin_by_id(admin["id"])
    return None

def mark_admin_login(admin_id: int):
    # update admin login timestamp
    with get_conn() as c:
        c.execute("UPDATE admin_users SET last_login_at=datetime('now') WHERE id=?", (admin_id,))

def create_admin_session(admin_id: int) -> str:
    # create separate admin session
    sid = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        c.execute(
            "INSERT INTO admin_sessions (id, admin_id, expires) VALUES (?,?,?)",
            (sid, admin_id, expires),
        )
    return sid

def get_admin_by_session(sid: str | None) -> dict | None:
    # resolve admin session
    if not sid:
        return None
    with get_conn() as c:
        row = c.execute(
            """SELECT a.id,a.email,a.name,a.created,a.last_login_at
               FROM admin_sessions s
               JOIN admin_users a ON a.id=s.admin_id
               WHERE s.id=? AND s.expires > datetime('now')""",
            (sid,),
        ).fetchone()
        return dict(row) if row else None

def delete_admin_session(sid: str | None):
    # delete admin session
    if sid:
        with get_conn() as c:
            c.execute("DELETE FROM admin_sessions WHERE id=?", (sid,))

# admin stats
def admin_stats() -> dict:
    # get admin statistics
    with get_conn() as c:
        users      = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        sites      = c.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        total_cost = c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM token_log").fetchone()[0]
        total_tok  = c.execute("SELECT COALESCE(SUM(claude_in+claude_out),0) FROM token_log").fetchone()[0]
        paid_count = c.execute("SELECT COUNT(*) FROM payments WHERE status='paid'").fetchone()[0]
        paid_sum   = c.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid'").fetchone()[0]
        recent     = c.execute("""
            SELECT u.id as user_id, u.phone, u.email, u.name, s.title, s.slug, s.tokens_used, s.created
            FROM sites s JOIN users u ON u.id=s.user_id
            ORDER BY s.created DESC LIMIT 20
        """).fetchall()
        return {
            "users": users, "sites": sites,
            "total_cost": total_cost, "total_tokens": total_tok,
            "paid_count": paid_count, "paid_sum": paid_sum,
            "recent_sites": [dict(r) for r in recent],
        }

def admin_users() -> list:
    # admin users
    with get_conn() as c:
        rows = c.execute("""
            SELECT u.id, u.phone, u.email, u.auth_provider, u.name,
                   u.tokens, u.dev_credits, u.promo_credits, u.created,
                   COUNT(DISTINCT s.id) as sites_count,
                   COALESCE(SUM(CASE WHEN p.status='paid' THEN p.amount ELSE 0 END), 0) as paid_total
            FROM users u
            LEFT JOIN sites s ON s.user_id = u.id
            LEFT JOIN payments p ON p.user_id = u.id
            GROUP BY u.id
            ORDER BY u.created DESC
        """).fetchall()
        return [dict(r) for r in rows]

def admin_user_detail(user_id: int) -> dict | None:
    # admin user detail
    with get_conn() as c:
        user = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return None
        sites = c.execute(
            """SELECT id, slug, title, tokens_used,
                      chat_in, chat_out, gen_in, gen_out, cache_read, cost_usd,
                      created
               FROM sites WHERE user_id=? ORDER BY created DESC""",
            (user_id,)
        ).fetchall()
        token_log = c.execute(
            "SELECT delta, reason, cost_usd, claude_in, claude_out, ts FROM token_log WHERE user_id=? ORDER BY ts DESC LIMIT 50",
            (user_id,)
        ).fetchall()
        payments = c.execute(
            """SELECT order_id, invoice_id, amount, tokens, dev_credits, promo_credits,
                      payment_kind, status, created
               FROM payments WHERE user_id=? ORDER BY created DESC""",
            (user_id,)
        ).fetchall()
        return {
            "user": dict(user),
            "sites": [dict(r) for r in sites],
            "token_log": [dict(r) for r in token_log],
            "payments": [dict(r) for r in payments],
        }

# payments
def create_payment(user_id: int, order_id: str, invoice_id: str, amount: int, tokens: int,
                   status: str = "pending", catalog_item_id: str = "",
                   payment_kind: str = "dev_credits", dev_credits: int | None = None,
                   promo_credits: int = 0, site_id: int | None = None,
                   support_invoice_id: int | None = None) -> dict:
    # create new payment record
    with get_conn() as c:
        # add catalog_item_id column
        cols = {r[1] for r in c.execute("PRAGMA table_info(payments)").fetchall()}
        if "catalog_item_id" not in cols:
            c.execute("ALTER TABLE payments ADD COLUMN catalog_item_id TEXT DEFAULT ''")
        cur = c.execute(
            """INSERT INTO payments
               (user_id, order_id, invoice_id, amount, tokens, status,
                catalog_item_id, payment_kind, dev_credits, promo_credits, site_id, support_invoice_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                order_id,
                invoice_id,
                amount,
                tokens,
                status,
                catalog_item_id,
                payment_kind,
                tokens if dev_credits is None else dev_credits,
                promo_credits,
                site_id,
                support_invoice_id,
            )
        )
        row = c.execute("SELECT * FROM payments WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

def get_payment_by_order(order_id: str) -> dict | None:
    # get payment by order
    with get_conn() as c:
        row = c.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()
        return dict(row) if row else None

def complete_payment(payment_id: int):
    # complete payment
    with get_conn() as c:
        c.execute("UPDATE payments SET status='paid', updated=datetime('now') WHERE id=?", (payment_id,))

def fail_payment(payment_id: int, reason: str = "failed"):
    # fail payment
    with get_conn() as c:
        c.execute("UPDATE payments SET status=?, updated=datetime('now') WHERE id=?", (reason, payment_id))


init_db()
