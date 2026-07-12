from sqlalchemy import Column, Integer, String, Text, ForeignKey
from core.database import Base

class PromotionSetup(Base):
    __tablename__ = "promotion_setups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, unique=True)
    credits_spent = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    created = Column(String)
    updated = Column(String)

class PromotionCampaign(Base):
    __tablename__ = "promotion_campaigns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    credits_spent = Column(Integer, nullable=False)
    duration_hours = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    forecast_json = Column(Text)
    starts_at = Column(String)
    ends_at = Column(String)
    stopped_reason = Column(String)
    created = Column(String)
    updated = Column(String)
