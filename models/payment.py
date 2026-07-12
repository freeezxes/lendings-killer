from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from core.database import Base

class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    ext_id = Column(String, nullable=True, index=True)
    amount = Column(Float, nullable=True)
    type = Column(String, nullable=True) # e.g. slot, dev_credit, promo_credit
    status = Column(String, default="pending", index=True)
    created = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")


class SupportInvoice(Base):
    __tablename__ = "support_invoices"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False, index=True)
    ext_id = Column(String, nullable=False, index=True)
    amount = Column(Float, nullable=False)
    status = Column(String, default="pending", index=True)
    created = Column(DateTime, default=datetime.utcnow)

    site = relationship("Site")
