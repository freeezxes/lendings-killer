from sqlalchemy import Column, Integer, String, Float, Text, ForeignKey
from core.database import Base

class TokenLog(Base):
    __tablename__ = "token_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    site_id = Column(Integer, ForeignKey("sites.id"))
    delta = Column(Integer, nullable=False)
    reason = Column(String)
    claude_in = Column(Integer, default=0)
    claude_out = Column(Integer, default=0)
    cache_read = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    ts = Column(String)

class DevCreditLog(Base):
    __tablename__ = "dev_credit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="SET NULL"))
    delta = Column(Integer, nullable=False)
    reason = Column(String)
    claude_in = Column(Integer, default=0)
    claude_out = Column(Integer, default=0)
    cache_read = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    balance_after = Column(Integer)
    legacy_token_log_id = Column(Integer, unique=True)
    created = Column(String)

class PromoCreditLog(Base):
    __tablename__ = "promo_credit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="SET NULL"))
    campaign_id = Column(Integer)
    delta = Column(Integer, nullable=False)
    reason = Column(String)
    balance_after = Column(Integer)
    created = Column(String)

class OnboardingSession(Base):
    __tablename__ = "onboarding_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    draft_title = Column(String)
    sort_order = Column(Integer, default=0)
    status = Column(String, nullable=False, default='draft')
    history = Column(Text, nullable=False, default='[]')
    collected = Column(Text, nullable=False, default='{}')
    photo_urls = Column(Text, nullable=False, default='[]')
    chat_in = Column(Integer, default=0)
    chat_out = Column(Integer, default=0)
    chat_cr = Column(Integer, default=0)
    generation_started_at = Column(String)
    generated_site_id = Column(Integer, ForeignKey("sites.id", ondelete="SET NULL"))
    error = Column(String)
    created = Column(String)
    updated = Column(String)

class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"))
    type = Column(String, nullable=False)
    title = Column(String, nullable=False)
    body = Column(String)
    is_read = Column(Integer, default=0)
    created = Column(String)
