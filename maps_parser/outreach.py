from __future__ import annotations

import json

from . import settings
from .models import Lead


def system_prompt() -> str:
    return settings.OUTREACH_SYSTEM_PROMPT


def build_user_prompt(lead: Lead, signature: str = "", unsubscribe_text: str = "") -> str:
    lead_payload = {
        "name": lead.name,
        "categories": lead.categories,
        "address": lead.address,
        "phone": lead.phone,
        "email": lead.email,
        "website": lead.website,
        "social_links": lead.social_links,
        "rating": lead.rating,
        "reviews": lead.reviews,
        "hours": lead.hours,
    }
    return settings.OUTREACH_USER_PROMPT.format(
        lead_json=json.dumps(lead_payload, ensure_ascii=False, indent=2),
        sender_name=settings.OUTREACH_SENDER_NAME,
        signature=signature,
        unsubscribe_text=unsubscribe_text,
    )
