from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from .config import env, require_env
from .models import GeneratedMessage, utc_now_iso


@dataclass(slots=True)
class SMTPConfig:
    host: str
    port: int
    username: str
    password: str
    mail_from: str
    reply_to: str = ""

    @classmethod
    def from_env(cls) -> "SMTPConfig":
        return cls(
            host=require_env("SMTP_HOST"),
            port=int(env("SMTP_PORT", "587")),
            username=require_env("SMTP_USER"),
            password=require_env("SMTP_PASSWORD"),
            mail_from=require_env("MAIL_FROM"),
            reply_to=env("MAIL_REPLY_TO"),
        )


def send_email(config: SMTPConfig, item: GeneratedMessage) -> GeneratedMessage:
    recipient = item.lead.email.strip()
    if not recipient:
        item.send_status = "skipped_no_email"
        return item

    message = EmailMessage()
    message["From"] = config.mail_from
    message["To"] = recipient
    message["Subject"] = item.subject
    if config.reply_to:
        message["Reply-To"] = config.reply_to
    message.set_content(item.message)

    with smtplib.SMTP(config.host, config.port, timeout=60) as server:
        server.starttls()
        server.login(config.username, config.password)
        server.send_message(message)

    item.sent_at = utc_now_iso()
    item.send_status = "sent"
    return item
