from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models.site import Site
from repositories.base import BaseRepository

class SiteRepository(BaseRepository[Site]):
    async def get_by_slug(self, db: AsyncSession, slug: str) -> Optional[Site]:
        result = await db.execute(select(Site).filter(Site.slug == slug))
        return result.scalars().first()

    async def get_multi_by_user(self, db: AsyncSession, user_id: int) -> List[Site]:
        result = await db.execute(select(Site).filter(Site.user_id == user_id).order_by(Site.created.desc()))
        return result.scalars().all()

site_repo = SiteRepository(Site)
