from sqlalchemy import Column, Integer, String, Text, Float, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from core.database import Base

class Site(Base):
    __tablename__ = "sites"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    slug = Column(String, unique=True, index=True, nullable=False)
    title = Column(String, nullable=True)
    data = Column(Text, nullable=True) # JSON payload
    html_path = Column(String, nullable=True)
    tokens_used = Column(Integer, default=0)
    
    support_paid_until = Column(String, nullable=True)
    support_status = Column(String, default="active", index=True)
    promo_status = Column(String, default="not_configured", index=True)
    analytics_status = Column(String, default="unavailable")
    promo_setup_done = Column(Integer, default=0)
    
    chat_in = Column(Integer, default=0)
    chat_out = Column(Integer, default=0)
    gen_in = Column(Integer, default=0)
    gen_out = Column(Integer, default=0)
    cache_read = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    
    edit_status = Column(String, default="ready")
    
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="sites")
    versions = relationship("SiteVersion", back_populates="site", cascade="all, delete-orphan")
