from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models.user import User
from repositories.base import BaseRepository

class UserRepository(BaseRepository[User]):
    async def get_by_email(self, db: AsyncSession, email: str) -> Optional[User]:
        result = await db.execute(select(User).filter(User.email == email))
        return result.scalars().first()
    
    async def get_by_phone(self, db: AsyncSession, phone: str) -> Optional[User]:
        result = await db.execute(select(User).filter(User.phone == phone))
        return result.scalars().first()

    async def get_by_google_id(self, db: AsyncSession, google_id: str) -> Optional[User]:
        result = await db.execute(select(User).filter(User.google_id == google_id))
        return result.scalars().first()

user_repo = UserRepository(User)
