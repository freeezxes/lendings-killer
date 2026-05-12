import sqlite3, json, bcrypt as _bcrypt
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
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            phone     TEXT UNIQUE NOT NULL,
            password  TEXT NOT NULL,
            name      TEXT,
            tokens    INTEGER DEFAULT 30,
            created   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sites (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            slug       TEXT UNIQUE NOT NULL,
            title      TEXT,
            data       TEXT,
            html_path  TEXT,
            tokens_used INTEGER DEFAULT 0,
            created    TEXT DEFAULT (datetime('now')),
            updated    TEXT DEFAULT (datetime('now'))
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

# ── Users ──────────────────────────────────────────────────────────────────
def create_user(phone: str, password: str, name: str = "") -> dict | None:
    try:
        hashed = bcrypt.hash(password)
        with get_conn() as c:
            cur = c.execute(
                "INSERT INTO users (phone, password, name) VALUES (?,?,?)",
                (phone, hashed, name)
            )
            return get_user_by_id(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None

def get_user_by_phone(phone: str) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        return dict(row) if row else None

def get_user_by_id(uid: int) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

def verify_password(phone: str, password: str) -> dict | None:
    user = get_user_by_phone(phone)
    if user and bcrypt.verify(password, user["password"]):
        return user
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

# ── Sites ──────────────────────────────────────────────────────────────────
def create_site(user_id: int, slug: str, title: str, data: dict, html_path: str, tokens_used: int) -> dict:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO sites (user_id,slug,title,data,html_path,tokens_used) VALUES (?,?,?,?,?,?)",
            (user_id, slug, title, json.dumps(data, ensure_ascii=False), html_path, tokens_used)
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
        users     = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        sites     = c.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        total_cost= c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM token_log").fetchone()[0]
        total_tok = c.execute("SELECT COALESCE(SUM(claude_in+claude_out),0) FROM token_log").fetchone()[0]
        recent    = c.execute("""
            SELECT u.phone, u.name, s.title, s.slug, s.tokens_used, s.created
            FROM sites s JOIN users u ON u.id=s.user_id
            ORDER BY s.created DESC LIMIT 20
        """).fetchall()
        return {
            "users": users, "sites": sites,
            "total_cost": total_cost, "total_tokens": total_tok,
            "recent_sites": [dict(r) for r in recent]
        }

# ── Payments ──────────────────────────────────────────────────────────────
def create_payment(user_id: int, order_id: str, invoice_id: str, amount: int, tokens: int, status: str = "pending") -> dict:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO payments (user_id, order_id, invoice_id, amount, tokens, status) VALUES (?,?,?,?,?,?)",
            (user_id, order_id, invoice_id, amount, tokens, status)
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
