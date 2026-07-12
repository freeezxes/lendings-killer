from sqlalchemy import Column, Integer, String, Float, Text, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from core.database import Base

class DevCreditLog(Base):
    __tablename__ = "dev_credit_log"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="SET NULL"), nullable=True)
    delta = Column(Integer, nullable=False)
    reason = Column(String, nullable=True)
    claude_in = Column(Integer, default=0)
    claude_out = Column(Integer, default=0)
    cache_read = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    balance_after = Column(Integer, nullable=True)
    legacy_token_log_id = Column(Integer, unique=True, nullable=True)
    created = Column(DateTime, default=datetime.utcnow)

class PromoCreditLog(Base):
    __tablename__ = "promo_credit_log"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="SET NULL"), nullable=True)
    campaign_id = Column(Integer, nullable=True)
    delta = Column(Integer, nullable=False)
    reason = Column(String, nullable=True)
    balance_after = Column(Integer, nullable=True)
    created = Column(DateTime, default=datetime.utcnow)

class OnboardingSession(Base):
    __tablename__ = "onboarding_sessions"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    draft_title = Column(String, nullable=True)
    draft_slug = Column(String, nullable=True)
    messages_json = Column(Text, nullable=True)
    status = Column(String, default="active")
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    is_read = Column(Integer, default=0)
    created = Column(DateTime, default=datetime.utcnow)
