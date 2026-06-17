from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def split_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item.strip() for item in value if item and item.strip()]
    return [item.strip() for item in value.replace("|", ";").split(";") if item.strip()]


@dataclass(slots=True)
class Lead:
    name: str
    categories: list[str] = field(default_factory=list)
    address: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    website_status: str = "absent"
    contact_status: str = "unknown"
    social_links: list[str] = field(default_factory=list)
    rating: str = ""
    reviews: str = ""
    hours: str = ""
    yandex_url: str = ""
    source: str = "yandex_maps"
    scraped_at: str = field(default_factory=utc_now_iso)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_website(self) -> bool:
        return bool((self.website or "").strip())

    @property
    def website_absent_verified(self) -> bool:
        return not self.has_website and (self.website_status or "").strip().casefold() == "absent"

    @property
    def has_phone(self) -> bool:
        return bool((self.phone or "").strip())

    @property
    def has_any_contact(self) -> bool:
        return bool(
            (self.phone or "").strip()
            or (self.email or "").strip()
            or self.social_links
        )

    def matches_contact_filter(self, mode: str) -> bool:
        normalized = (mode or "phone").strip().casefold()
        if normalized == "all":
            return True
        if normalized == "any":
            return self.has_any_contact
        return self.has_phone

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "Lead":
        return cls(
            name=str(data.get("name", "")).strip(),
            categories=split_list(data.get("categories")),
            address=str(data.get("address", "")).strip(),
            phone=str(data.get("phone", "")).strip(),
            email=str(data.get("email", "")).strip(),
            website=str(data.get("website", "")).strip(),
            website_status=str(data.get("website_status", "")).strip() or ("present" if str(data.get("website", "")).strip() else "unknown"),
            contact_status=str(data.get("contact_status", "")).strip() or "unknown",
            social_links=split_list(data.get("social_links")),
            rating=str(data.get("rating", "")).strip(),
            reviews=str(data.get("reviews", "")).strip(),
            hours=str(data.get("hours", "")).strip(),
            yandex_url=str(data.get("yandex_url", "")).strip(),
            source=str(data.get("source", "yandex_maps")).strip() or "yandex_maps",
            scraped_at=str(data.get("scraped_at", "")).strip() or utc_now_iso(),
            raw=data.get("raw") if isinstance(data.get("raw"), dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["categories"] = "; ".join(self.categories)
        data["social_links"] = "; ".join(self.social_links)
        data["raw"] = self.raw
        return data


@dataclass(slots=True)
class GeneratedMessage:
    lead: Lead
    subject: str
    message: str
    provider: str = "deepseek"
    model: str = ""
    generated_at: str = field(default_factory=utc_now_iso)
    sent_at: str = ""
    send_status: str = "draft"

    def to_dict(self) -> dict[str, Any]:
        return {
            "lead": self.lead.to_dict(),
            "subject": self.subject,
            "message": self.message,
            "provider": self.provider,
            "model": self.model,
            "generated_at": self.generated_at,
            "sent_at": self.sent_at,
            "send_status": self.send_status,
        }
