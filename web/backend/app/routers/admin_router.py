from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import User, UserRole, Lead, LeadStatus
from app.schemas import UserCreate, UserUpdate, UserOut, DashboardStats
from app.auth import require_admin, hash_password

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return result.scalars().all()


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    data: UserCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    existing = await db.execute(select(User).where(User.username == data.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Пользователь с таким логином уже существует")

    user = User(
        username=data.username,
        password_hash=hash_password(data.password),
        full_name=data.full_name,
        role=UserRole(data.role),
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if data.full_name is not None:
        user.full_name = data.full_name
    if data.is_active is not None:
        user.is_active = data.is_active
    if data.password is not None:
        user.password_hash = hash_password(data.password)

    await db.flush()
    await db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if user.role == UserRole.admin:
        raise HTTPException(status_code=400, detail="Нельзя удалить администратора")
    await db.delete(user)


@router.get("/stats", response_model=DashboardStats)
async def get_stats(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    total = await db.execute(select(func.count(Lead.id)))
    new_count = await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.new))
    assigned_count = await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.assigned))
    confirmed_count = await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.confirmed))
    declined_count = await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.declined))
    followup_count = await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.followup))
    no_answer_count = await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.no_answer))
    calling_count = await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.calling))
    waiting_count = await db.execute(select(func.count(Lead.id)).where(Lead.status == LeadStatus.waiting))

    total_sellers = await db.execute(select(func.count(User.id)).where(User.role == UserRole.seller))
    active_sellers = await db.execute(
        select(func.count(User.id)).where(User.role == UserRole.seller, User.is_active == True)
    )

    total_val = total.scalar() or 0
    confirmed_val = confirmed_count.scalar() or 0
    declined_val = declined_count.scalar() or 0
    processed = confirmed_val + declined_val
    conversion = (confirmed_val / processed * 100) if processed > 0 else 0.0

    sellers_result = await db.execute(
        select(User).where(User.role == UserRole.seller).order_by(User.full_name)
    )
    sellers = sellers_result.scalars().all()

    seller_stats = []
    for seller in sellers:
        s_assigned = await db.execute(
            select(func.count(Lead.id)).where(Lead.assigned_to == seller.id)
        )
        s_confirmed = await db.execute(
            select(func.count(Lead.id)).where(Lead.assigned_to == seller.id, Lead.status == LeadStatus.confirmed)
        )
        s_declined = await db.execute(
            select(func.count(Lead.id)).where(Lead.assigned_to == seller.id, Lead.status == LeadStatus.declined)
        )
        seller_stats.append({
            "id": seller.id,
            "name": seller.full_name,
            "is_active": seller.is_active,
            "assigned": s_assigned.scalar() or 0,
            "confirmed": s_confirmed.scalar() or 0,
            "declined": s_declined.scalar() or 0,
        })

    return DashboardStats(
        total_leads=total_val,
        new_leads=new_count.scalar() or 0,
        assigned_leads=assigned_count.scalar() or 0,
        confirmed_leads=confirmed_val,
        declined_leads=declined_val,
        followup_leads=followup_count.scalar() or 0,
        no_answer_leads=no_answer_count.scalar() or 0,
        calling_leads=calling_count.scalar() or 0,
        waiting_leads=waiting_count.scalar() or 0,
        total_sellers=total_sellers.scalar() or 0,
        active_sellers=active_sellers.scalar() or 0,
        conversion_rate=round(conversion, 1),
        seller_stats=seller_stats,
    )
