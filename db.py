import sqlite3, json, re, secrets, hashlib, hmac, time, bcrypt as _bcrypt
from pathlib import Path
from datetime import datetime

class _BcryptWrapper:
    def hash(self, password: str) -> str:
        return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
    def verify(self, password: str, hashed: str) -> bool:
        return _bcrypt.checkpw(password.encode(), hashed.encode())

bcrypt = _BcryptWrapper()

DB_PATH = Path("lendings.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with get_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT UNIQUE,
            password    TEXT,
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
            site_slots  INTEGER DEFAULT 0,
            created     TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sites (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            slug        TEXT UNIQUE NOT NULL,
            title       TEXT,
            data        TEXT,
            html_path   TEXT,
            tokens_used INTEGER DEFAULT 0,
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
            status     TEXT DEFAULT 'pending',
            created    TEXT DEFAULT (datetime('now')),
            updated    TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id       TEXT PRIMARY KEY,
            user_id  INTEGER NOT NULL REFERENCES users(id),
            expires  TEXT NOT NULL
        );
        """)
    # Migrate users table — add site_slots if missing
    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "site_slots" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN site_slots INTEGER DEFAULT 0")

    # Migrate existing sites table — add new columns if missing
    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(sites)").fetchall()}
        for col, defn in [
            ("chat_in",   "INTEGER DEFAULT 0"),
            ("chat_out",  "INTEGER DEFAULT 0"),
            ("gen_in",    "INTEGER DEFAULT 0"),
            ("gen_out",   "INTEGER DEFAULT 0"),
            ("cache_read","INTEGER DEFAULT 0"),
            ("cost_usd",  "REAL DEFAULT 0"),
        ]:
            if col not in cols:
                c.execute(f"ALTER TABLE sites ADD COLUMN {col} {defn}")
    migrate_add_oauth_columns()

def _make_user_columns_nullable(columns: list[str]):
    """Relax legacy NOT NULL constraints without rebuilding or copying tables."""
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
    """Add Google OAuth columns and indexes without touching existing rows."""
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

        # SQLite cannot add UNIQUE columns with ALTER TABLE, so use partial
        # unique indexes that still allow multiple NULL values.
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

# ── Users ──────────────────────────────────────────────────────────────────
def _hash_email_verify_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def generate_email_verify_token() -> str:
    return secrets.token_urlsafe(32)

def create_user(phone: str, password: str, name: str = "", email: str = "") -> dict | None:
    email = normalize_email(email)
    try:
        hashed = bcrypt.hash(password)
        with get_conn() as c:
            cur = c.execute(
                """INSERT INTO users
                   (phone, password, email, email_verified, name, tokens)
                   VALUES (?,?,?,?,?,0)""",
                (phone, hashed, email or None, 0, name)
            )
            row = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row) if row else None
    except sqlite3.IntegrityError:
        return None

def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()

def get_user_by_phone(phone: str) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        return dict(row) if row else None

def get_user_by_email(email: str) -> dict | None:
    email = normalize_email(email)
    if not email:
        return None
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return dict(row) if row else None

def get_user_by_google_id(google_id: str) -> dict | None:
    google_id = (google_id or "").strip()
    if not google_id:
        return None
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE google_id=?", (google_id,)).fetchone()
        return dict(row) if row else None

def get_user_by_id(uid: int) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

def verify_password(phone: str, password: str) -> dict | None:
    user = get_user_by_phone(phone)
    if user and user.get("password") and bcrypt.verify(password, user["password"]):
        return user
    return None

def set_email_verification_token(user_id: int, token: str | None = None,
                                 expires_at: int | None = None,
                                 sent_at: int | None = None) -> str | None:
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
    email = normalize_email(email)
    google_id = (google_id or "").strip()
    if not email or not google_id:
        return None

    display_name = (name or "").strip() or email.split("@", 1)[0]
    try:
        with get_conn() as c:
            cur = c.execute(
                """INSERT INTO users
                   (phone, password, email, email_verified, google_id, auth_provider, avatar_url, name, tokens)
                   VALUES (NULL, NULL, ?, ?, ?, 'google', ?, ?, 0)""",
                (email, 1 if email_verified else 0, google_id, avatar_url or "", display_name),
            )
            row = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row) if row else None
    except sqlite3.IntegrityError:
        return None

def link_google_to_existing_user(user_id: int, email: str, google_id: str, avatar_url: str = "",
                                 email_verified: bool = True) -> dict | None:
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
                       auth_provider=?
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
    with get_conn() as c:
        user = c.execute("SELECT tokens FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["tokens"] < amount:
            return False
        c.execute("UPDATE users SET tokens=tokens-? WHERE id=?", (amount, user_id))
        c.execute(
            "INSERT INTO token_log (user_id,site_id,delta,reason,claude_in,claude_out,cache_read,cost_usd) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, site_id, -amount, reason, claude_in, claude_out, cache_read, cost_usd)
        )
        return True

def add_tokens(user_id: int, amount: int, reason: str):
    with get_conn() as c:
        c.execute("UPDATE users SET tokens=tokens+? WHERE id=?", (amount, user_id))
        c.execute("INSERT INTO token_log (user_id,delta,reason) VALUES (?,?,?)",
                  (user_id, amount, reason))

def add_site_slot(user_id: int, credits: int, reason: str):
    """Give user +1 site slot and add credits."""
    with get_conn() as c:
        c.execute("UPDATE users SET site_slots=site_slots+1, tokens=tokens+? WHERE id=?",
                  (credits, user_id))
        c.execute("INSERT INTO token_log (user_id,delta,reason) VALUES (?,?,?)",
                  (user_id, credits, reason))

# ── Sites ──────────────────────────────────────────────────────────────────
def create_site(user_id: int, slug: str, title: str, data: dict, html_path: str,
                tokens_used: int, chat_in: int = 0, chat_out: int = 0,
                gen_in: int = 0, gen_out: int = 0,
                cache_read: int = 0, cost_usd: float = 0.0) -> dict:
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO sites
               (user_id,slug,title,data,html_path,tokens_used,
                chat_in,chat_out,gen_in,gen_out,cache_read,cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, slug, title, json.dumps(data, ensure_ascii=False), html_path, tokens_used,
             chat_in, chat_out, gen_in, gen_out, cache_read, cost_usd)
        )
        return get_site_by_id(cur.lastrowid)

def get_site_by_id(sid: int) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM sites WHERE id=?", (sid,)).fetchone()
        if not row: return None
        d = dict(row)
        d["data"] = json.loads(d["data"] or "{}")
        return d

def get_site_by_slug(slug: str) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()
        if not row: return None
        d = dict(row)
        d["data"] = json.loads(d["data"] or "{}")
        return d

def get_user_sites(user_id: int) -> list:
    with get_conn() as c:
        rows = c.execute("SELECT * FROM sites WHERE user_id=? ORDER BY created DESC", (user_id,)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["data"] = json.loads(d["data"] or "{}")
            result.append(d)
        return result

def update_site_html(site_id: int, html_path: str, tokens_used: int):
    with get_conn() as c:
        c.execute("UPDATE sites SET html_path=?,tokens_used=?,updated=datetime('now') WHERE id=?",
                  (html_path, tokens_used, site_id))

def update_site_data(site_id: int, data: dict):
    with get_conn() as c:
        c.execute("UPDATE sites SET data=?,updated=datetime('now') WHERE id=?",
                  (json.dumps(data, ensure_ascii=False), site_id))

def delete_site(site_id: int, user_id: int) -> bool:
    with get_conn() as c:
        cur = c.execute("DELETE FROM sites WHERE id=? AND user_id=?", (site_id, user_id))
        return cur.rowcount > 0

def get_token_log(user_id: int, limit: int = 20) -> list:
    with get_conn() as c:
        rows = c.execute(
            "SELECT delta, reason, cost_usd, ts FROM token_log WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

def update_user_name(user_id: int, name: str):
    with get_conn() as c:
        c.execute("UPDATE users SET name=? WHERE id=?", (name.strip(), user_id))

# ── Sessions ───────────────────────────────────────────────────────────────
import uuid as _uuid

def create_session(user_id: int) -> str:
    sid = _uuid.uuid4().hex
    expires = datetime.utcnow().replace(year=datetime.utcnow().year + 1).isoformat()
    with get_conn() as c:
        c.execute("INSERT INTO sessions VALUES (?,?,?)", (sid, user_id, expires))
    return sid

def get_session_user(sid: str) -> dict | None:
    with get_conn() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.expires > datetime('now')",
            (sid,)
        ).fetchone()
        return dict(row) if row else None

def delete_session(sid: str):
    with get_conn() as c:
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))

# ── Admin stats ────────────────────────────────────────────────────────────
def admin_stats() -> dict:
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
    with get_conn() as c:
        rows = c.execute("""
            SELECT u.id, u.phone, u.email, u.auth_provider, u.name, u.tokens, u.created,
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
            "SELECT order_id, invoice_id, amount, tokens, status, created FROM payments WHERE user_id=? ORDER BY created DESC",
            (user_id,)
        ).fetchall()
        return {
            "user": dict(user),
            "sites": [dict(r) for r in sites],
            "token_log": [dict(r) for r in token_log],
            "payments": [dict(r) for r in payments],
        }

