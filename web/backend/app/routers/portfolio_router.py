import os
import uuid
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import User, PortfolioCategory, PortfolioProject, PortfolioScreenshot
from app.schemas import (
    PortfolioCategoryCreate, PortfolioCategoryUpdate, PortfolioCategoryOut,
    PortfolioProjectCreate, PortfolioProjectUpdate, PortfolioProjectOut,
    PortfolioScreenshotOut
)
from app.auth import get_current_user, require_admin
from app.config import settings

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = ''.join(c if c.isalnum() or c in '-_' else '-' for c in text)
    text = '-'.join(filter(None, text.split('-')))
    return text or str(uuid.uuid4())[:8]


def ensure_upload_dir(category_slug: str, project_slug: str) -> Path:
    upload_path = Path(settings.UPLOAD_DIR) / category_slug / project_slug
    upload_path.mkdir(parents=True, exist_ok=True)
    return upload_path


@router.get("/categories", response_model=list[PortfolioCategoryOut])
async def list_categories(
    db: AsyncSession = Depends(get_db),
    active_only: bool = False,
):
    query = select(PortfolioCategory).order_by(PortfolioCategory.sort_order)
    if active_only:
        query = query.where(PortfolioCategory.is_active == True)
    result = await db.execute(query)
    categories = result.scalars().all()
    
    output = []
    for cat in categories:
        projects_count = len(cat.projects) if cat.projects else 0
        cat_dict = {c.name: getattr(cat, c.name) for c in PortfolioCategory.__table__.columns}
        cat_dict["projects_count"] = projects_count
        output.append(cat_dict)
    return output


@router.get("/categories/{slug}/projects", response_model=list[PortfolioProjectOut])
async def list_category_projects(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(PortfolioCategory).where(PortfolioCategory.slug == slug))
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    
    projects_result = await db.execute(
        select(PortfolioProject)
        .where(PortfolioProject.category_id == category.id)
        .order_by(PortfolioProject.created_at.desc())
    )
    projects = projects_result.scalars().all()
    
    output = []
    for proj in projects:
        proj_dict = {c.name: getattr(proj, c.name) for c in PortfolioProject.__table__.columns}
        proj_dict["screenshots"] = [
            {c.name: getattr(s, c.name) for c in PortfolioScreenshot.__table__.columns}
            for s in (proj.screenshots or [])
        ]
        output.append(proj_dict)
    return output


@router.get("/projects/{slug}", response_model=PortfolioProjectOut)
async def get_project(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(PortfolioProject).where(PortfolioProject.slug == slug))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден")
    
    proj_dict = {c.name: getattr(project, c.name) for c in PortfolioProject.__table__.columns}
    proj_dict["screenshots"] = [
        {c.name: getattr(s, c.name) for c in PortfolioScreenshot.__table__.columns}
        for s in (project.screenshots or [])
    ]
    return proj_dict


