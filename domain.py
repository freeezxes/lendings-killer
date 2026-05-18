from enum import Enum
import re
import unicodedata


class SupportStatus(str, Enum):
    # support status class
    ACTIVE = "active"
    EXPIRING_SOON = "expiring_soon"
    INVOICE_ISSUED = "invoice_issued"
    SUSPENDED = "suspended"


class PromotionStatus(str, Enum):
    # promotion status class
    NOT_CONFIGURED = "not_configured"
    CONFIGURED = "configured"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


class AnalyticsStatus(str, Enum):
    # analytics status class
    UNAVAILABLE = "unavailable"
    ACTIVE = "active"
    OUTDATED = "outdated"
    BLOCKED = "blocked"


class CampaignStatus(str, Enum):
    # campaign status class
    ACTIVE = "active"
    COMPLETED = "completed"
    PAUSED = "paused"
    STOPPED_SUPPORT_EXPIRED = "stopped_support_expired"
    STOPPED_SITE_CHANGED = "stopped_site_changed"
    FAILED = "failed"


class InvoiceStatus(str, Enum):
    # invoice status class
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"


PROMO_CREDIT_TENGE = 10
PROMO_MIN_PURCHASE = 100
PROMO_SETUP_COST = 500
CAMPAIGN_MIN_CREDITS = 100
CAMPAIGN_MIN_DURATION_HOURS = 2
SUPPORT_MONTHLY_PRICE = 1000
SUPPORT_INCLUDED_DAYS = 30
SUPPORT_WARNING_DAYS = 7
SUPPORT_GRACE_DAYS = 1
VERSION_RESTORE_DEV_CREDITS = 10

MAX_DRAFTS = 5
ACTIVE_DRAFT_STATUSES = ("draft", "ready", "generating", "failed")
DRAFT_TITLE_MAX_CHARS = 16
_DRAFT_FORBIDDEN_CHARS = set("<>/\\{}[]\"'`;|&$`")
_DRAFT_INVISIBLE_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn"}


class DraftValidationError(ValueError):
    # draft validation error
    pass


def is_active_draft_status(status: str | None) -> bool:
    # is active draft status
    return (status or "draft") in ACTIVE_DRAFT_STATUSES


def normalize_draft_title(value: str | None) -> str:
    # normalize and validate a user-provided draft title
    if not isinstance(value, str):
        raise DraftValidationError("Invalid draft name")
    title = unicodedata.normalize("NFKC", value)
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        raise DraftValidationError("Draft name cannot be empty")
    if len(title) > DRAFT_TITLE_MAX_CHARS:
        raise DraftValidationError("Draft name is too long")
    for ch in title:
        if unicodedata.category(ch) in _DRAFT_INVISIBLE_CATEGORIES or ch in _DRAFT_FORBIDDEN_CHARS:
            raise DraftValidationError("Draft name contains invalid characters")
    return title
