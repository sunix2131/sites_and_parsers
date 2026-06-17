from __future__ import annotations

from dataclasses import asdict, dataclass

from . import settings


@dataclass(frozen=True, slots=True)
class RunModeConfig:
    key: str
    label: str
    parallel_org_tabs: int
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
    no_site_scan_multiplier: int
    no_site_scan_min_extra: int
    no_site_scan_max_cards: int
    website_platform_audit: int = 0

    def parser_settings(self) -> dict[str, float | int | str]:
        values = asdict(self)
        return {
            "PARALLEL_ORG_TABS": values["parallel_org_tabs"],
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
            "CAPTCHA_RETRY_MAX": values["captcha_retry_max"],
            "NO_SITE_SCAN_MULTIPLIER": values["no_site_scan_multiplier"],
            "NO_SITE_SCAN_MIN_EXTRA": values["no_site_scan_min_extra"],
            "NO_SITE_SCAN_MAX_CARDS": values["no_site_scan_max_cards"],
            "WEBSITE_PLATFORM_AUDIT": values["website_platform_audit"],
        }


SAFE_MODE = RunModeConfig(
    key="safe",
    label="Безопасный",
    parallel_org_tabs=1,
    page_delay=8.0,
    page_delay_jitter=8.0,
    cooldown_after=25,
    cooldown_seconds=90.0,
    scroll_step_px=260,
    scroll_pause_ms=900,
    website_recheck_attempts=2,
    website_recheck_delay_ms=1500,
    captcha_retry_seconds=900,
    captcha_retry_max=5,
    no_site_scan_multiplier=16,
    no_site_scan_min_extra=80,
    no_site_scan_max_cards=1000,
)

NORMAL_MODE = RunModeConfig(
    key="normal",
    label="Нормальный",
    parallel_org_tabs=2,
    page_delay=4.0,
    page_delay_jitter=4.0,
    cooldown_after=0,
    cooldown_seconds=0.0,
    scroll_step_px=390,
    scroll_pause_ms=450,
    website_recheck_attempts=1,
    website_recheck_delay_ms=700,
    captcha_retry_seconds=900,
    captcha_retry_max=3,
    no_site_scan_multiplier=12,
    no_site_scan_min_extra=60,
    no_site_scan_max_cards=800,
)

FAST_MODE = RunModeConfig(
    key="fast",
    label="Быстрый",
    parallel_org_tabs=3,
    page_delay=2.0,
    page_delay_jitter=2.0,
    cooldown_after=0,
    cooldown_seconds=0.0,
    scroll_step_px=520,
    scroll_pause_ms=250,
    website_recheck_attempts=1,
    website_recheck_delay_ms=450,
    captcha_retry_seconds=900,
    captcha_retry_max=2,
    no_site_scan_multiplier=8,
    no_site_scan_min_extra=40,
    no_site_scan_max_cards=500,
)

LONG_MODE = RunModeConfig(
    key="long",
    label="Долгий",
    parallel_org_tabs=1,
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
    no_site_scan_multiplier=20,
    no_site_scan_min_extra=100,
    no_site_scan_max_cards=1500,
    website_platform_audit=1,
)

RUN_MODES = {mode.key: mode for mode in (SAFE_MODE, NORMAL_MODE, FAST_MODE, LONG_MODE)}


def get_run_mode(key: str) -> RunModeConfig:
    return RUN_MODES.get((key or "").strip().casefold(), NORMAL_MODE)


def apply_run_profile(key: str) -> RunModeConfig:
    """Apply a parser profile to the mutable settings module for the current process."""
    mode = get_run_mode(key)
    settings.PARALLEL_ORG_TABS = mode.parallel_org_tabs
    settings.PAGE_DELAY = mode.page_delay
    for name, value in mode.parser_settings().items():
        setattr(settings, name, value)
    return mode
