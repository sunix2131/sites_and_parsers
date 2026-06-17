from __future__ import annotations

import asyncio
import csv
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from . import settings
from .models import Lead
from .storage import (
    append_leads_unique_csv,
    append_processed_leads_csv,
    normalize_url_identity,
    read_processed_yandex_urls,
    write_leads_csv,
)
from .yandex_maps import (
    _PlaywrightProxyPool,
    _chromium_browser_launch_plan,
    _new_browser_context,
    accept_cookies,
    canonical_org_url,
    collect_org_urls,
    is_yandex_maps_org_url,
    open_maps_search_page,
    scrape_yandex_org_url,
    unique,
)


@dataclass(slots=True)
class LinkCollectResult:
    query: str
    location: str
    urls: list[str]
    urls_path: Path


@dataclass(slots=True)
class LinkCheckResult:
    input_path: Path
    leads: list[Lead]
    leads_path: Path
    processed_path: Path
    checked: int
    skipped_existing: int
    failed: int


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", value).strip("_")[:60] or "links"


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def org_urls_path(output_dir: Path, query: str, location: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    label = _slug("_".join(part for part in (location, query) if part))
    return output_dir / f"org_urls_{label}_{_timestamp()}.csv"


def checked_leads_path(output_dir: Path, label: str = "checked") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"leads_{_slug(label)}_{_timestamp()}.csv"


def write_org_urls_csv(path: Path, urls: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["url"])
        writer.writeheader()
        for url in urls:
            writer.writerow({"url": url})


def read_org_urls(path: Path) -> list[str]:
    """Read org URLs from CSV with url/yandex_url column or from plain text."""
    if not path.exists():
        raise FileNotFoundError(path)

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if not text.strip():
        return []

    first_line = text.splitlines()[0].casefold()
    if "," in first_line and any(column in first_line for column in ("url", "yandex_url")):
        with path.open("r", newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            urls = []
            for row in reader:
                raw = str(row.get("url") or row.get("yandex_url") or "").strip()
                if raw and is_yandex_maps_org_url(raw):
                    urls.append(canonical_org_url(raw))
            return unique(urls)

    urls = re.findall(r"https?://[^\s,;]+", text)
    return unique(canonical_org_url(url) for url in urls if is_yandex_maps_org_url(url))


def collect_yandex_org_links(
    *,
    query: str,
    location: str,
    limit: int,
    output_dir: Path,
    headless: bool = True,
    log=print,
) -> LinkCollectResult:
    urls = asyncio.run(
        _collect_yandex_org_links_async(
            query=query,
            location=location,
            limit=limit,
            headless=headless,
            log=log,
        )
    )
    path = org_urls_path(output_dir, query=query, location=location)
    write_org_urls_csv(path, urls)
    return LinkCollectResult(query=query, location=location, urls=urls, urls_path=path)


async def _collect_yandex_org_links_async(
    *,
    query: str,
    location: str,
    limit: int,
    headless: bool,
    log,
) -> list[str]:
    if not query.strip():
        raise ValueError("query is required")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run: pip install -r requirements.txt") from exc

    search_text = " ".join(part for part in (location.strip(), query.strip()) if part)
    search_url = f"{settings.YANDEX_MAPS_ORIGIN.rstrip('/')}/maps/?text={quote_plus(search_text)}"
    launch_kwargs, internal_proxy, pool_proxies = _chromium_browser_launch_plan(
        headless=headless,
        logger=log,
    )
    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            proxy_pool = _PlaywrightProxyPool(browser, pool_proxies, logger=log) if pool_proxies else None
            context = None if proxy_pool else await _new_browser_context(browser)
            page_context = await proxy_pool.context_for_card(1) if proxy_pool else context
            if page_context is None:
                raise RuntimeError("Не удалось открыть контекст Chromium.")
            page = await page_context.new_page()
            try:
                log(f"Открываю Яндекс Карты для сбора ссылок: {search_text}")
                await open_maps_search_page(page, search_url, log, search_text_fallback=search_text)
                await accept_cookies(page)
                await page.wait_for_timeout(900)
                urls = await collect_org_urls(page, limit=limit, log=log)
                return unique(canonical_org_url(url) for url in urls if is_yandex_maps_org_url(url))
            finally:
                await page.close()
                if proxy_pool:
                    await proxy_pool.close()
                await browser.close()
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if internal_proxy:
            internal_proxy.shutdown()
            internal_proxy.server_close()


def check_yandex_org_links(
    *,
    input_path: Path,
    output_dir: Path,
    processed_file: Path | None = None,
    headless: bool = True,
    delay_seconds: float = 0.0,
    contact_filter: str = "all",
    only_no_site: bool = False,
    log=print,
) -> LinkCheckResult:
    urls = read_org_urls(input_path)
    processed_path = processed_file or output_dir / "processed_checked_links.csv"
    processed_urls = read_processed_yandex_urls(processed_path)
    leads: list[Lead] = []
    checked = 0
    skipped_existing = 0
    failed = 0

    for index, url in enumerate(urls, start=1):
        identity = normalize_url_identity(canonical_org_url(url))
        if identity in processed_urls:
            skipped_existing += 1
            log(f"[{index}/{len(urls)}] Уже обработано, пропускаю: {url}")
            continue
        try:
            log(f"[{index}/{len(urls)}] Проверяю карточку: {url}")
            lead = scrape_yandex_org_url(url, headless=headless, log=log, light_parse=False)
            checked += 1
            append_processed_leads_csv(processed_path, [lead])
            processed_urls.add(identity)
            if only_no_site and not lead.website_absent_verified:
                continue
            if not lead.matches_contact_filter(contact_filter):
                continue
            leads.append(lead)
            if lead.website_absent_verified:
                append_leads_unique_csv(output_dir / "no_site_leads.csv", [lead])
        except Exception as exc:
            failed += 1
            log(f"[{index}/{len(urls)}] Ошибка проверки: {exc}")
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    path = checked_leads_path(output_dir, input_path.stem)
    write_leads_csv(path, leads)
    return LinkCheckResult(
        input_path=input_path,
        leads=leads,
        leads_path=path,
        processed_path=processed_path,
        checked=checked,
        skipped_existing=skipped_existing,
        failed=failed,
    )
