import hashlib
import hmac
import os
import re
import secrets
import time
import unicodedata
from datetime import datetime, timezone

import db


EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
EMAIL_MAX_LENGTH = 254
EMAIL_LOCAL_MAX_LENGTH = 64
NAME_MIN_LENGTH = 2
NAME_MAX_LENGTH = 80
try:
    PASSWORD_MIN_LENGTH = int(os.environ.get("AUTH_PASSWORD_MIN_LENGTH", "8"))
except ValueError:
    PASSWORD_MIN_LENGTH = 8
PASSWORD_MIN_LENGTH = min(12, max(8, PASSWORD_MIN_LENGTH))
PASSWORD_MAX_LENGTH = 128
BCRYPT_MAX_BYTES = 72
PASSWORD_RESET_SECONDS = 60 * 60
PASSWORD_RESET_COOLDOWN_SECONDS = 60
PASSWORD_RESET_WINDOW_SECONDS = 60 * 60
PASSWORD_RESET_MAX_PER_WINDOW = 5
AUTH_RATE_WINDOW_SECONDS = 10 * 60
AUTH_RATE_MAX_ATTEMPTS = 12
AUTH_IDENTITY_RATE_MAX_ATTEMPTS = 8
AUTH_CSRF_COOKIE = "auth_csrf"

_AUTH_ATTEMPTS: dict[str, list[float]] = {}
_RESET_ATTEMPTS: dict[str, list[float]] = {}
_AUTH_FAILURES: dict[str, list[float]] = {}

COMMON_PASSWORDS = {
    "password", "password1", "password123", "qwerty123", "qwertyuiop",
    "12345678", "123456789", "1234567890", "11111111", "00000000",
    "admin123", "letmein1", "welcome1", "iloveyou1", "kazakhstan1",
    "пароль123", "йцукен123",
}
INVISIBLE_CHARS = {
    "\u00ad", "\u034f", "\u061c", "\u115f", "\u1160", "\u17b4", "\u17b5",
    "\u180e", "\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u2028",
    "\u2029", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2060",
    "\u2061", "\u2062", "\u2063", "\u2064", "\u2066", "\u2067", "\u2068",
    "\u2069", "\u206a", "\u206b", "\u206c", "\u206d", "\u206e", "\u206f",
    "\ufeff",
}


class AuthError(Exception):
    # auth error class
    def __init__(self, code: str, message: str, field: str | None = None, status_code: int = 400):
        #  init  
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.status_code = status_code


def now_ts() -> int:
    # now ts
    return int(time.time())


def normalize_email(email: str | None) -> str:
    # normalize email
    value = _normalize_text(email or "", "email", max_length=EMAIL_MAX_LENGTH).lower()
    return value


def is_valid_email(email: str) -> bool:
    # is valid email
    try:
        validate_email(email)
        return True
    except AuthError:
        return False


def _contains_unsafe_char(value: str) -> bool:
    # contains unsafe char
    for ch in value:
        category = unicodedata.category(ch)
        if ch in INVISIBLE_CHARS or category[0] == "C" or category in {"Zl", "Zp"}:
            return True
    return False


def _normalize_text(value: str | None, field: str, max_length: int) -> str:
    # normalize text
    if not isinstance(value, str):
        raise AuthError(f"invalid_{field}", "Unable to process request", field)
    if "\x00" in value:
        raise AuthError(f"invalid_{field}", "Unable to process request", field)
    try:
        normalized = unicodedata.normalize("NFKC", value)
    except Exception as exc:
        raise AuthError(f"invalid_{field}", "Unable to process request", field) from exc
    normalized = normalized.strip()
    if len(normalized) > max_length:
        raise AuthError(f"invalid_{field}", "Значение слишком длинное.", field)
    if _contains_unsafe_char(normalized):
        raise AuthError(f"invalid_{field}", "Проверьте введённые данные.", field)
    return normalized


