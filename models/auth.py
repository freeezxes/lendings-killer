from sqlalchemy import Column, Integer, String, Text, ForeignKey
from core.database import Base

class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    name = Column(String)
    created = Column(String) # TEXT DEFAULT (datetime('now'))
    last_login_at = Column(String)

class AdminSession(Base):
    __tablename__ = "admin_sessions"

    id = Column(String, primary_key=True)
    admin_id = Column(Integer, ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False)
    expires = Column(String, nullable=False)

class Session(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    expires = Column(String, nullable=False)

class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    token = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expires_at = Column(String, nullable=False)
    used = Column(Integer, default=0)
    created_at = Column(String)
    used_at = Column(String)
