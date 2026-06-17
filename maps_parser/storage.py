from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Iterable

from .models import GeneratedMessage, Lead, utc_now_iso


LEAD_COLUMNS = [
    "name",
    "categories",
    "address",
    "phone",
    "email",
    "website",
    "website_platform",
    "website_status",
    "contact_status",
    "social_links",
    "rating",
    "reviews",
    "hours",
    "yandex_url",
    "source",
    "scraped_at",
]

PROCESSED_LEAD_COLUMNS = [
    "key",
    "name",
    "address",
    "phone",
    "website",
    "website_platform",
    "website_status",
    "contact_status",
    "yandex_url",
    "source",
    "processed_at",
]


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def normalize_identity_part(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def normalize_url_identity(value: str) -> str:
    normalized = normalize_identity_part(value)
    return normalized.rstrip("/")


def lead_identity_key(lead: Lead) -> str:
    if lead.yandex_url:
        return f"yandex_url:{normalize_url_identity(lead.yandex_url)}"
    if lead.website:
        return f"website:{normalize_url_identity(lead.website)}"

    name = normalize_identity_part(lead.name)
    address = normalize_identity_part(lead.address)
    phone = normalize_identity_part(lead.phone)
    if name and (address or phone):
        return f"lead:{name}|{address}|{phone}"
    return ""


def write_leads_csv(path: Path, leads: Iterable[Lead]) -> None:
    ensure_parent(path)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=LEAD_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            writer.writerow(lead.to_dict())


def read_leads_csv(path: Path) -> list[Lead]:
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        return [Lead.from_mapping(row) for row in csv.DictReader(file)]


def read_processed_lead_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        return {
            str(row.get("key", "")).strip()
            for row in csv.DictReader(file)
            if str(row.get("key", "")).strip()
        }


def read_processed_yandex_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        return {
            normalize_url_identity(str(row.get("yandex_url", "")))
            for row in csv.DictReader(file)
            if str(row.get("yandex_url", "")).strip()
        }


def append_processed_leads_csv(path: Path, leads: Iterable[Lead]) -> int:
    ensure_parent(path)
    existing_keys = read_processed_lead_keys(path)
    rows: list[dict[str, str]] = []

    for lead in leads:
        key = lead_identity_key(lead)
        if not key or key in existing_keys:
            continue
        existing_keys.add(key)
        rows.append(
            {
                "key": key,
                "name": lead.name,
                "address": lead.address,
                "phone": lead.phone,
                "website": lead.website,
                "website_platform": str(lead.raw.get("website_platform", "")),
                "website_status": lead.website_status,
                "contact_status": lead.contact_status,
                "yandex_url": lead.yandex_url,
                "source": lead.source,
                "processed_at": utc_now_iso(),
            }
        )

    if not rows:
        return 0

    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=PROCESSED_LEAD_COLUMNS, extrasaction="ignore")
        if needs_header:
            writer.writeheader()
        writer.writerows(rows)

    return len(rows)


def append_leads_unique_csv(path: Path, leads: Iterable[Lead]) -> int:
    ensure_parent(path)
    existing: set[str] = set()
    if path.exists():
        with path.open("r", newline="", encoding="utf-8-sig") as file:
            for row in csv.DictReader(file):
                url = normalize_url_identity(str(row.get("yandex_url", "")))
                if url:
                    existing.add(url)

    rows: list[dict[str, object]] = []
    for lead in leads:
        identity = normalize_url_identity(lead.yandex_url)
        if not identity or identity in existing:
            continue
        existing.add(identity)
        data = lead.to_dict()
        data["website_platform"] = str(lead.raw.get("website_platform", ""))
        rows.append(data)

    if not rows:
        return 0
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=LEAD_COLUMNS, extrasaction="ignore")
        if needs_header:
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def write_messages_jsonl(path: Path, messages: Iterable[GeneratedMessage]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8") as file:
        for item in messages:
            file.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")


def read_messages_jsonl(path: Path) -> list[GeneratedMessage]:
    messages: list[GeneratedMessage] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            payload = json.loads(line)
            lead = Lead.from_mapping(payload.get("lead", {}))
            messages.append(
                GeneratedMessage(
                    lead=lead,
                    subject=str(payload.get("subject", "")).strip(),
                    message=str(payload.get("message", "")).strip(),
                    provider=str(payload.get("provider", "deepseek")).strip(),
                    model=str(payload.get("model", "")).strip(),
                    generated_at=str(payload.get("generated_at", "")).strip(),
                    sent_at=str(payload.get("sent_at", "")).strip(),
                    send_status=str(payload.get("send_status", "draft")).strip() or "draft",
                )
            )
    return messages
