from enum import Enum


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
