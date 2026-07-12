from models.auth import AdminUser, AdminSession, Session, PasswordResetToken
from repositories.base import BaseRepository

class AdminUserRepository(BaseRepository[AdminUser]):
    pass
admin_user_repo = AdminUserRepository(AdminUser)

class AdminSessionRepository(BaseRepository[AdminSession]):
    pass
admin_session_repo = AdminSessionRepository(AdminSession)

class SessionRepository(BaseRepository[Session]):
    pass
session_repo = SessionRepository(Session)

class PasswordResetTokenRepository(BaseRepository[PasswordResetToken]):
    pass
password_reset_token_repo = PasswordResetTokenRepository(PasswordResetToken)
