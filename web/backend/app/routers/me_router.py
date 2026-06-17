from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import User, UserRole, Lead, LeadStatus
from app.auth import get_current_user
from app.rate_limiter import rate_limiter

router = APIRouter(prefix="/api/me", tags=["me"])


@router.get("")
async def get_me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role.value if isinstance(user.role, UserRole) else user.role,
        "is_active": user.is_active,
    }


@router.get("/stats")
async def get_my_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role == UserRole.seller:
        total = await db.execute(select(func.count(Lead.id)).where(Lead.assigned_to == user.id))
        confirmed = await db.execute(
            select(func.count(Lead.id)).where(Lead.assigned_to == user.id, Lead.status == LeadStatus.confirmed)
        )
        declined = await db.execute(
            select(func.count(Lead.id)).where(Lead.assigned_to == user.id, Lead.status == LeadStatus.declined)
        )
        followup = await db.execute(
            select(func.count(Lead.id)).where(Lead.assigned_to == user.id, Lead.status == LeadStatus.followup)
        )
        no_answer = await db.execute(
            select(func.count(Lead.id)).where(Lead.assigned_to == user.id, Lead.status == LeadStatus.no_answer)
        )
        calling = await db.execute(
            select(func.count(Lead.id)).where(Lead.assigned_to == user.id, Lead.status == LeadStatus.calling)
        )
        assigned = await db.execute(
            select(func.count(Lead.id)).where(Lead.assigned_to == user.id, Lead.status == LeadStatus.assigned)
        )
        waiting = await db.execute(
            select(func.count(Lead.id)).where(Lead.assigned_to == user.id, Lead.status == LeadStatus.waiting)
        )

        rate_info = await rate_limiter.get_info(db, user.id)

        return {
            "total": total.scalar() or 0,
            "assigned": assigned.scalar() or 0,
            "calling": calling.scalar() or 0,
            "confirmed": confirmed.scalar() or 0,
            "declined": declined.scalar() or 0,
            "followup": followup.scalar() or 0,
            "no_answer": no_answer.scalar() or 0,
            "waiting": waiting.scalar() or 0,
            "rate_limit": rate_info,
        }
    else:
        total = await db.execute(select(func.count(Lead.id)))
        new_count = await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.new))
        confirmed = await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.confirmed))
        declined = await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.declined))

        return {
            "total": total.scalar() or 0,
            "new": new_count.scalar() or 0,
            "confirmed": confirmed.scalar() or 0,
            "declined": declined.scalar() or 0,
        }
