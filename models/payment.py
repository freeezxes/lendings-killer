from sqlalchemy import Column, Integer, String, Float, ForeignKey
from sqlalchemy.orm import relationship
from core.database import Base

class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    order_id = Column(String, unique=True, nullable=False)
    invoice_id = Column(String)
    amount = Column(Integer, nullable=False)
    tokens = Column(Integer, nullable=False)
    payment_kind = Column(String, default='legacy')
    promo_credits = Column(Integer, default=0)
    dev_credits = Column(Integer, default=0)
    site_id = Column(Integer, ForeignKey("sites.id"))
    support_invoice_id = Column(Integer)
    status = Column(String, default="pending")
    created = Column(String)
    updated = Column(String)
    catalog_item_id = Column(String, default='')

    user = relationship("User", lazy="noload")

class SupportInvoice(Base):
    __tablename__ = "support_invoices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Integer, nullable=False)
    months = Column(Integer, default=1)
    status = Column(String, nullable=False, default='pending')
    due_at = Column(String)
    paid_at = Column(String)
    order_id = Column(String, unique=True)
    created = Column(String)
    updated = Column(String)

    site = relationship("Site", lazy="noload")
    user = relationship("User", lazy="noload")
