from models.log import TokenLog, DevCreditLog, PromoCreditLog, OnboardingSession, Notification
from repositories.base import BaseRepository

class TokenLogRepository(BaseRepository[TokenLog]):
    pass
token_log_repo = TokenLogRepository(TokenLog)

class DevCreditLogRepository(BaseRepository[DevCreditLog]):
    pass
dev_credit_log_repo = DevCreditLogRepository(DevCreditLog)

class PromoCreditLogRepository(BaseRepository[PromoCreditLog]):
    pass
promo_credit_log_repo = PromoCreditLogRepository(PromoCreditLog)

class OnboardingSessionRepository(BaseRepository[OnboardingSession]):
    pass
onboarding_session_repo = OnboardingSessionRepository(OnboardingSession)

class NotificationRepository(BaseRepository[Notification]):
    pass
notification_repo = NotificationRepository(Notification)
