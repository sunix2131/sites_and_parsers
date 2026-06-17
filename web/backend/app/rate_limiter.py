from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.models import CallLog
from app.config import settings


class RateLimiter:
    def __init__(self, max_actions_per_hour: int = None):
        self.max_actions = max_actions_per_hour or settings.RATE_LIMIT_PER_HOUR

    async def check(self, db: AsyncSession, seller_id: int) -> tuple[bool, dict]:
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)

        result = await db.execute(
            select(func.count(CallLog.id)).where(
                CallLog.seller_id == seller_id,
                CallLog.action.in_(["status_confirmed", "status_declined", "status_followup", "status_no_answer"]),
                CallLog.created_at >= one_hour_ago,
            )
        )
        count = result.scalar()

        remaining = max(0, self.max_actions - count)
        resets_at = datetime.utcnow() + timedelta(hours=1)

        info = {
            "actions_remaining": remaining,
            "actions_used": count,
            "limit": self.max_actions,
            "resets_at": resets_at,
        }

        return remaining > 0, info

    async def get_info(self, db: AsyncSession, seller_id: int) -> dict:
        _, info = await self.check(db, seller_id)
        return info


rate_limiter = RateLimiter()