def safe_form_value(value: str | None, max_length: int = 254) -> str:
    # safe form value
    if not isinstance(value, str):
        return ""
    try:
        normalized = unicodedata.normalize("NFKC", value).strip()
    except Exception:
        return ""
    normalized = "".join(ch for ch in normalized if not _contains_unsafe_char(ch))
    return normalized[:max_length]


def _identity_digest(value: str) -> str:
    # identity digest
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:24]


def validate_email(email: str | None) -> str:
    # validate and normalize email format
    value = normalize_email(email)
    if not value:
        raise AuthError("invalid_email", "Введите корректный email.", "email")
    if len(value) > EMAIL_MAX_LENGTH or " " in value or "\t" in value:
        raise AuthError("invalid_email", "Введите корректный email.", "email")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AuthError("invalid_email", "Введите корректный email.", "email") from exc
    if value.count("@") != 1:
        raise AuthError("invalid_email", "Введите корректный email.", "email")
    local, domain = value.rsplit("@", 1)
    if (
        not local or not domain or len(local) > EMAIL_LOCAL_MAX_LENGTH
        or ".." in local or ".." in domain or domain.startswith(".") or domain.endswith(".")
    ):
        raise AuthError("invalid_email", "Введите корректный email.", "email")
    labels = domain.split(".")
    if len(labels) < 2 or any(not label or len(label) > 63 for label in labels):
        raise AuthError("invalid_email", "Введите корректный email.", "email")
    if not EMAIL_RE.fullmatch(value):
        raise AuthError("invalid_email", "Введите корректный email.", "email")
    tld = labels[-1]
    if len(tld) < 2 or not tld.isalpha():
        raise AuthError("invalid_email", "Введите корректный email.", "email")
    return value


def _is_latin_or_cyrillic_letter(ch: str) -> bool:
    # is latin or cyrillic letter
    if not unicodedata.category(ch).startswith("L"):
        return False
    name = unicodedata.name(ch, "")
    return "LATIN" in name or "CYRILLIC" in name


def validate_name(name: str | None, required: bool = True) -> str:
    # validate user name rules
    value = _normalize_text(name or "", "name", NAME_MAX_LENGTH)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[‐‑‒–—―]", "-", value)
    value = value.replace("`", "'").replace("’", "'").replace("ʼ", "'")
    if not value:
        if required:
            raise AuthError("invalid_name", "Введите имя.", "name")
        return ""
    if len(value) < NAME_MIN_LENGTH:
        raise AuthError("invalid_name", "Введите имя.", "name")
    if value[0] in " -'" or value[-1] in " -'":
        raise AuthError("invalid_name", "Проверьте имя.", "name")
    punctuation_count = 0
    prev = ""
    for ch in value:
        if _is_latin_or_cyrillic_letter(ch) or ch == " ":
            prev = ch
            continue
        if ch in "-'":
            punctuation_count += 1
            if prev in "-' ":
                raise AuthError("invalid_name", "Проверьте имя.", "name")
            prev = ch
            continue
        raise AuthError("invalid_name", "Проверьте имя.", "name")
    if punctuation_count > 6 or re.search(r"([ '-])\1{1,}", value):
        raise AuthError("invalid_name", "Проверьте имя.", "name")
    return value


def validate_password(
    password: str,
    confirm_password: str | None = None,
    email: str | None = None,
    name: str | None = None,
):
    # validate password strength and confirmation
    password = password or ""
    if not isinstance(password, str):
        raise AuthError("weak_password", "Пароль не подходит.", "password")
    if "\x00" in password or _contains_unsafe_char(password):
        raise AuthError("weak_password", "Пароль не подходит.", "password")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AuthError("weak_password", f"Пароль должен быть не короче {PASSWORD_MIN_LENGTH} символов.", "password")
    if len(password) > PASSWORD_MAX_LENGTH or len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise AuthError("weak_password", "Пароль слишком длинный.", "password")
    if password.strip() != password:
        raise AuthError("weak_password", "Уберите пробелы в начале или конце пароля.", "password")
    if confirm_password is not None and password != confirm_password:
        raise AuthError("password_mismatch", "Пароли не совпадают.", "confirm_password")


