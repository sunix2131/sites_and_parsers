from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import env, require_env
from .deepseek import DeepSeekClient
from .email_sender import SMTPConfig, send_email
from .models import GeneratedMessage, Lead
from .storage import (
    append_processed_leads_csv,
    append_leads_unique_csv,
    lead_identity_key,
    normalize_url_identity,
    read_processed_lead_keys,
    read_processed_yandex_urls,
    write_leads_csv,
    write_messages_jsonl,
)
from . import settings
from .yandex_maps import (
    MapsScrapeJob,
    canonical_org_url,
    is_yandex_service_lead,
    scrape_yandex_maps,
    scrape_yandex_maps_batch,
    scrape_yandex_org_url,
)


LogFn = Callable[[str], None]
OnGeneratedFn = Callable[[GeneratedMessage], None]
OnLeadCheckedFn = Callable[[Lead], None]
ShouldStopFn = Callable[[], bool]


def noop_log(_: str) -> None:
    return None


def lead_matches_output_target(lead: Lead, contact_filter: str, target_mode: str) -> bool:
    redesign = str(lead.raw.get("website_platform", "")).casefold() in {
        "tilda",
        "wordpress",
        "yandex_business",
    }
    if target_mode == "redesign":
        target = redesign
    elif target_mode == "combined":
        target = lead.website_absent_verified or redesign
    else:
        target = lead.website_absent_verified
    return target and lead.matches_contact_filter(contact_filter)


@dataclass(slots=True)
class LeadJobResult:
    query: str
    location: str
    leads: list[Lead]
    no_site_leads: list[Lead]
    messages: list[GeneratedMessage]
    leads_path: Path
    processed_path: Path
    skipped_existing: int = 0
    messages_path: Path | None = None


@dataclass(slots=True)
class LeadJobSpec:
    query: str
    location: str
    limit: int
    search_url: str = ""
    priority_urls: list[str] | None = None
    group_key: str = ""


def output_path(output_dir: Path, prefix: str, suffix: str, label: str = "") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    slug = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", label).strip("_")[:40]
    label_part = f"_{slug}" if slug else ""
    return output_dir / f"{prefix}{label_part}_{stamp}.{suffix}"


def processed_file_path(output_dir: Path, processed_file: Path | None = None) -> Path:
    return processed_file or output_dir / "processed_leads.csv"


def target_registry_paths(output_dir: Path, target_mode: str) -> list[Path]:
    no_site = output_dir / "processed_no_site.csv"
    redesign = output_dir / "processed_redesign.csv"
    if target_mode == "redesign":
        return [redesign]
    if target_mode == "combined":
        return [no_site, redesign]
    return [no_site]


def target_skip_urls(output_dir: Path, target_mode: str) -> set[str]:
    no_site = read_processed_yandex_urls(output_dir / "processed_no_site.csv")
    redesign = read_processed_yandex_urls(output_dir / "processed_redesign.csv")
    legacy = read_processed_yandex_urls(output_dir / "processed_leads.csv")
    if target_mode == "redesign":
        return redesign
    if target_mode == "combined":
        return (no_site | legacy) & redesign
    return no_site | legacy


def filter_new_leads(leads: list[Lead], processed_keys: set[str]) -> tuple[list[Lead], int]:
    new_leads: list[Lead] = []
    skipped = 0
    current_keys = set(processed_keys)

    for lead in leads:
        key = lead_identity_key(lead)
        if key and key in current_keys:
            skipped += 1
            continue
        if key:
            current_keys.add(key)
        new_leads.append(lead)

    return new_leads, skipped


def filter_collectable_leads(leads: list[Lead]) -> tuple[list[Lead], int]:
    collectable = [
        lead
        for lead in leads
        if lead.name.strip() and not is_yandex_service_lead(lead)
    ]
    return collectable, len(leads) - len(collectable)


def _deepseek_parallel_workers() -> int:
    return settings.DEEPSEEK_PARALLEL_WORKERS


