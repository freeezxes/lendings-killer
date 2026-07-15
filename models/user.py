from sqlalchemy import Column, Integer, String, Text, Float, Boolean, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from core.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    phone = Column(String, unique=True, index=True, nullable=True)
    password = Column(String, nullable=True)
    password_hash = Column(String, nullable=True)
    email = Column(String, unique=True, index=True, nullable=True)
    email_verified = Column(Integer, default=0)
    email_verify_token = Column(String, index=True, nullable=True)
    email_verify_expires = Column(Integer, nullable=True)
    verification_sent_at = Column(Integer, nullable=True)
    google_id = Column(String, unique=True, index=True, nullable=True)
    auth_provider = Column(String, default="local")
    avatar_url = Column(String, nullable=True)
    name = Column(String, nullable=True)
    
    tokens = Column(Integer, default=0)
    dev_credits = Column(Integer, default=0)
    promo_credits = Column(Integer, default=0)
    site_slots = Column(Integer, default=0)
    
    created = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)

    sites = relationship("Site", back_populates="user", cascade="all, delete-orphan")

    # Dict-compatibility shims: large parts of the codebase predate the ORM
    # migration and still treat `user` as a dict (user.get("x"), user["x"]).
    # These let a User instance stand in for that legacy interface.
    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        return getattr(self, key)

    def __contains__(self, key):
        return hasattr(self, key)