def client_key(request, suffix: str = "") -> str:
    # client key
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    ip = forwarded or (request.client.host if request.client else "unknown")
    ua = request.headers.get("user-agent", "")[:80]
    return f"{ip}:{suffix}:{hashlib.sha256(ua.encode()).hexdigest()[:12]}"


def _limited(bucket: dict[str, list[float]], key: str, limit: int, window: int) -> bool:
    # limited
    now = time.time()
    attempts = [ts for ts in bucket.get(key, []) if now - ts < window]
    if len(attempts) >= limit:
        bucket[key] = attempts
        return True
    attempts.append(now)
    bucket[key] = attempts
    return False


def clear_rate_limit(key: str):
    # clear rate limit
    _AUTH_ATTEMPTS.pop(key, None)
    _AUTH_FAILURES.pop(key, None)


def _check_identity_rate(route: str, identity: str):
    # check identity rate
    digest = _identity_digest(identity)
    if _limited(_AUTH_FAILURES, f"{route}:identity:{digest}", AUTH_IDENTITY_RATE_MAX_ATTEMPTS, AUTH_RATE_WINDOW_SECONDS):
        raise AuthError("rate_limited", "Слишком много попыток. Попробуйте позже.", status_code=429)


class CsrfService:
    # csrf service class
    @staticmethod
    def generate() -> str:
        # generate
        return secrets.token_urlsafe(32)

    @staticmethod
    def verify(form_token: str | None, cookie_token: str | None):
        # verify
        if not form_token or not cookie_token or not hmac.compare_digest(form_token, cookie_token):
            raise AuthError("csrf_failed", "Сессия формы устарела. Обновите страницу и попробуйте снова.", status_code=403)


class AuthService:
    # auth service class
    @staticmethod
    def register(email: str, password: str, confirm_password: str, name: str, key: str) -> dict:
        # process user registration
        if _limited(_AUTH_ATTEMPTS, f"register:{key}", AUTH_RATE_MAX_ATTEMPTS, AUTH_RATE_WINDOW_SECONDS):
            raise AuthError("rate_limited", "Слишком много попыток. Попробуйте позже.", status_code=429)

        email = validate_email(email)
        _check_identity_rate("register", email)
        name = validate_name(name)
        validate_password(password, confirm_password, email=email, name=name)
        if db.get_user_by_email(email):
            raise AuthError("unable_to_process", "Не удалось обработать запрос.", status_code=400)

        user = db.create_user_with_email(email, password, name)
        if not user:
            raise AuthError("unable_to_process", "Не удалось обработать запрос.", status_code=400)
        clear_rate_limit(f"register:{key}")
        clear_rate_limit(f"register:identity:{_identity_digest(email)}")
        return user

    @staticmethod
    def login(email: str, password: str, key: str) -> dict:
        # process user login
        if _limited(_AUTH_ATTEMPTS, f"login:{key}", AUTH_RATE_MAX_ATTEMPTS, AUTH_RATE_WINDOW_SECONDS):
            raise AuthError("rate_limited", "Слишком много попыток входа. Попробуйте позже.", status_code=429)

        identity = _normalize_text(email or "", "email", max_length=EMAIL_MAX_LENGTH)
        if not identity:
            raise AuthError("invalid_email", "Введите email, который использовали при регистрации.", "email")
        if "@" in identity:
            identity = validate_email(identity)
        else:
            phone = re.sub(r"[^\d]", "", identity)
            if len(phone) < 7:
                raise AuthError("invalid_email", "Введите email, который использовали при регистрации.", "email")
            identity = phone
        _check_identity_rate("login", identity)
        if not password:
            raise AuthError("missing_password", "Введите пароль.", "password")
        if not isinstance(password, str) or len(password) > PASSWORD_MAX_LENGTH or len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
            raise AuthError("invalid_credentials", "Неверный email или пароль.", status_code=401)
        if "\x00" in password or _contains_unsafe_char(password):
            raise AuthError("invalid_credentials", "Неверный email или пароль.", status_code=401)

        user = db.verify_password_identity(identity, password)
        if not user:
            raise AuthError("invalid_credentials", "Неверный email или пароль.", status_code=401)
        db.mark_user_login(user["id"])
        clear_rate_limit(f"login:{key}")
        clear_rate_limit(f"login:identity:{_identity_digest(identity)}")
        return db.get_user_by_id(user["id"]) or user