@router.post("/categories", response_model=PortfolioCategoryOut)
async def create_category(
    data: PortfolioCategoryCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    slug = slugify(data.name)
    
    existing = await db.execute(select(PortfolioCategory).where(PortfolioCategory.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{slug}-{uuid.uuid4().hex[:4]}"
    
    category = PortfolioCategory(
        name=data.name,
        slug=slug,
        description=data.description,
        sort_order=data.sort_order,
        is_active=data.is_active,
    )
    db.add(category)
    await db.flush()
    
    cat_dict = {c.name: getattr(category, c.name) for c in PortfolioCategory.__table__.columns}
    cat_dict["projects_count"] = 0
    return cat_dict


@router.put("/categories/{category_id}", response_model=PortfolioCategoryOut)
async def update_category(
    category_id: int,
    data: PortfolioCategoryUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(PortfolioCategory).where(PortfolioCategory.id == category_id))
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    
    if data.name is not None:
        category.name = data.name
    if data.description is not None:
        category.description = data.description
    if data.sort_order is not None:
        category.sort_order = data.sort_order
    if data.is_active is not None:
        category.is_active = data.is_active
    
    await db.flush()
    
    projects_count = len(category.projects) if category.projects else 0
    cat_dict = {c.name: getattr(category, c.name) for c in PortfolioCategory.__table__.columns}
    cat_dict["projects_count"] = projects_count
    return cat_dict


@router.delete("/categories/{category_id}")
async def delete_category(
    category_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(PortfolioCategory).where(PortfolioCategory.id == category_id))
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    
    for project in category.projects:
        for screenshot in project.screenshots:
            file_path = Path(screenshot.file_path)
            if file_path.exists():
                file_path.unlink()
            await db.delete(screenshot)
        await db.delete(project)
    
    await db.delete(category)
    await db.flush()
    return {"ok": True}


@router.post("/projects", response_model=PortfolioProjectOut)
async def create_project(
    data: PortfolioProjectCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(PortfolioCategory).where(PortfolioCategory.id == data.category_id))
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    
    slug = slugify(data.name)
    existing = await db.execute(select(PortfolioProject).where(PortfolioProject.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{slug}-{uuid.uuid4().hex[:4]}"
    
    project = PortfolioProject(
        category_id=data.category_id,
        name=data.name,
        slug=slug,
        description=data.description,
        client_name=data.client_name,
    )
    db.add(project)
    await db.flush()
    
    proj_dict = {c.name: getattr(project, c.name) for c in PortfolioProject.__table__.columns}
    proj_dict["screenshots"] = []
    return proj_dict


@router.put("/projects/{project_id}", response_model=PortfolioProjectOut)
async def update_project(
    project_id: int,
    data: PortfolioProjectUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(PortfolioProject).where(PortfolioProject.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден")
    
    if data.name is not None:
        project.name = data.name
    if data.description is not None:
        project.description = data.description
    if data.client_name is not None:
        project.client_name = data.client_name
    
    await db.flush()
    
    proj_dict = {c.name: getattr(project, c.name) for c in PortfolioProject.__table__.columns}
    proj_dict["screenshots"] = [
        {c.name: getattr(s, c.name) for c in PortfolioScreenshot.__table__.columns}
        for s in (project.screenshots or [])
    ]
    return proj_dict


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(PortfolioProject).where(PortfolioProject.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найдена")
    
    for screenshot in project.screenshots:
        file_path = Path(screenshot.file_path)
        if file_path.exists():
            file_path.unlink()
        await db.delete(screenshot)
    
    await db.delete(project)
    await db.flush()
    return {"ok": True}


@router.post("/projects/{project_id}/screenshots", response_model=list[PortfolioScreenshotOut])
async def upload_screenshots(
    project_id: int,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(PortfolioProject).where(PortfolioProject.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден")
    
    category_result = await db.execute(select(PortfolioCategory).where(PortfolioCategory.id == project.category_id))
    category = category_result.scalar_one()
    
    upload_path = ensure_upload_dir(category.slug, project.slug)
    
    max_order_result = await db.execute(
        select(func.max(PortfolioScreenshot.sort_order)).where(PortfolioScreenshot.project_id == project_id)
    )
    max_order = max_order_result.scalar() or 0
    
    uploaded = []
    for i, file in enumerate(files):
        if not file.content_type or not file.content_type.startswith("image/"):
            continue
        
        ext = Path(file.filename).suffix or ".jpg"
        filename = f"{uuid.uuid4().hex}{ext}"
        file_path = upload_path / filename
        
        content = await file.read()
        file_path.write_bytes(content)
        
        screenshot = PortfolioScreenshot(
            project_id=project_id,
            filename=filename,
            original_filename=file.filename or "",
            file_path=str(file_path),
            sort_order=max_order + i + 1,
        )
        db.add(screenshot)
        uploaded.append(screenshot)
    
    await db.flush()
    
    return [
        {c.name: getattr(s, c.name) for c in PortfolioScreenshot.__table__.columns}
        for s in uploaded
    ]


@router.delete("/screenshots/{screenshot_id}")
async def delete_screenshot(
    screenshot_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(PortfolioScreenshot).where(PortfolioScreenshot.id == screenshot_id))
    screenshot = result.scalar_one_or_none()
    if not screenshot:
        raise HTTPException(status_code=404, detail="Скриншот не найден")
    
    file_path = Path(screenshot.file_path)
    if file_path.exists():
        file_path.unlink()
    
    await db.delete(screenshot)
    await db.flush()
    return {"ok": True}


@router.get("/uploads/{category_slug}/{project_slug}/{filename}")
async def serve_screenshot(
    category_slug: str,
    project_slug: str,
    filename: str,
):
    file_path = Path(settings.UPLOAD_DIR) / category_slug / project_slug / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(file_path)
