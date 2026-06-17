from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import GeneratedMessage, Lead
from .outreach import build_user_prompt, system_prompt


logger = logging.getLogger(__name__)

_DEEPSEEK_TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: int = 120,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def generate_outreach(
        self,
        lead: Lead,
        signature: str = "",
        unsubscribe_text: str = "",
    ) -> GeneratedMessage:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt()},
                {
                    "role": "user",
                    "content": build_user_prompt(lead, signature, unsubscribe_text),
                },
            ],
            "temperature": 0.7,
        }
        data = post_json(
            f"{self.base_url}/chat/completions",
            payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        content = data["choices"][0]["message"]["content"]
        parsed = parse_json_response(content)
        return GeneratedMessage(
            lead=lead,
            subject=parsed.get("subject", "").strip() or fallback_subject(lead),
            message=parsed.get("message", "").strip(),
            model=self.model,
        )


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    max_attempts = max(1, int(os.environ.get("DEEPSEEK_MAX_RETRIES", "10")))

    for attempt in range(1, max_attempts + 1):
        request = Request(url, data=body_bytes, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            code = int(exc.code or 0)
            if code in _DEEPSEEK_TRANSIENT_HTTP and attempt < max_attempts:
                delay = min(2 ** (attempt - 1), 90)
                logger.warning(
                    "DeepSeek HTTP %s, попытка %s/%s — пауза %ss",
                    code,
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"DeepSeek API request failed with HTTP {code}: {body}") from exc
        except URLError as exc:
            if attempt < max_attempts:
                delay = min(2 ** (attempt - 1), 75)
                logger.warning(
                    "DeepSeek сеть (%s/%s): %s — пауза %ss",
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"DeepSeek API network error: {exc}") from exc
        except TimeoutError as exc:
            if attempt < max_attempts:
                delay = min(2 ** (attempt - 1), 75)
                logger.warning(
                    "DeepSeek timeout (%s/%s), пауза %ss",
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"DeepSeek API timeout: {exc}") from exc
        except OSError as exc:
            if attempt < max_attempts:
                delay = min(2 ** (attempt - 1), 75)
                logger.warning(
                    "DeepSeek сокет (%s/%s): %s — пауза %ss",
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
                continue
            raise


def parse_json_response(content: str) -> dict[str, str]:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()

    try:
        data: Any = json.loads(text)
        if isinstance(data, dict):
            return {
                "subject": str(data.get("subject", "")).strip(),
                "message": str(data.get("message", "")).strip(),
            }
    except json.JSONDecodeError:
        pass

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return {"subject": "", "message": ""}
    if len(lines) == 1:
        return {"subject": "", "message": lines[0]}
    return {"subject": lines[0].removeprefix("Тема:").strip(), "message": "\n".join(lines[1:]).strip()}


def fallback_subject(lead: Lead) -> str:
    name = lead.name or "вашей организации"
    return f"Сайт для {name}"