def _finalize_lead_job(
    *,
    query: str,
    location: str,
    leads: list[Lead],
    processed_keys: set[str],
    processed_path: Path,
    output_dir: Path,
    generate: bool,
    log: LogFn,
    on_message_generated: OnGeneratedFn | None,
    contact_filter: str,
    target_no_site_limit: int | None = None,
    target_mode: str = "no_site",
) -> LeadJobResult:
    leads, skipped_service = filter_collectable_leads(leads)
    if skipped_service:
        log(f"Служебных/пустых страниц Яндекса пропущено: {skipped_service}.")
    leads, skipped_after_scrape = filter_new_leads(leads, processed_keys)

    leads_path = output_path(output_dir, "leads", "csv", query)
    write_leads_csv(leads_path, leads)

    no_site_leads = [
        lead
        for lead in leads
        if lead_matches_output_target(lead, contact_filter, target_mode)
    ]
    if target_no_site_limit is not None:
        no_site_leads = no_site_leads[: max(0, target_no_site_limit)]
    if skipped_after_scrape:
        log(f"Повторно найдено и пропущено после проверки: {skipped_after_scrape}.")
    messages: list[GeneratedMessage] = []
    messages_path: Path | None = None
    if generate:
        if not no_site_leads:
            log("Организаций без сайта нет, генерация черновиков не нужна.")
        else:
            log("Начинаю генерацию сообщений через DeepSeek.")
            messages = generate_messages(no_site_leads, log=log, on_each=on_message_generated)
            messages_path = output_path(output_dir, "messages", "jsonl", query)
            write_messages_jsonl(messages_path, messages)
            log(f"Черновики сохранены: {messages_path}")

    added_to_registry = append_processed_leads_csv(processed_path, leads)

    return LeadJobResult(
        query=query,
        location=location,
        leads=leads,
        no_site_leads=no_site_leads,
        messages=messages,
        leads_path=leads_path,
        processed_path=processed_path,
        skipped_existing=skipped_after_scrape,
        messages_path=messages_path,
    )


def run_lead_job(
    *,
    query: str,
    location: str,
    limit: int,
    output_dir: Path,
    generate: bool = True,
    headless: bool = True,
    delay_seconds: float = settings.PAGE_DELAY,
    processed_file: Path | None = None,
    search_url: str = "",
    log: LogFn = noop_log,
    on_message_generated: OnGeneratedFn | None = None,
    light_parse: bool | None = None,
    prefer_no_site_stop: bool | None = None,
    contact_filter: str = "all",
    should_stop: ShouldStopFn | None = None,
) -> LeadJobResult:
    processed_path = processed_file or output_dir / "processed_no_site.csv"
    processed_keys = read_processed_lead_keys(processed_path)
    processed_yandex_urls = target_skip_urls(output_dir, "no_site")
    light = not generate if light_parse is None else light_parse
    stop_on_no_site = generate if prefer_no_site_stop is None else prefer_no_site_stop
    log(f"Старт поиска: {query}; город: {location or 'не указан'}; лимит: {limit}")
    if processed_keys:
        log(f"Реестр обработанных: {processed_path} ({len(processed_keys)} записей)")
    leads = asyncio.run(
        scrape_yandex_maps(
            query=query,
            location=location,
            limit=limit,
            headless=headless,
            delay_seconds=delay_seconds,
            skip_yandex_urls=processed_yandex_urls,
            search_url=search_url,
            log=log,
            light_parse=light,
            prefer_no_site_stop=stop_on_no_site,
            contact_filter=contact_filter,
            on_lead_checked=lambda lead: (
                append_processed_leads_csv(processed_path, [lead]),
                append_leads_unique_csv(
                    output_dir / "no_site_leads.csv",
                    [lead] if lead.website_absent_verified else [],
                ),
            ),
            should_stop=should_stop,
        )
    )

    return _finalize_lead_job(
        query=query,
        location=location,
        leads=leads,
        processed_keys=processed_keys,
        processed_path=processed_path,
        output_dir=output_dir,
        generate=generate,
        log=log,
        on_message_generated=on_message_generated,
        contact_filter=contact_filter,
        target_no_site_limit=limit if stop_on_no_site else None,
        target_mode="no_site",
    )


