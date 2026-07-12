from sqlalchemy import Column, Integer, String, Float, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from core.database import Base

class SiteVersion(Base):
    __tablename__ = "site_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    site_id = Column(Integer, ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    version_no = Column(Integer, nullable=False)
    html = Column(Text, nullable=False)
    data = Column(Text)
    reason = Column(String)
    created = Column(String)

    __table_args__ = (
        UniqueConstraint('site_id', 'version_no', name='uix_site_version'),
    )

    site = relationship("Site", back_populates="versions", lazy="noload")