# ── Payments ──────────────────────────────────────────────────────────────
def create_payment(user_id: int, order_id: str, invoice_id: str, amount: int, tokens: int,
                   status: str = "pending", catalog_item_id: str = "") -> dict:
    with get_conn() as c:
        # add catalog_item_id column if missing
        cols = {r[1] for r in c.execute("PRAGMA table_info(payments)").fetchall()}
        if "catalog_item_id" not in cols:
            c.execute("ALTER TABLE payments ADD COLUMN catalog_item_id TEXT DEFAULT ''")
        cur = c.execute(
            "INSERT INTO payments (user_id, order_id, invoice_id, amount, tokens, status, catalog_item_id) VALUES (?,?,?,?,?,?,?)",
            (user_id, order_id, invoice_id, amount, tokens, status, catalog_item_id)
        )
        row = c.execute("SELECT * FROM payments WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

def get_payment_by_order(order_id: str) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()
        return dict(row) if row else None

def complete_payment(payment_id: int):
    with get_conn() as c:
        c.execute("UPDATE payments SET status='paid', updated=datetime('now') WHERE id=?", (payment_id,))

def fail_payment(payment_id: int, reason: str = "failed"):
    with get_conn() as c:
        c.execute("UPDATE payments SET status=?, updated=datetime('now') WHERE id=?", (reason, payment_id))


init_db()