class SessionService:
    # session service class
    @staticmethod
    def create(user_id: int) -> str:
        # create new user session
        db.mark_user_login(user_id)
        return db.create_session(user_id)

    @staticmethod
    def delete(sid: str | None):
        # delete user session
        if sid:
            db.delete_session(sid)


class PasswordResetService:
    # password reset service class
    @staticmethod
    def _hash_token(token: str) -> str:
        # hash token
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def request(email: str, key: str) -> dict:
        # initiate password reset request
        if _limited(_RESET_ATTEMPTS, f"reset:{key}", 8, AUTH_RATE_WINDOW_SECONDS):
            raise AuthError("rate_limited", "Слишком много запросов. Попробуйте позже.", status_code=429)

        email = validate_email(email)
        _check_identity_rate("reset", email)

        user = db.get_user_by_email(email)
        if not user:
            return {"ok": True, "sent": False, "email": email}

        recent = db.count_recent_password_resets(user["id"], now_ts() - PASSWORD_RESET_WINDOW_SECONDS)
        if recent >= PASSWORD_RESET_MAX_PER_WINDOW:
            raise AuthError("reset_rate_limited", "Слишком много ссылок для этого аккаунта. Попробуйте позже.", status_code=429)

        token = secrets.token_urlsafe(40)
        expires_at = now_ts() + PASSWORD_RESET_SECONDS
        db.create_password_reset_token(user["id"], PasswordResetService._hash_token(token), expires_at)
        return {
            "ok": True,
            "sent": True,
            "email": email,
            "user": user,
            "token": token,
            "expires_at": expires_at,
            "expires_minutes": PASSWORD_RESET_SECONDS // 60,
        }

    @staticmethod
    def validate(token: str) -> dict:
        # validate password reset token
        token = (token or "").strip()
        if not token:
            raise AuthError("invalid_reset_token", "Ссылка восстановления неверна или уже использована.")
        row = db.get_password_reset_token(PasswordResetService._hash_token(token))
        if not row:
            raise AuthError("invalid_reset_token", "Ссылка восстановления неверна или уже использована.")
        if int(row.get("used") or 0):
            raise AuthError("used_reset_token", "Эта ссылка уже использована.")
        expires = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if expires.timestamp() < now_ts():
            raise AuthError("expired_reset_token", "Ссылка восстановления истекла. Запросите новую.")
        return row

    @staticmethod
    def reset(token: str, password: str, confirm_password: str, key: str) -> dict:
        # execute password reset
        if _limited(_AUTH_ATTEMPTS, f"reset-submit:{key}", AUTH_RATE_MAX_ATTEMPTS, AUTH_RATE_WINDOW_SECONDS):
            raise AuthError("rate_limited", "Слишком много попыток. Попробуйте позже.", status_code=429)

        row = PasswordResetService.validate(token)
        validate_password(password, confirm_password, email=row.get("email"), name=row.get("name"))
        user = db.update_user_password(row["user_id"], password)
        db.mark_password_reset_token_used(PasswordResetService._hash_token(token))
        clear_rate_limit(f"reset-submit:{key}")
        return user
