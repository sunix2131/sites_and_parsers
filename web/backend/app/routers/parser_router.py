import asyncio
import logging
import subprocess
import sys
import threading
import uuid
import csv
import glob
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.auth import require_admin, get_current_user
from app.models import User, Lead
from app.config import settings
from app.database import async_session

router = APIRouter(prefix="/api/parser", tags=["parser"])
logger = logging.getLogger(__name__)

tasks = {}
MAX_TASK_OUTPUT_CHARS = 200_000


class ParserRunRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    location: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=20, ge=1, le=1000)
    mode: Literal["scrape", "run"] = "scrape"


class TaskResponse(BaseModel):
    task_id: str
    status: str
    started_at: str
    mode: Literal["scrape", "run"]
    output: Optional[str] = None


async def import_csv_leads(parser_dir: Path):
    csv_files = glob.glob(str(parser_dir / "out" / "*.csv"))

    for csv_file in csv_files:
        try:
            with open(csv_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            async with async_session() as db:
                for row in rows:
                    phone = row.get("phone", "")
                    if not phone:
                        continue

                    result = await db.execute(
                        select(Lead).where(Lead.phone == phone)
                    )
                    existing = result.scalar_one_or_none()
                    if existing:
                        continue

                    categories = row.get("categories", "")
                    if isinstance(categories, str):
                        categories = [c.strip() for c in categories.split(",") if c.strip()]

                    social_links = row.get("social_links", "")
                    if isinstance(social_links, str):
                        social_links = [s.strip() for s in social_links.split(",") if s.strip()]

                    scraped_at = None
                    if row.get("scraped_at"):
                        try:
                            scraped_at = datetime.fromisoformat(row["scraped_at"])
                        except (ValueError, TypeError):
                            pass

                    lead = Lead(
                        name=row.get("name", ""),
                        categories=categories,
                        address=row.get("address", ""),
                        phone=phone,
                        email=row.get("email", ""),
                        website=row.get("website", ""),
                        website_status=row.get("website_status", "unknown"),
                        website_platform=row.get("website_platform", ""),
                        social_links=social_links,
                        rating=row.get("rating", ""),
                        reviews=row.get("reviews", ""),
                        hours=row.get("hours", ""),
                        yandex_url=row.get("yandex_url", ""),
                        source="parser",
                        scraped_at=scraped_at,
                    )
                    db.add(lead)

                await db.commit()
        except Exception:
            logger.exception("Failed to import parser output from %s", csv_file)


def run_parser_task(task_id: str, query: str, location: str, limit: int, mode: str):
    parser_dir = Path(settings.PARSER_DIR)
    python_path = parser_dir / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python_path.is_file():
        tasks[task_id]["status"] = "error"
        tasks[task_id]["output"] = f"Parser interpreter not found: {python_path}"
        return

    cmd = [
        str(python_path),
        "run.py",
        mode,
        "--query", query,
        "--location", location,
        "--limit", str(limit),
    ]

    tasks[task_id]["status"] = "running"

    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(parser_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        tasks[task_id]["process"] = process

        output_lines = []
        output_size = 0
        for line in process.stdout:
            output_lines.append(line)
            output_size += len(line)
            while output_size > MAX_TASK_OUTPUT_CHARS and output_lines:
                output_size -= len(output_lines.pop(0))
            tasks[task_id]["output"] = "".join(output_lines)

        process.wait()

        if process.returncode == 0:
            tasks[task_id]["status"] = "completed"
            loop = asyncio.new_event_loop()
            loop.run_until_complete(import_csv_leads(parser_dir))
            loop.close()
        else:
            tasks[task_id]["status"] = "failed"

    except Exception as e:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["output"] = str(e)


@router.post("/run", response_model=TaskResponse)
async def run_parser(
    request: ParserRunRequest,
    current_user: User = Depends(require_admin),
):
    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "pending",
        "output": "",
        "started_at": datetime.now().isoformat(),
        "mode": request.mode,
        "process": None,
    }

    thread = threading.Thread(
        target=run_parser_task,
        args=(task_id, request.query, request.location, request.limit, request.mode),
        daemon=True,
    )
    thread.start()

    return TaskResponse(
        task_id=task_id,
        status="pending",
        started_at=tasks[task_id]["started_at"],
        mode=request.mode,
    )


@router.get("/tasks", response_model=list[TaskResponse])
async def list_tasks(current_user: User = Depends(get_current_user)):
    return [
        TaskResponse(
            task_id=task_id,
            status=task["status"],
            started_at=task["started_at"],
            mode=task["mode"],
            output=None,
        )
        for task_id, task in tasks.items()
    ]


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, current_user: User = Depends(get_current_user)):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[task_id]
    return TaskResponse(
        task_id=task_id,
        status=task["status"],
        started_at=task["started_at"],
        mode=task["mode"],
        output=task["output"],
    )


@router.post("/stop/{task_id}")
async def stop_task(task_id: str, current_user: User = Depends(require_admin)):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[task_id]
    process = task.get("process")

    if not process or task["status"] != "running":
        raise HTTPException(status_code=400, detail="Task is not running")

    process.terminate()
    task["status"] = "stopped"

    return {"message": "Task stopped"}
