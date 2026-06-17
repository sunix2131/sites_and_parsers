from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class RunModeConfig:
    key: str
    label: str
    page_delay: float
    page_delay_jitter: float
    cooldown_after: int
    cooldown_seconds: float
    scroll_step_px: int
    scroll_pause_ms: int
    website_recheck_attempts: int
    website_recheck_delay_ms: int
    captcha_retry_seconds: int
    captcha_retry_max: int

    def parser_settings(self) -> dict[str, float | int | str]:
        values = asdict(self)
        return {
            "PAGE_DELAY_JITTER_SECONDS": values["page_delay_jitter"],
            "CARD_COOLDOWN_AFTER": values["cooldown_after"],
            "CARD_COOLDOWN_SECONDS": values["cooldown_seconds"],
            "SEARCH_SCROLL_STEP_PX": values["scroll_step_px"],
            "SEARCH_SCROLL_PAUSE_MS": values["scroll_pause_ms"],
            "WEBSITE_RECHECK_ATTEMPTS": values["website_recheck_attempts"],
            "WEBSITE_RECHECK_DELAY_MS": values["website_recheck_delay_ms"],
            "LONG_POOL_MODE": 1 if self.key == "long" else 0,
            "LONG_POOL_SEARCHES": 4,
            "LONG_INITIAL_SEARCH_LINKS": 100,
            "LONG_NEXT_SEARCH_LINKS": 50,
            "CAPTCHA_RETRY_SECONDS": values["captcha_retry_seconds"],
        }


FAST_MODE = RunModeConfig(
    key="fast",
    label="Быстрый",
    page_delay=5.0,
    page_delay_jitter=7.0,
    cooldown_after=0,
    cooldown_seconds=0.0,
    scroll_step_px=390,
    scroll_pause_ms=450,
    website_recheck_attempts=1,
    website_recheck_delay_ms=700,
    captcha_retry_seconds=900,
    captcha_retry_max=3,
)

LONG_MODE = RunModeConfig(
    key="long",
    label="Долгий",
    page_delay=30.0,
    page_delay_jitter=3.0,
    cooldown_after=20,
    cooldown_seconds=120.0,
    scroll_step_px=180,
    scroll_pause_ms=1800,
    website_recheck_attempts=2,
    website_recheck_delay_ms=2500,
    captcha_retry_seconds=600,
    captcha_retry_max=100,
)

RUN_MODES = {mode.key: mode for mode in (FAST_MODE, LONG_MODE)}


def get_run_mode(key: str) -> RunModeConfig:
    return RUN_MODES.get(key, FAST_MODE)
