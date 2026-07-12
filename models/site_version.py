from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from core.database import Base

class SiteVersion(Base):
    __tablename__ = "site_versions"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False, index=True)
    slug = Column(String, nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    html_content = Column(Text, nullable=False)
    created = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('site_id', 'version_number', name='uix_site_version'),
    )

    site = relationship("Site", back_populates="versions")