def run_lead_jobs_batch(
    specs: list[LeadJobSpec],
    *,
    output_dir: Path,
    generate: bool = True,
    headless: bool = True,
    delay_seconds: float = settings.PAGE_DELAY,
    processed_file: Path | None = None,
    log: LogFn = noop_log,
    on_message_generated: OnGeneratedFn | None = None,
    light_parse: bool | None = None,
    prefer_no_site_stop: bool | None = None,
    contact_filter: str = "all",
    overall_no_site_limit: int | None = None,
    on_lead_checked: OnLeadCheckedFn | None = None,
    runtime_settings: dict[str, float | int | str] | None = None,
    should_stop: ShouldStopFn | None = None,
) -> list[LeadJobResult]:
    if not specs:
        return []

    target_mode = str((runtime_settings or {}).get("TARGET_MODE", "no_site"))
    registry_paths = (
        [processed_file]
        if processed_file is not None
        else target_registry_paths(output_dir, target_mode)
    )
    processed_path = registry_paths[0]
    processed_yandex_urls = target_skip_urls(output_dir, target_mode)
    processed_keys = {
        f"yandex_url:{identity}" for identity in processed_yandex_urls
    }
    priority_identities = {
        normalize_url_identity(canonical_org_url(url))
        for spec in specs
        for url in (spec.priority_urls or [])
    }
    processed_yandex_urls.difference_update(priority_identities)
    processed_keys.difference_update(
        f"yandex_url:{identity}" for identity in priority_identities
    )
    light = not generate if light_parse is None else light_parse
    stop_on_no_site = generate if prefer_no_site_stop is None else prefer_no_site_stop
    log(f"Старт пакета: {len(specs)} поисков в одном браузере.")
    if processed_keys:
        log(f"Реестр обработанных: {processed_path} ({len(processed_keys)} записей)")

    jobs = [
        MapsScrapeJob(
            query=spec.query,
            location=spec.location,
            limit=spec.limit,
            search_url=spec.search_url,
            priority_urls=spec.priority_urls,
            group_key=spec.group_key,
        )
        for spec in specs
    ]

    def checkpoint_lead(lead: Lead) -> None:
        for registry_path in registry_paths:
            append_processed_leads_csv(registry_path, [lead])
        if lead.website_absent_verified:
            append_leads_unique_csv(output_dir / "no_site_leads.csv", [lead])
        if str(lead.raw.get("website_platform", "")).casefold() in {
            "tilda",
            "wordpress",
            "yandex_business",
        }:
            append_leads_unique_csv(output_dir / "redesign_leads.csv", [lead])
        if on_lead_checked:
            on_lead_checked(lead)

    lead_batches = asyncio.run(
        scrape_yandex_maps_batch(
            jobs,
            headless=headless,
            delay_seconds=delay_seconds,
            skip_yandex_urls=processed_yandex_urls,
            log=log,
            light_parse=light,
            prefer_no_site_stop=stop_on_no_site,
            contact_filter=contact_filter,
            overall_no_site_limit=overall_no_site_limit,
            on_lead_checked=checkpoint_lead,
            runtime_settings=runtime_settings,
            should_stop=should_stop,
        )
    )

    results: list[LeadJobResult] = []
    for spec, leads in zip(specs, lead_batches, strict=True):
        result = _finalize_lead_job(
            query=spec.query,
            location=spec.location,
            leads=leads,
            processed_keys=processed_keys,
            processed_path=processed_path,
            output_dir=output_dir,
            generate=generate,
            log=log,
            on_message_generated=on_message_generated,
            contact_filter=contact_filter,
            target_no_site_limit=spec.limit if stop_on_no_site else None,
            target_mode=target_mode,
        )
        for lead in result.leads:
            key = lead_identity_key(lead)
            if key:
                processed_keys.add(key)
        results.append(result)
    return results


def run_td_draft_job(
    yandex_url: str,
    *,
    headless: bool = True,
    log: LogFn = noop_log,
) -> GeneratedMessage:
    log("Открываю карточку в Chromium и собираю данные.")
    lead = asyncio.run(
        scrape_yandex_org_url(
            yandex_url,
            headless=headless,
            log=log,
            light_parse=False,
        )
    )
    log(f"Карточка: {lead.name}")
    messages = generate_messages([lead], log=log)
    if not messages:
        raise RuntimeError("DeepSeek не вернул черновик.")
    return messages[0]


def generate_messages(
    leads: list[Lead],
    log: LogFn = noop_log,
    *,
    on_each: OnGeneratedFn | None = None,
) -> list[GeneratedMessage]:
    if not leads:
        return []

    api_key = require_env("DEEPSEEK_API_KEY")
    client = DeepSeekClient(
        api_key=api_key,
        base_url=env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=env("DEEPSEEK_MODEL", "deepseek-chat"),
    )
    signature = env("MAIL_SIGNATURE")
    unsubscribe_text = env("MAIL_UNSUBSCRIBE_TEXT")
    workers = _deepseek_parallel_workers()

    def build_message(index: int, lead: Lead) -> GeneratedMessage:
        log(f"[{index}/{len(leads)}] Генерация черновика: {lead.name}")
        item = client.generate_outreach(lead, signature, unsubscribe_text)
        if on_each is not None:
            on_each(item)
        return item

    if workers <= 1 or len(leads) == 1:
        return [build_message(index, lead) for index, lead in enumerate(leads, start=1)]

    messages: list[GeneratedMessage | None] = [None] * len(leads)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(build_message, index, lead): index - 1
            for index, lead in enumerate(leads, start=1)
        }
        for future in as_completed(futures):
            slot = futures[future]
            messages[slot] = future.result()
    return [item for item in messages if item is not None]


def send_messages(
    messages: list[GeneratedMessage],
    confirmation: str,
    expected_confirmation: str,
    log: LogFn = noop_log,
) -> list[GeneratedMessage]:
    if confirmation != expected_confirmation:
        raise SystemExit(f"Refusing to send. Pass --confirm-send {expected_confirmation}")
    config = SMTPConfig.from_env()
    processed: list[GeneratedMessage] = []
    for index, item in enumerate(messages, start=1):
        processed_item = send_email(config, item)
        log(f"[{index}/{len(messages)}] Email status for {item.lead.name}: {processed_item.send_status}")
        processed.append(processed_item)
    return processed
