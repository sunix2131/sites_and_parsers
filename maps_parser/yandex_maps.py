from __future__ import annotations

import asyncio
import html
import os
import random
import re
import select
import socket
import socketserver
import struct
import threading
import time
from contextvars import ContextVar
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse, urlunparse

from .models import Lead
from . import settings
from .storage import normalize_url_identity


YANDEX_HOST_MARKERS = (
    "yandex.",
    "ya.ru",
    "yastatic.",
    "yandexadexchange.",
    "yandexusercontent.",
)

SOCIAL_HOSTS = (
    "vk.com",
    "vk.link",
    "t.me",
    "telegram.me",
    "wa.me",
    "whatsapp.com",
    "instagram.com",
    "facebook.com",
    "ok.ru",
    "youtube.com",
    "youtu.be",
    "rutube.ru",
    "dzen.ru",
    "viber.click",
    "viber.com",
)

# Хосты, которые являются контактом/редиректом/каталогом, а не собственным сайтом бизнеса.
CONTACT_ONLY_HOSTS = (
    "wa.me",
    "whatsapp.com",
    "t.me",
    "telegram.me",
    "viber.click",
    "viber.com",
    "vk.com",
    "vk.link",
    "ok.ru",
    "instagram.com",
    "facebook.com",
)

DIRECTORY_HOSTS = (
    "yandex.ru",
    "yandex.com",
    "maps.yandex.ru",
    "maps.yandex.com",
    "2gis.ru",
    "go.2gis.com",
    "zoon.ru",
    "spravker.ru",
    "orgpage.ru",
    "flamp.ru",
)

TECHNICAL_HOSTS = (
    "schema.org",
    "w3.org",
    "ogp.me",
)

REDIRECT_HOSTS = (
    "clck.ru",
    "yandex.ru",
    "yandex.com",
    "yandex.by",
    "yandex.kz",
    "bit.ly",
    "tinyurl.com",
    "goo.gl",
    "vk.cc",
)

SEARCH_QUERY_VARIANTS = {
    "где поесть": ("где поесть", "рестораны", "кафе", "столовые", "бары"),
    "отели": ("отели", "гостиницы", "гостевые дома", "хостелы"),
    "автосервисы": ("автосервисы", "сто", "шиномонтаж", "ремонт автомобилей"),
    "кафе": ("кафе", "кофейни", "столовые", "рестораны"),
    "салоны красоты": ("салоны красоты", "парикмахерские", "барбершопы", "маникюр"),
    "спорт": ("спорт", "фитнес-клубы", "тренажерные залы", "спортивные секции"),
}

_URL_RE = re.compile(r"https?:\\?/\\?/[^\s\"'<>]+|https?://[^\s\"'<>]+", re.I)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s().\-–—]*){10,18}(?!\d)")
_WEBSITE_LABEL_RE = re.compile(r"(?:официальн(?:ый|ого|ом)?\s+)?сайт|website|web[-\s]?site", re.I)

LogFn = Callable[[str], None]
LeadCheckedFn = Callable[[Lead], None]
_RUNTIME_SETTINGS: ContextVar[dict[str, float | int | str]] = ContextVar(
    "maps_parser_runtime_settings",
    default={},
)


def _runtime_setting(name: str, default: float | int | str) -> float | int | str:
    return _RUNTIME_SETTINGS.get().get(name, getattr(settings, name, default))


class YandexServicePageError(RuntimeError):
    """Raised when Yandex shows captcha or another non-organization service page."""


YANDEX_BOOTSTRAP_HOSTS = (
    "yandex.ru",
    "maps.yandex.ru",
    "yandex.com",
    "maps.yandex.com",
    "yandex.eu",
    "maps.yandex.eu",
    "api-maps.yandex.ru",
    "static-maps.yandex.ru",
    "yastatic.net",
    "avatars.mds.yandex.net",
    "core-renderer-tiles.maps.yandex.net",
)

YANDEX_FALLBACK_IPS = {
    "yandex.ru": "77.88.55.242",
    "maps.yandex.ru": "213.180.204.242",
    "yandex.com": "77.88.55.242",
    "maps.yandex.com": "213.180.204.242",
    "yandex.eu": "77.88.55.242",
    "maps.yandex.eu": "213.180.204.242",
    # CDN статики; apex не совпадает с суффиксом ".yastatic.net" — см. _resolve_host_for_proxy
    "yastatic.net": "93.158.134.91",
}

# Запасные IP для CONNECT через встроенный proxy (таймауты / перегрузка одного узла).
_YANDEX_MAIN_FRONT_IPS = ("77.88.55.242", "77.88.55.88", "5.255.255.77", "87.250.250.242")
_YANDEX_MAPS_FRONT_IPS = ("213.180.204.242", "87.250.251.134", "213.180.204.41")
_YANDEX_YASTATIC_FALLBACK_IPS = ("93.158.134.91", "87.250.247.183")

# Chromium ходит сюда в фоне (компоненты Google); при блокировке DNS это не ломает карты.
_DNS_PROXY_NON_ESSENTIAL_HOST_SUFFIXES = (".gvt1.com",)


def _dns_proxy_is_non_essential_host(host: str) -> bool:
    h = host.strip("[] \t\r\n.").lower()
    return any(h.endswith(suffix) for suffix in _DNS_PROXY_NON_ESSENTIAL_HOST_SUFFIXES)


def _local_yandex_dns_ok() -> bool:
    try:
        socket.getaddrinfo("yandex.ru", 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


def _needs_internal_dns_proxy() -> bool:
    if settings.SKIP_INTERNAL_DNS_PROXY:
        return False
    if settings.USE_INTERNAL_DNS_PROXY:
        return True
    return not _local_yandex_dns_ok()


def _feed_collection_target(card_limit: int, registry_size: int) -> int:
    """Сколько уникальных /org/ собрать в ленте до остановки скролла."""
    slack = max(10, card_limit // 6)
    registry_slack = min(max(0, registry_size), max(20, card_limit // 2))
    return min(220, card_limit + registry_slack + slack)


def _no_site_scan_limit(target_no_site: int) -> int:
    """Сколько карточек можно проверить, чтобы набрать target_no_site без сайта."""
    target = max(1, int(target_no_site))
    max_cards = max(target, int(getattr(settings, "NO_SITE_SCAN_MAX_CARDS", 1000)))
    return min(max_cards, target * 5)


def _no_site_collection_target(scan_limit: int, registry_size: int) -> int:
    """Сколько ссылок из выдачи собрать для режима точного набора без сайта."""
    scan = max(1, int(scan_limit))
    max_cards = max(scan, int(getattr(settings, "NO_SITE_SCAN_MAX_CARDS", 1000)))
    registry_slack = min(max(0, registry_size), max_cards)
    return scan + registry_slack


async def _polite_card_pause(
    done_cards: int,
    base_delay_seconds: float,
    logger: LogFn,
    *,
    elapsed_seconds: float = 0.0,
) -> None:
    """Умеренные паузы между карточками и периодический cooldown без обхода защит."""
    jitter = max(0.0, float(_runtime_setting("PAGE_DELAY_JITTER_SECONDS", 0.0)))
    base = max(0.0, float(base_delay_seconds))
    target_interval = base + (random.uniform(0.0, jitter) if jitter else 0.0)
    delay = max(0.0, target_interval - max(0.0, elapsed_seconds))
    if delay > 0:
        await asyncio.sleep(delay)

    cooldown_after = max(0, int(_runtime_setting("CARD_COOLDOWN_AFTER", 0)))
    if cooldown_after and done_cards > 0 and done_cards % cooldown_after == 0:
        cooldown = max(0.0, float(_runtime_setting("CARD_COOLDOWN_SECONDS", 0.0)))
        if cooldown > 0:
            extra = random.uniform(0.0, jitter) if jitter else 0.0
            logger(
                "Бережный режим: cooldown %.0f сек. после %s проверенных карточек."
                % (cooldown + extra, done_cards)
            )
            await asyncio.sleep(cooldown + extra)


@dataclass(slots=True)
class MapsScrapeJob:
    query: str
    location: str = ""
    limit: int = 20
    search_url: str = ""
    priority_urls: list[str] | None = None
    group_key: str = ""


@dataclass(slots=True)
class OrgPageSnapshot:
    dom_items: list[dict[str, str]]
    body_text: str
    html_text: str
    external_links: list[str]
    unresolved_redirects: list[str]
    social_links: list[str]
    phones: list[str]
    emails: list[str]
    websites: list[str]
    website_link_signal: bool = False


_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
_BLOCKED_URL_MARKERS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "mc.yandex.ru/watch",
    "yandex.ru/clck",
    "core-renderer-tiles.maps.yandex",
    "yandex.ru/ads",
)

_YANDEX_SERVICE_TITLE_MARKERS = (
    "подтвердите, что запросы отправляли вы, а не робот",
    "yandex uses cookies",
)

_YANDEX_SERVICE_BODY_MARKERS = (
    "подтвердите, что запросы отправляли вы, а не робот",
    "smartcaptcha",
    "showcaptcha",
    "yandex uses cookies",
)


def _parallel_org_tabs() -> int:
    return settings.PARALLEL_ORG_TABS


def _card_log_batch_size() -> int:
    return settings.CARD_LOG_BATCH


def _parse_playwright_proxy(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if not raw:
        raise ValueError("Пустой адрес прокси.")
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"Некорректный прокси: {raw}")
    port = parsed.port
    if port is None:
        port = 80 if parsed.scheme == "http" else 443
    proxy: dict[str, str] = {"server": f"{parsed.scheme}://{parsed.hostname}:{port}"}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def _playwright_proxy_configs() -> list[dict[str, str]]:
    configs: list[dict[str, str]] = []
    multi = (os.environ.get("PLAYWRIGHT_PROXIES") or "").strip()
    if multi:
        for part in re.split(r"[\n,;]+", multi):
            part = part.strip()
            if part:
                configs.append(_parse_playwright_proxy(part))
    if not configs:
        single = (os.environ.get("PLAYWRIGHT_PROXY") or "").strip()
        if single:
            configs.append(_parse_playwright_proxy(single))
    if not configs:
        multi = (settings.PLAYWRIGHT_PROXIES or "").strip()
        if multi:
            for part in re.split(r"[\n,;]+", multi):
                part = part.strip()
                if part:
                    configs.append(_parse_playwright_proxy(part))
    if not configs:
        single = (settings.PLAYWRIGHT_PROXY or "").strip()
        if single:
            configs.append(_parse_playwright_proxy(single))
    if not configs:
        single = _playwright_proxy_from_generic_env()
        if single:
            configs.append(_parse_playwright_proxy(single))
    return configs


def _playwright_proxy_from_generic_env() -> str:
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        raw = os.environ.get(name, "").strip()
        if raw.lower().startswith(("http://", "https://", "socks4://", "socks5://", "socks5h://")):
            return raw
    return ""


def _proxy_index_for_card(card_index: int, proxy_count: int, links_per: int | None = None) -> int:
    if proxy_count <= 0:
        return 0
    per_context = max(1, links_per if links_per is not None else settings.PROXY_LINKS_PER_CONTEXT)
    group = (max(1, card_index) - 1) // per_context
    return group % proxy_count


class _PlaywrightProxyPool:
    def __init__(
        self,
        browser: Any,
        proxies: list[dict[str, str]],
        *,
        logger: LogFn,
    ) -> None:
        self._browser = browser
        self._proxies = proxies
        self._logger = logger
        self._contexts: dict[int, Any] = {}

    async def context_for_card(self, card_index: int) -> Any:
        proxy_index = _proxy_index_for_card(card_index, len(self._proxies))
        if proxy_index not in self._contexts:
            proxy = self._proxies[proxy_index]
            self._logger(
                "Прокси %s/%s: %s (до %s ссылок подряд)."
                % (
                    proxy_index + 1,
                    len(self._proxies),
                    proxy["server"],
                    settings.PROXY_LINKS_PER_CONTEXT,
                )
            )
            context = await self._browser.new_context(
                proxy=proxy,
                locale="ru-RU",
                viewport={"width": 1440, "height": 1000},
            )
            await _install_speed_routes(context)
            self._contexts[proxy_index] = context
        return self._contexts[proxy_index]

    async def close(self) -> None:
        for context in self._contexts.values():
            try:
                await context.close()
            except Exception:
                pass
        self._contexts.clear()


def _chromium_browser_launch_plan(
    *,
    headless: bool,
    logger: LogFn,
) -> tuple[dict[str, Any], socketserver.ThreadingTCPServer | None, list[dict[str, str]]]:
    proxy_configs = _playwright_proxy_configs()
    internal_proxy: socketserver.ThreadingTCPServer | None = None
    launch_kwargs: dict[str, Any] = {"headless": headless}
    pool_proxies = proxy_configs if len(proxy_configs) > 1 else []

    if pool_proxies:
        logger(
            "Ротация прокси: %s шт., %s ссылок подряд на каждый."
            % (len(pool_proxies), settings.PROXY_LINKS_PER_CONTEXT)
        )
    elif proxy_configs:
        launch_kwargs["proxy"] = proxy_configs[0]
    elif _needs_internal_dns_proxy():
        internal_proxy = _start_internal_dns_proxy(logger)
        host, port = internal_proxy.server_address
        launch_kwargs["proxy"] = {"server": f"http://{host}:{port}"}

    dns_args = _chromium_dns_resolver_args(
        logger,
        skip_if_proxy=bool(proxy_configs or internal_proxy),
    )
    if dns_args:
        launch_kwargs["args"] = [*launch_kwargs.get("args", []), *dns_args]
    return launch_kwargs, internal_proxy, pool_proxies


async def _new_browser_context(browser: Any) -> Any:
    context = await browser.new_context(
        locale="ru-RU",
        viewport={"width": 1440, "height": 1000},
    )
    await _install_speed_routes(context)
    return context


def _eligible_no_site_leads(leads: list[Lead], contact_filter: str = "all") -> int:
    return sum(
        1
        for lead in leads
        if lead.website_absent_verified
        and lead.name.strip()
        and lead.matches_contact_filter(contact_filter)
    )


def lead_matches_target(lead: Lead, contact_filter: str = "all") -> bool:
    mode = str(_runtime_setting("TARGET_MODE", "no_site"))
    redesign = str(lead.raw.get("website_platform", "")).casefold() in {
        "tilda",
        "wordpress",
        "yandex_business",
    }
    if mode == "redesign":
        target = redesign
    elif mode == "combined":
        target = lead.website_absent_verified or redesign
    else:
        target = lead.website_absent_verified
    return target and lead.name.strip() and lead.matches_contact_filter(contact_filter)


def _eligible_target_leads(leads: list[Lead], contact_filter: str = "all") -> int:
    return sum(1 for lead in leads if lead_matches_target(lead, contact_filter))


def _scrape_should_stop(
    leads: list[Lead],
    limit: int,
    stop_after_no_site: int | None,
    contact_filter: str = "all",
) -> bool:
    if len(leads) >= limit:
        return True
    if (
        stop_after_no_site is not None
        and _eligible_target_leads(leads, contact_filter) >= stop_after_no_site
    ):
        return True
    return False


class CardLogBatcher:
    def __init__(self, logger: LogFn, batch_size: int) -> None:
        self._logger = logger
        self._batch_size = max(1, batch_size)
        self._lines: list[str] = []

    def append(self, index: int, total: int, nav_url: str, lead: Lead) -> None:
        platform = str(lead.raw.get("website_platform", "")).casefold()
        if platform:
            marker = "🔧"
            platform_label = {
                "tilda": "Tilda",
                "wordpress": "WordPress",
                "yandex_business": "Яндекс.Бизнес",
            }.get(platform, platform)
            status_txt = f"переработка сайта — {platform_label}"
        elif lead.has_website:
            marker = "❌"
            status_txt = "есть сайт"
        elif lead.website_absent_verified:
            marker = "✅"
            status_txt = "нет сайта подтверждено"
        else:
            marker = "⚠️"
            status_txt = "сайт не подтверждён"
        title = lead.name.strip() if lead.name else "без названия"
        self._lines.append(f"[{index}/{total}] {marker} {status_txt} — {title}\n{nav_url}")
        if len(self._lines) >= self._batch_size or index >= total:
            self.flush()

    def flush(self) -> None:
        if not self._lines:
            return
        self._logger("\n".join(self._lines))
        self._lines.clear()


def _read_dns_name(data: bytes, offset: int, *, depth: int = 0) -> tuple[str, int]:
    if depth > 10:
        return "", offset
    parts: list[str] = []
    jumped = False
    while offset < len(data):
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                return ".".join(parts), len(data)
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            pointed, _ = _read_dns_name(data, pointer, depth=depth + 1)
            if pointed:
                parts.append(pointed)
            offset += 2
            jumped = True
            break
        offset += 1
        label = data[offset : offset + length]
        parts.append(label.decode("ascii", errors="ignore"))
        offset += length
    return ".".join(part for part in parts if part), offset if not jumped else offset


def _dns_query_a(host: str, server: str, timeout: float = 1.2) -> tuple[list[str], list[str]]:
    query_id = struct.unpack("!H", os.urandom(2))[0]
    labels = b"".join(len(part).to_bytes(1, "big") + part.encode("ascii") for part in host.split("."))
    question = labels + b"\x00" + struct.pack("!HH", 1, 1)
    packet = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + question

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(4096)

    if len(data) < 12 or struct.unpack("!H", data[:2])[0] != query_id:
        return [], []
    qdcount, ancount = struct.unpack("!HH", data[4:8])
    offset = 12
    for _ in range(qdcount):
        _, offset = _read_dns_name(data, offset)
        offset += 4

    ips: list[str] = []
    cnames: list[str] = []
    for _ in range(ancount):
        if offset >= len(data):
            break
        _, offset = _read_dns_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, rclass, _, rdlength = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlength]
        offset += rdlength
        if rtype == 1 and rclass == 1 and rdlength == 4:
            ips.append(socket.inet_ntoa(rdata))
        elif rtype == 5 and rclass == 1:
            cname, _ = _read_dns_name(data, offset - rdlength)
            if cname:
                cnames.append(cname.rstrip("."))
    return ips, cnames


def _resolve_a_via_public_dns(host: str, *, seen: set[str] | None = None) -> str | None:
    seen = seen or set()
    if host in seen:
        return None
    seen.add(host)
    servers_raw = settings.DNS_SERVERS
    cname_candidates: list[str] = []
    for server in [part.strip() for part in servers_raw.split(",") if part.strip()]:
        try:
            ips, cnames = _dns_query_a(host, server)
        except OSError:
            continue
        if ips:
            return ips[0]
        cname_candidates.extend(cnames)
    for cname in cname_candidates:
        ip = _resolve_a_via_public_dns(cname, seen=seen)
        if ip:
            return ip
    return None


def _yandex_host_ip_map() -> dict[str, str]:
    result: dict[str, str] = {}
    for host in YANDEX_BOOTSTRAP_HOSTS:
        ip = _resolve_a_via_public_dns(host) or YANDEX_FALLBACK_IPS.get(host)
        if ip:
            result[host] = ip
    return result


def _ensure_etc_hosts_yandex(host_ips: dict[str, str], logger: LogFn) -> None:
    if not host_ips or settings.SKIP_ETC_HOSTS:
        return
    hosts_path = Path("/etc/hosts")
    if os.name == "nt" or not hosts_path.exists():
        return
    start = "# maps_parser yandex dns start"
    end = "# maps_parser yandex dns end"
    block_lines = [start]
    for host, ip in sorted(host_ips.items()):
        block_lines.append(f"{ip}\t{host}")
    block_lines.append(end)
    block = "\n".join(block_lines) + "\n"
    try:
        current = hosts_path.read_text(encoding="utf-8", errors="replace")
        pattern = re.compile(rf"{re.escape(start)}\n.*?{re.escape(end)}\n?", re.DOTALL)
        updated = pattern.sub(block, current) if pattern.search(current) else current.rstrip() + "\n\n" + block
        if updated != current:
            hosts_path.write_text(updated, encoding="utf-8")
            logger("DNS-обход записан в /etc/hosts для: " + ", ".join(sorted(host_ips)))
    except OSError as exc:
        logger(f"Не удалось обновить /etc/hosts ({exc}); оставляю Chromium host-resolver-rules.")


def _resolve_host_for_proxy(host: str) -> str:
    host = host.strip("[] \t\r\n.").lower()
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass
    ip = _resolve_a_via_public_dns(host) or YANDEX_FALLBACK_IPS.get(host)
    if ip:
        return ip
    # Apex "yastatic.net" не оканчивается на ".yastatic.net"; без этого уходит в getaddrinfo и падает при сломанном DNS.
    if host == "yastatic.net" or host.endswith(".yastatic.net"):
        return YANDEX_FALLBACK_IPS["yastatic.net"]
    if host.endswith((".yandex.ru", ".yandex.com", ".yandex.eu", ".yandex.net")):
        if host.endswith((".com", ".eu")):
            return YANDEX_FALLBACK_IPS["yandex.com"]
        if host.endswith(".net"):
            return YANDEX_FALLBACK_IPS["maps.yandex.ru"]
        return YANDEX_FALLBACK_IPS["yandex.ru"]
    return socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)[0][4][0]


def _proxy_upstream_candidates(host: str, resolved_ip: str) -> list[str]:
    host_l = host.lower()
    if host_l == "yastatic.net" or host_l.endswith(".yastatic.net"):
        extras: tuple[str, ...] = _YANDEX_YASTATIC_FALLBACK_IPS
    elif "maps.yandex" in host_l or host_l.endswith(".maps.yandex.net"):
        extras = _YANDEX_MAPS_FRONT_IPS
    elif host_l.endswith(".yandex.net"):
        extras = _YANDEX_MAPS_FRONT_IPS
    elif host_l.endswith((".yandex.com", ".yandex.eu")) or host_l in {"yandex.com", "yandex.eu"}:
        extras = _YANDEX_MAIN_FRONT_IPS
    elif host_l.endswith(".yandex.ru") or host_l == "yandex.ru":
        extras = _YANDEX_MAIN_FRONT_IPS
    else:
        extras = (*_YANDEX_MAIN_FRONT_IPS, *_YANDEX_MAPS_FRONT_IPS)
    out: list[str] = []
    for ip in (resolved_ip, *extras):
        if ip and ip not in out:
            out.append(ip)
    return out


def _split_host_port(target: str, default_port: int = 443) -> tuple[str, int]:
    if target.startswith("[") and "]" in target:
        host, _, port_raw = target[1:].partition("]:")
        return host, int(port_raw or default_port)
    host, sep, port_raw = target.rpartition(":")
    if sep and port_raw.isdigit():
        return host, int(port_raw)
    return target, default_port


def _relay_sockets(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 60)
            if not readable:
                return
            for src in readable:
                data = src.recv(65536)
                if not data:
                    return
                dst = right if src is left else left
                dst.sendall(data)
    finally:
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def _start_internal_dns_proxy(logger: LogFn) -> socketserver.ThreadingTCPServer:
    class ProxyServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    class ProxyHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(20)
            chunks: list[bytes] = []
            while b"\r\n\r\n" not in b"".join(chunks):
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                chunks.append(chunk)
                if sum(len(item) for item in chunks) > 65536:
                    return
            header = b"".join(chunks)
            first_line = header.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
            parts = first_line.split()
            if len(parts) < 3 or parts[0].upper() != "CONNECT":
                self.request.sendall(b"HTTP/1.1 501 Not Implemented\r\nConnection: close\r\n\r\n")
                return
            host, port = _split_host_port(parts[1], 443)
            connect_timeout = settings.PROXY_CONNECT_TIMEOUT
            try:
                primary = _resolve_host_for_proxy(host)
            except OSError as exc:
                if _dns_proxy_is_non_essential_host(host):
                    logger(
                        f"DNS-proxy: фон {host}:{port} не резолвится ({exc}); "
                        "для Яндекс.Карт не нужен."
                    )
                else:
                    logger(f"DNS-proxy: не удалось подключиться к {host}:{port} ({exc})")
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                return
            last_exc: OSError | None = None
            upstream: socket.socket | None = None
            max_ips = settings.PROXY_MAX_IPS
            for cand in _proxy_upstream_candidates(host, primary)[:max_ips]:
                try:
                    upstream = socket.create_connection((cand, port), timeout=connect_timeout)
                    if cand != primary:
                        logger(f"DNS-proxy: {host}:{port} подключено через запасной IP {cand}")
                    break
                except OSError as exc:
                    last_exc = exc
                    continue
            if upstream is None:
                if _dns_proxy_is_non_essential_host(host):
                    logger(
                        f"DNS-proxy: фон {host}:{port} не достучаться ({last_exc}); "
                        "для Яндекс.Карт не нужен."
                    )
                else:
                    logger(f"DNS-proxy: не удалось подключиться к {host}:{port} ({last_exc})")
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                return
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _relay_sockets(self.request, upstream)

    server = ProxyServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    logger(f"Встроенный DNS-proxy для Chromium: http://{host}:{port}")
    return server


def _chromium_dns_resolver_args(logger: LogFn, *, skip_if_proxy: bool) -> list[str]:
    """Подставить IP стартовых yandex-хостов прямо в Chromium, обходя его DNS-резолвер."""
    if skip_if_proxy:
        return []
    if settings.SKIP_DNS_CHECK:
        return []
    local_dns_ok = True
    try:
        socket.getaddrinfo("yandex.ru", 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        local_dns_ok = False
        logger(
            f"DNS: не удаётся разрешить yandex.ru ({exc}). "
            "Пробую обойти DNS WSL через Chromium host-resolver-rules."
        )
    host_ips = _yandex_host_ip_map()
    _ensure_etc_hosts_yandex(host_ips, logger)
    rules = [f"MAP {host} {ip}" for host, ip in host_ips.items()]
    if rules:
        logger("DNS-обход включён для Chromium: " + ", ".join(rules))
        return ["--host-resolver-rules=" + ",".join([*rules, "EXCLUDE localhost"])]
    if local_dns_ok:
        return []
    logger(
        "DNS-обход не сработал: публичные DNS тоже недоступны. "
        "Проверь сеть WSL/VPN или задай PLAYWRIGHT_PROXY=http://127.0.0.1:<порт>."
    )
    raise RuntimeError("В этой среде не резолвится Яндекс (DNS), и UDP DNS-обход недоступен.")


def _maps_origin_netloc() -> str:
    raw = settings.YANDEX_MAPS_ORIGIN
    if "://" in raw:
        return urlparse(raw).netloc or "yandex.ru"
    return raw.removeprefix("/") or "yandex.ru"


def _maps_nav_fallback_urls(primary: str) -> list[str]:
    """Порядок: как пришло, затем тот же путь на другом домене yandex.ru ↔ yandex.com."""
    primary = primary.replace("http://yandex.", "https://yandex.", 1)
    out: list[str] = []
    for candidate in (
        primary,
        primary.replace("://yandex.com/", "://yandex.ru/", 1),
        primary.replace("://yandex.ru/", "://yandex.com/", 1),
    ):
        if candidate not in out:
            out.append(candidate)
    return out


def _normalize_maps_subdomain_path(url: str) -> str:
    """На maps.yandex.* путь уже без префикса /maps/ — убрать дубль после редиректа."""
    parsed = urlparse(url)
    if not parsed.netloc.startswith("maps.yandex."):
        return url
    path = parsed.path
    if path.startswith("/maps/"):
        path = "/" + path[len("/maps/") :].lstrip("/")
    elif path == "/maps":
        path = "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))


def _maps_subdomain_variant(url: str) -> str:
    """Тот же путь на maps.yandex.* — иногда резолвится иначе, чем yandex.ru."""
    if "://maps.yandex." in url:
        return _normalize_maps_subdomain_path(url)
    return _normalize_maps_subdomain_path(
        url.replace("https://yandex.ru/maps/", "https://maps.yandex.ru/", 1)
        .replace("http://yandex.ru/maps/", "https://maps.yandex.ru/", 1)
        .replace("https://yandex.com/maps/", "https://maps.yandex.com/", 1)
        .replace("http://yandex.com/maps/", "https://maps.yandex.com/", 1)
    )


def _path_url_candidates(primary: str) -> list[str]:
    out: list[str] = []
    for base in _maps_nav_fallback_urls(primary):
        for u in (base, _maps_subdomain_variant(base)):
            if u not in out:
                out.append(u)
    return out


def _maps_text_search_urls(search_text: str) -> list[str]:
    """Запасной режим — только текстовый поиск на разных хостах карт."""
    q = quote_plus(search_text.strip())
    return [
        f"https://yandex.ru/maps/?text={q}",
        f"https://maps.yandex.ru/?text={q}",
        f"https://yandex.com/maps/?text={q}",
        f"https://maps.yandex.com/?text={q}",
    ]


def _all_map_open_candidates(primary: str, search_text_fallback: str | None) -> list[str]:
    out = _path_url_candidates(primary)
    if search_text_fallback:
        for u in _maps_text_search_urls(search_text_fallback):
            if u not in out:
                out.append(u)
    return out


async def _maps_page_open_failed(page) -> bool:
    try:
        if "/maps/maps/" in page.url:
            return True
        title = (await page.title()).lower()
        if "404" in title:
            return True
        body = await page.locator("body").inner_text(timeout=3_000)
        if "Ошибка 404" in body or "Нет такой страницы" in body:
            return True
        if "can't be reached" in body.lower() or "took too long to respond" in body.lower():
            return True
    except Exception:
        return False
    return False


def is_yandex_service_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "yandex" not in host:
        return False
    return "showcaptcha" in path or "/support/smart-captcha" in path


def is_yandex_service_lead(lead: Lead) -> bool:
    name = lead.name.strip().casefold()
    return is_yandex_service_url(lead.yandex_url) or any(
        marker in name for marker in _YANDEX_SERVICE_TITLE_MARKERS
    )


async def _raise_if_yandex_service_page(page, *, context: str) -> None:
    if is_yandex_service_url(page.url):
        raise YandexServicePageError(f"Яндекс показал служебную страницу вместо карточки ({context})")
    try:
        title = (await page.title()).casefold()
        if any(marker in title for marker in _YANDEX_SERVICE_TITLE_MARKERS):
            raise YandexServicePageError(f"Яндекс показал служебную страницу вместо карточки ({context})")
        body = (await page.locator("body").inner_text(timeout=2_000)).casefold()
        if any(marker in body for marker in _YANDEX_SERVICE_BODY_MARKERS):
            raise YandexServicePageError(f"Яндекс показал служебную страницу вместо карточки ({context})")
    except YandexServicePageError:
        raise
    except Exception:
        return


async def open_maps_search_page(
    page,
    search_url: str,
    logger: LogFn,
    *,
    search_text_fallback: str | None = None,
) -> None:
    last_err: BaseException | None = None
    candidates = _all_map_open_candidates(search_url, search_text_fallback)
    for index, candidate in enumerate(candidates):
        try:
            await page.goto(candidate, wait_until="domcontentloaded", timeout=settings.NAV_TIMEOUT_MS)
            if await _maps_page_open_failed(page):
                logger(f"Не удалось открыть (пустая или 404): {candidate}")
                continue
            if index > 0:
                logger(f"Открыто после запасного URL: {candidate}")
            return
        except Exception as exc:
            last_err = exc
            err_txt = str(exc)
            if any(
                marker in err_txt
                for marker in (
                    "ERR_NAME_NOT_RESOLVED",
                    "ERR_CONNECTION",
                    "ERR_INTERNET_DISCONNECTED",
                    "ERR_TIMED_OUT",
                    "TimeoutError",
                    "net::ERR",
                )
            ):
                logger(f"Не удалось открыть ({candidate}): {exc}")
                continue
            raise
    if last_err:
        if "NAME_NOT_RESOLVED" in str(last_err).upper() or "ERR_NAME_NOT_RESOLVED" in str(last_err):
            logger(
                "Браузер не может разрешить имя хоста Яндекса (DNS). "
                "Проверь DNS в WSL (/etc/resolv.conf), VPN или задай прокси: "
                "PLAYWRIGHT_PROXY=http://127.0.0.1:ПОРТ"
            )
        raise last_err


async def open_maps_org_page(page, nav_url: str, logger: LogFn) -> None:
    """Прямой переход на карточку /org/; запасные URL — только при сбое."""
    candidate = nav_url.replace("http://yandex.", "https://yandex.", 1)
    try:
        await page.goto(candidate, wait_until="domcontentloaded", timeout=settings.NAV_TIMEOUT_MS)
        if not await _maps_page_open_failed(page):
            return
    except Exception as exc:
        err_txt = str(exc)
        if not any(
            marker in err_txt
            for marker in (
                "ERR_NAME_NOT_RESOLVED",
                "ERR_CONNECTION",
                "ERR_INTERNET_DISCONNECTED",
                "ERR_TIMED_OUT",
                "TimeoutError",
                "net::ERR",
            )
        ):
            raise
        logger(f"Не удалось открыть карточку напрямую ({candidate}): {exc}")
    await open_maps_search_page(page, nav_url, logger, search_text_fallback=None)


async def _install_speed_routes(context) -> None:
    if settings.DISABLE_RESOURCE_BLOCK:
        return

    async def handle(route) -> None:
        request = route.request
        if request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return
        url = request.url.lower()
        if any(marker in url for marker in _BLOCKED_URL_MARKERS):
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", handle)


def normalize_org_nav_url(url: str) -> str:
    """Привести домен карточки к выбранному YANDEX_MAPS_ORIGIN (по умолчанию yandex.ru)."""
    netloc = _maps_origin_netloc()
    if netloc in {"maps.yandex.ru", "yandex.ru"}:
        u = (
            url.replace("https://maps.yandex.com/", "https://maps.yandex.ru/", 1)
            .replace("http://maps.yandex.com/", "https://maps.yandex.ru/", 1)
            .replace("https://yandex.com/", "https://yandex.ru/", 1)
            .replace("http://yandex.com/", "https://yandex.ru/", 1)
        )
        if netloc == "maps.yandex.ru":
            u = u.replace("https://yandex.ru/", "https://maps.yandex.ru/", 1).replace(
                "http://yandex.ru/", "https://maps.yandex.ru/", 1
            )
        else:
            u = u.replace("https://maps.yandex.ru/", "https://yandex.ru/", 1).replace(
                "http://maps.yandex.ru/", "https://yandex.ru/", 1
            )
        return u
    if netloc in {"maps.yandex.com", "yandex.com"}:
        u = (
            url.replace("https://maps.yandex.ru/", "https://maps.yandex.com/", 1)
            .replace("http://maps.yandex.ru/", "https://maps.yandex.com/", 1)
            .replace("https://yandex.ru/", "https://yandex.com/", 1)
            .replace("http://yandex.ru/", "https://yandex.com/", 1)
        )
        if netloc == "maps.yandex.com":
            u = u.replace("https://yandex.com/", "https://maps.yandex.com/", 1).replace(
                "http://yandex.com/", "https://maps.yandex.com/", 1
            )
        else:
            u = u.replace("https://maps.yandex.com/", "https://yandex.com/", 1).replace(
                "http://maps.yandex.com/", "https://yandex.com/", 1
            )
        return u
    return url


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def host_matches(host: str, markers: Iterable[str]) -> bool:
    host = host.lower().removeprefix("www.").strip(".")
    for raw_marker in markers:
        marker = raw_marker.lower().removeprefix("www.").strip()
        if not marker:
            continue
        if marker.endswith(".") or marker.startswith("."):
            needle = marker.strip(".")
            if needle and needle in host:
                return True
            continue
        marker = marker.strip(".")
        if host == marker or host.endswith("." + marker):
            return True
    return False


def _url_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _is_redirect_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        return False
    if host_matches(host, ("yandex.ru", "yandex.com", "yandex.by", "yandex.kz")):
        return "/clck" in parsed.path.lower() or "clid=" in parsed.query.lower()
    return host_matches(host, REDIRECT_HOSTS)


def _decode_url_text(value: str) -> str:
    decoded = html.unescape(value or "")
    replacements = {
        "\\/": "/",
        "\\u002F": "/",
        "\\u002f": "/",
        "\\u003A": ":",
        "\\u003a": ":",
        "\\u0026": "&",
        "\\u0027": "'",
        "\\u003D": "=",
        "\\u003d": "=",
    }
    for source, target in replacements.items():
        decoded = decoded.replace(source, target)
    return decoded


def clean_url(raw: str) -> str:
    value = _decode_url_text(raw).strip()
    if value.startswith("http:\\//") or value.startswith("https:\\//"):
        value = value.replace("\\/", "/")
    value = re.split(r"[\\\s\"'<>]+", value, maxsplit=1)[0]
    value = value.rstrip(".,;:)]}»")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.params, parsed.query, ""))


def extract_urls_from_text(value: str) -> list[str]:
    decoded = _decode_url_text(value)
    return unique(clean_url(match.group(0)) for match in _URL_RE.finditer(decoded))


def is_social_url(url: str) -> bool:
    host = _url_host(url)
    if not host or not host_matches(host, SOCIAL_HOSTS):
        return False
    lower_url = url.lower()
    return not any(marker in lower_url for marker in ("yandex.maps", "mapsyandex", "yandexmaps"))


def is_business_website(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        return False
    if host_matches(host, YANDEX_HOST_MARKERS):
        return False
    if host_matches(host, CONTACT_ONLY_HOSTS):
        return False
    if host_matches(host, DIRECTORY_HOSTS):
        return False
    if host_matches(host, TECHNICAL_HOSTS):
        return False
    if _is_redirect_url(url):
        return False
    return True


def normalize_phone(raw: str) -> str:
    value = html.unescape(raw or "")
    digits = re.sub(r"\D+", "", value)
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 11 and digits.startswith("7"):
        pass
    elif digits.startswith("7"):
        return ""
    elif 12 <= len(digits) <= 15 and value.strip().startswith("+"):
        pass
    else:
        return ""
    return f"+{digits}"


def phones_from_text(value: str) -> list[str]:
    phones: list[str] = []
    for match in _PHONE_RE.finditer(value or ""):
        raw = match.group(0)
        # Не считаем голые id/координаты телефонами: у телефона обычно есть +, пробелы, скобки или дефисы.
        if not any(ch in raw for ch in "+ ()-.–—"):
            continue
        phone = normalize_phone(raw)
        if phone:
            phones.append(phone)
    return unique(phones)


def emails_from_text(value: str) -> list[str]:
    return unique(
        item.strip(".,;:()[]{}<>")
        for item in re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", value or "", re.I)
    )


async def _resolve_redirect_url(page, url: str) -> str:
    try:
        response = await page.context.request.get(
            url,
            max_redirects=6,
            timeout=5_000,
            fail_on_status_code=False,
        )
        return clean_url(response.url)
    except Exception:
        return ""


async def resolve_redirect_links_with_status(page, urls: Iterable[str]) -> tuple[list[str], list[str]]:
    out: list[str] = []
    unresolved_redirects: list[str] = []
    for url in urls:
        clean = clean_url(url)
        if not clean:
            continue
        out.append(clean)
        if _is_redirect_url(clean):
            resolved = await _resolve_redirect_url(page, clean)
            if resolved and resolved != clean:
                out.append(resolved)
            else:
                unresolved_redirects.append(clean)
    return unique(out), unique(unresolved_redirects)


async def resolve_redirect_links(page, urls: Iterable[str]) -> list[str]:
    out, _ = await resolve_redirect_links_with_status(page, urls)
    return out


async def wait_for_org_card_content(page, *, light: bool = False) -> None:
    selectors = [
        'h1[class*="card-title"]',
        '[class*="card-title-view__title"]',
        '[class*="orgpage-header-view__header"] h1',
        'a[href^="tel:"]',
        'a[href^="mailto:"]',
        '[class*="business-contacts"]',
        'h1',
    ]
    try:
        await page.wait_for_selector(
            ", ".join(selectors),
            timeout=3_000 if light else 4_000,
            state="attached",
        )
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=800 if light else 1_200)
    except Exception:
        pass
    await page.wait_for_timeout(300 if light else 500)


async def collect_dom_link_items(page) -> list[dict[str, str]]:
    try:
        return await page.locator("a[href], [data-href], [data-url]").evaluate_all(
            """
            nodes => nodes.map(node => {
                const attrs = {};
                for (const attr of node.getAttributeNames ? node.getAttributeNames() : []) {
                    attrs[attr] = node.getAttribute(attr) || '';
                }
                return {
                    href: node.href || node.getAttribute('href') || node.getAttribute('data-href') || node.getAttribute('data-url') || '',
                    text: node.innerText || node.textContent || '',
                    aria: node.getAttribute('aria-label') || '',
                    title: node.getAttribute('title') || '',
                    attrs: JSON.stringify(attrs),
                };
            })
            """
        )
    except Exception:
        return []


def _dom_items_blob(dom_items: Iterable[dict[str, str]]) -> str:
    return "\n".join(
        " ".join(str(item.get(key, "")) for key in ("href", "text", "aria", "title", "attrs"))
        for item in dom_items
        if isinstance(item, dict)
    )


def _website_link_signal(dom_items: Iterable[dict[str, str]]) -> bool:
    for item in dom_items:
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get(key, "")) for key in ("text", "aria", "title"))
        hrefish = " ".join(str(item.get(key, "")) for key in ("href", "attrs"))
        if _WEBSITE_LABEL_RE.search(label) and ("http" in hrefish or "clck" in hrefish or "url" in hrefish):
            return True
    return False


def _candidate_urls_from_dom_items(dom_items: Iterable[dict[str, str]]) -> list[str]:
    link_candidates: list[str] = []
    for item in dom_items:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href", ""))
        link_candidates.append(href)
        link_candidates.extend(extract_urls_from_text(href))
    return unique(clean_url(url) for url in link_candidates)


async def collect_org_page_snapshot(page) -> OrgPageSnapshot:
    raw_phones = [normalize_phone(item) for item in await href_values(page, "tel:")]
    raw_emails = await href_values(page, "mailto:")
    dom_items = await collect_dom_link_items(page)
    try:
        body_text = await page.locator("body").inner_text(timeout=2_000)
    except Exception:
        body_text = ""
    try:
        contact_text = "\n".join(
            await page.locator(
                '[class*="business-contacts"], [class*="contact"], a[href^="tel:"]'
            ).all_inner_texts()
        )
    except Exception:
        contact_text = ""
    try:
        html_text = await page.content()
    except Exception:
        html_text = ""

    link_candidates = _candidate_urls_from_dom_items(dom_items)
    external_links, unresolved_redirects = await resolve_redirect_links_with_status(
        page,
        unique(clean_url(url) for url in link_candidates),
    )

    social_links = unique(canonical_social_url(url) for url in external_links if is_social_url(url))
    phones = unique(
        phone
        for phone in [
            *raw_phones,
            *phones_from_links(social_links),
            *phones_from_text(contact_text),
        ]
        if phone
    )[:2]
    emails = unique(raw_emails)
    websites = unique(url for url in external_links if is_business_website(url))
    return OrgPageSnapshot(
        dom_items=dom_items,
        body_text=body_text,
        html_text=html_text,
        external_links=external_links,
        unresolved_redirects=unresolved_redirects,
        social_links=social_links,
        phones=phones,
        emails=emails,
        websites=websites,
        website_link_signal=_website_link_signal(dom_items),
    )


def merge_org_snapshots(snapshots: Iterable[OrgPageSnapshot]) -> OrgPageSnapshot:
    items = list(snapshots)
    if not items:
        return OrgPageSnapshot([], "", "", [], [], [], [], [], [], False)
    return OrgPageSnapshot(
        dom_items=[item for snapshot in items for item in snapshot.dom_items],
        body_text="\n".join(snapshot.body_text for snapshot in items if snapshot.body_text),
        html_text="\n".join(snapshot.html_text for snapshot in items if snapshot.html_text),
        external_links=unique(url for snapshot in items for url in snapshot.external_links),
        unresolved_redirects=unique(url for snapshot in items for url in snapshot.unresolved_redirects),
        social_links=unique(url for snapshot in items for url in snapshot.social_links),
        phones=unique(phone for snapshot in items for phone in snapshot.phones),
        emails=unique(email for snapshot in items for email in snapshot.emails),
        websites=unique(url for snapshot in items for url in snapshot.websites),
        website_link_signal=any(snapshot.website_link_signal for snapshot in items),
    )


def _ambiguous_unresolved_redirects(urls: Iterable[str]) -> list[str]:
    ambiguous: list[str] = []
    for url in urls:
        host = _url_host(url)
        if not host:
            continue
        # yandex.ru/clck часто встречается в служебной разметке; сам по себе это
        # не доказательство сайта. Если это настоящая кнопка "Сайт", её поймает
        # website_link_signal по тексту/aria/title элемента.
        if host_matches(host, YANDEX_HOST_MARKERS):
            continue
        ambiguous.append(url)
    return unique(ambiguous)


def strict_website_status(title: str, snapshot: OrgPageSnapshot, snapshot_count: int) -> str:
    if snapshot.websites:
        return "present"
    if not title.strip():
        return "unknown"
    # Если видим кнопку/редирект сайта, но не смогли раскрыть итоговый URL, не ставим ложное "нет сайта".
    if snapshot.website_link_signal or _ambiguous_unresolved_redirects(snapshot.unresolved_redirects):
        return "unknown"
    min_snapshots = max(1, int(getattr(settings, "STRICT_ABSENCE_MIN_SNAPSHOTS", 2)))
    if snapshot_count >= min_snapshots:
        return "absent"
    return "unknown"


def canonical_org_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "org" not in parts:
        return url.split("?")[0]
    org_index = parts.index("org")
    if len(parts) < org_index + 3:
        return url.split("?")[0]
    canonical_parts = parts[: org_index + 3]
    return f"{parsed.scheme}://{parsed.netloc}/{'/'.join(canonical_parts)}/"


def org_slug_identity(url: str) -> str:
    parts = [part.casefold() for part in urlparse(url).path.split("/") if part]
    try:
        org_index = parts.index("org")
    except ValueError:
        return ""
    if len(parts) <= org_index + 1:
        return ""
    return parts[org_index + 1]


def clean_rating_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) > 80:
        return ""
    match = re.search(r"(?<!\d)([0-5][.,]\d)(?!\d)", text)
    if not match:
        return ""
    rating = match.group(1).replace(".", ",")
    try:
        numeric = float(rating.replace(",", "."))
    except ValueError:
        return ""
    return rating if 0.0 <= numeric <= 5.0 else ""


def clean_reviews_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    match = re.search(
        r"(?<!\d)(\d[\d\s]{0,8})\s+(оцен(?:ка|ки|ок)|отзыв(?:а|ов)?)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""
    count = re.sub(r"\s+", "", match.group(1))
    return f"{count} {match.group(2)}"


def clean_address_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    text = re.sub(r"^Адрес\s*:?\s*", "", text, flags=re.IGNORECASE)
    text = re.split(
        r"\s+(?:Маршрут|Контакты|Время работы|График|Исправить неточность|Как добраться)\b",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    return text[:180].rstrip(" ,.;")


def detect_website_platform_from_html(url: str, html_text: str) -> str:
    host = (urlparse(url).hostname or "").casefold()
    text = (html_text or "").casefold()
    if any(
        marker in text
        for marker in (
            "made on tilda",
            "сделано на tilda",
            "tildacdn.",
            "tilda-blocks-",
            "tilda-zero-block",
        )
    ) or host.endswith((".tilda.ws", ".tilda.cc")):
        return "tilda"
    if any(
        marker in text
        for marker in (
            'content="wordpress',
            "wordpress",
            "/wp-content/",
            "/wp-includes/",
            "wp-json",
        )
    ):
        return "wordpress"
    if host.endswith((".clients.site", ".business.site")) or any(
        marker in text
        for marker in (
            "сайт от яндекс бизнеса",
            "сделано в яндекс бизнесе",
            "yandex business site",
            "yandex-business-site",
        )
    ):
        return "yandex_business"
    return ""


async def audit_website_platform(page, website_url: str) -> str:
    if not website_url or not int(_runtime_setting("WEBSITE_PLATFORM_AUDIT", 0)):
        return ""
    audit_page = await page.context.new_page()
    try:
        await audit_page.goto(website_url, wait_until="domcontentloaded", timeout=20_000)
        html_text = await audit_page.content()
        return detect_website_platform_from_html(audit_page.url or website_url, html_text)
    except Exception:
        return ""
    finally:
        await audit_page.close()


async def _collect_leads_for_job(
    job: MapsScrapeJob,
    *,
    context: Any | None = None,
    proxy_pool: _PlaywrightProxyPool | None = None,
    skip_urls: set[str],
    logger: LogFn,
    delay_seconds: float,
    light_parse: bool,
    stop_after_no_site: int | None,
    contact_filter: str,
    scan_limit_override: int | None = None,
    on_lead_checked: LeadCheckedFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[Lead]:
    if context is None and proxy_pool is None:
        raise ValueError("Нужен context или proxy_pool.")
    search_context = await proxy_pool.context_for_card(1) if proxy_pool else context
    if search_context is None:
        raise ValueError("Не удалось открыть контекст Chromium для поиска.")

    search_text = " ".join(part for part in [job.location.strip(), job.query.strip()] if part)
    default_origin = settings.YANDEX_MAPS_ORIGIN.rstrip("/")
    search_url = job.search_url or f"{default_origin}/maps/?text={quote_plus(search_text)}"
    page = await search_context.new_page()
    parallel_tabs = _parallel_org_tabs()
    if proxy_pool and parallel_tabs > 1:
        logger(
            "При ротации прокси PARALLEL_ORG_TABS=%s: вкладки в одной пачке могут идти с разных IP."
            % parallel_tabs
        )
    card_wait_ms = 450 if light_parse else 750
    card_logger = CardLogBatcher(logger, _card_log_batch_size())

    try:
        logger(f"Открываю Яндекс Карты: {search_text}")
        await open_maps_search_page(page, search_url, logger, search_text_fallback=search_text)
        await accept_cookies(page)
        await page.wait_for_timeout(900 if light_parse else 1_300)
        try:
            await _raise_if_yandex_service_page(page, context="поиск")
        except YandexServicePageError as exc:
            logger(f"{exc}. Останавливаю сбор, чтобы не усиливать капчу.")
            return []

        registry = len(skip_urls)
        target_no_site = stop_after_no_site
        scan_limit = (
            job.limit
            if target_no_site is None
            else max(target_no_site, scan_limit_override or _no_site_scan_limit(target_no_site))
        )
        collection_limit = (
            _feed_collection_target(scan_limit, registry)
            if target_no_site is None
            else _no_site_collection_target(scan_limit, registry)
        )
        if target_no_site is None:
            logger(
                "Лента выдачи: скроллю до ~%s уникальных /org/ "
                "(лимит проверки %s, реестр %s шт.)."
                % (collection_limit, scan_limit, registry)
            )
        else:
            logger(
                "Лента выдачи: ищу именно %s подтверждённых без сайта; "
                "проверю до %s карточек и соберу до ~%s ссылок /org/ "
                "(реестр %s шт.)."
                % (target_no_site, scan_limit, collection_limit, registry)
            )
        collected_urls = await collect_org_urls(
            page,
            limit=collection_limit,
            log=logger,
            should_stop=should_stop,
        )
        priority_urls = [
            canonical_org_url(url)
            for url in (job.priority_urls or [])
            if is_yandex_maps_org_url(url)
        ]
        org_urls = unique([*priority_urls, *collected_urls])
        if priority_urls:
            logger(f"Приоритетные ссылки поставлены первыми: {len(priority_urls)}.")
        logger(f"Собрано ссылок на карточки (уникальных): {len(org_urls)}")
        logger(f"Параллельных вкладок для карточек организаций: {parallel_tabs}")

        leads: list[Lead] = []
        seen_urls: set[str] = set()
        visited_card = False

        def record_card(idx: int, nav_u: str, lead: Lead) -> None:
            card_logger.append(idx, len(org_urls), nav_u, lead)

        if parallel_tabs <= 1:
            for index, url in enumerate(org_urls, start=1):
                if should_stop and should_stop():
                    logger("Парсинг остановлен пользователем.")
                    break
                if _scrape_should_stop(leads, scan_limit, stop_after_no_site, contact_filter):
                    break
                clean_url = canonical_org_url(url)
                if normalize_url_identity(clean_url) in skip_urls:
                    logger(f"[{index}/{len(org_urls)}] Уже обработано, пропускаю: {clean_url}")
                    continue
                if clean_url in seen_urls:
                    continue
                seen_urls.add(clean_url)
                nav_url = normalize_org_nav_url(clean_url)
                visited_card = True
                card_started_at = time.monotonic()
                try:
                    if proxy_pool:
                        card_context = await proxy_pool.context_for_card(index)
                        card_page = await card_context.new_page()
                        try:
                            await open_maps_org_page(card_page, nav_url, logger)
                            await card_page.wait_for_timeout(card_wait_ms)
                            lead = await parse_org_page(card_page, clean_url, light=light_parse)
                        finally:
                            await card_page.close()
                    else:
                        await open_maps_org_page(page, nav_url, logger)
                        await page.wait_for_timeout(card_wait_ms)
                        lead = await parse_org_page(page, clean_url, light=light_parse)
                except YandexServicePageError as exc:
                    logger(f"[{index}/{len(org_urls)}] {exc}. Останавливаю сбор, чтобы не усиливать капчу.")
                    break
                if is_yandex_service_lead(lead):
                    logger(f"[{index}/{len(org_urls)}] Служебная страница Яндекса вместо карточки. Останавливаю сбор.")
                    break
                record_card(index, nav_url, lead)
                if lead.name:
                    leads.append(lead)
                    if on_lead_checked:
                        on_lead_checked(lead)
                await _polite_card_pause(
                    len(leads),
                    delay_seconds,
                    logger,
                    elapsed_seconds=time.monotonic() - card_started_at,
                )
        else:

            async def fetch_org_card(list_pos: int, clean_u: str, nav_u: str) -> tuple[int, Lead]:
                card_context = await proxy_pool.context_for_card(list_pos) if proxy_pool else context
                if card_context is None:
                    raise RuntimeError("Не удалось открыть контекст Chromium для карточки.")
                tab = await card_context.new_page()
                try:
                    await open_maps_org_page(tab, nav_u, logger)
                    await tab.wait_for_timeout(card_wait_ms)
                    lead_obj = await parse_org_page(tab, clean_u, light=light_parse)
                    return list_pos, lead_obj
                finally:
                    await tab.close()

            pending_batch: list[tuple[int, str, str]] = []
            captcha_seen = False

            async def flush_batch() -> None:
                nonlocal leads, visited_card, captcha_seen
                if not pending_batch:
                    return
                batch_started_at = time.monotonic()
                tasks = [fetch_org_card(idx, cu, nv) for idx, cu, nv in pending_batch]
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                for item, outcome in zip(pending_batch, outcomes):
                    idx, _, nav_u = item
                    if isinstance(outcome, Exception):
                        if isinstance(outcome, YandexServicePageError):
                            logger(
                                f"[{idx}/{len(org_urls)}] {outcome}. Останавливаю сбор, чтобы не усиливать капчу."
                            )
                            captcha_seen = True
                            continue
                        logger(f"[{idx}/{len(org_urls)}] Ошибка вкладки: {outcome}\n{nav_u}")
                        continue
                    _, lead = outcome
                    visited_card = True
                    if is_yandex_service_lead(lead):
                        logger(
                            f"[{idx}/{len(org_urls)}] Служебная страница Яндекса вместо карточки. "
                            "Останавливаю сбор."
                        )
                        captcha_seen = True
                        continue
                    record_card(idx, nav_u, lead)
                    if lead.name:
                        leads.append(lead)
                        if on_lead_checked:
                            on_lead_checked(lead)
                pending_batch.clear()
                await _polite_card_pause(
                    len(leads),
                    delay_seconds,
                    logger,
                    elapsed_seconds=time.monotonic() - batch_started_at,
                )

            for index, url in enumerate(org_urls, start=1):
                if should_stop and should_stop():
                    logger("Парсинг остановлен пользователем.")
                    break
                if captcha_seen:
                    break
                if _scrape_should_stop(leads, scan_limit, stop_after_no_site, contact_filter):
                    break
                clean_url = canonical_org_url(url)
                if normalize_url_identity(clean_url) in skip_urls:
                    logger(f"[{index}/{len(org_urls)}] Уже обработано, пропускаю: {clean_url}")
                    continue
                if clean_url in seen_urls:
                    continue
                seen_urls.add(clean_url)
                nav_url = normalize_org_nav_url(clean_url)
                pending_batch.append((index, clean_url, nav_url))
                if len(pending_batch) >= parallel_tabs:
                    await flush_batch()
                if captcha_seen:
                    break
                if _scrape_should_stop(leads, scan_limit, stop_after_no_site, contact_filter):
                    break

            if (
                not captcha_seen
                and not _scrape_should_stop(leads, scan_limit, stop_after_no_site, contact_filter)
                and pending_batch
            ):
                await flush_batch()

        card_logger.flush()

        if not leads and not visited_card and not org_urls:
            try:
                lead = await parse_org_page(page, page.url, light=light_parse)
            except YandexServicePageError as exc:
                logger(f"{exc}. Не добавляю служебную страницу в лиды.")
                lead = Lead(name="")
            if lead.name:
                leads.append(lead)

        eligible_count = _eligible_target_leads(leads, contact_filter)
        if stop_after_no_site is None:
            if len(leads) < job.limit:
                logger(
                    f"Собрано организаций: {len(leads)} из запрошенного лимита {job.limit}. "
                    f"Уникальных ссылок /org/ в выдаче: {len(org_urls)} "
                    f"(если мало — в городе мало точек или мало новых после фильтра реестра)."
                )
        else:
            if eligible_count >= stop_after_no_site:
                logger(f"Цель выполнена: подтверждённых без сайта {eligible_count} из {stop_after_no_site}.")
            else:
                logger(
                    f"Цель не добрана: подтверждённых без сайта {eligible_count} из {stop_after_no_site}; "
                    f"проверено карточек: {len(leads)}, уникальных ссылок /org/: {len(org_urls)}. "
                    "Возможные причины: выдача закончилась, часть организаций с сайтом, нет контактов под выбранный фильтр "
                    "или Яндекс показал служебную страницу."
                )
        return leads
    finally:
        await page.close()


async def _wait_with_browser_open(
    seconds: float,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if should_stop and should_stop():
            return False
        await asyncio.sleep(min(1.0, deadline - time.monotonic()))
    return True


async def _collect_long_mode_urls(
    job: MapsScrapeJob,
    *,
    limit: int,
    context: Any | None,
    proxy_pool: _PlaywrightProxyPool | None,
    logger: LogFn,
    should_stop: Callable[[], bool] | None,
) -> list[str]:
    search_context = await proxy_pool.context_for_card(1) if proxy_pool else context
    if search_context is None:
        raise RuntimeError("Не удалось открыть контекст Chromium для поиска.")
    search_text = " ".join(part for part in [job.location.strip(), job.query.strip()] if part)
    search_url = job.search_url or (
        f"{settings.YANDEX_MAPS_ORIGIN.rstrip('/')}/maps/?text={quote_plus(search_text)}"
    )
    captcha_delay = float(_runtime_setting("CAPTCHA_RETRY_SECONDS", 600))

    while not (should_stop and should_stop()):
        page = await search_context.new_page()
        try:
            await open_maps_search_page(page, search_url, logger, search_text_fallback=search_text)
            await accept_cookies(page)
            await page.wait_for_timeout(1_300)
            await _raise_if_yandex_service_page(page, context="поиск")
            return await collect_org_urls(
                page,
                limit=limit,
                log=lambda _: None,
                should_stop=should_stop,
            )
        except YandexServicePageError:
            logger(
                f"Яндекс показал капчу при сборе «{search_text}». "
                f"Вкладка закрыта, браузер остаётся открыт. Повтор через {int(captcha_delay // 60)} мин."
            )
        finally:
            await page.close()
        if not await _wait_with_browser_open(captcha_delay, should_stop=should_stop):
            break
    return []


async def _check_long_mode_pool(
    pool: list[tuple[int, str]],
    *,
    context: Any | None,
    proxy_pool: _PlaywrightProxyPool | None,
    logger: LogFn,
    delay_seconds: float,
    light_parse: bool,
    on_lead_checked: LeadCheckedFn | None,
    should_stop: Callable[[], bool] | None,
    card_index_start: int,
    current_no_site: int,
    target_no_site: int,
    contact_filter: str,
) -> tuple[list[tuple[int, Lead]], int]:
    checked: list[tuple[int, Lead]] = []
    card_logger = CardLogBatcher(logger, _card_log_batch_size())
    captcha_delay = float(_runtime_setting("CAPTCHA_RETRY_SECONDS", 600))
    card_index = card_index_start
    shared_page = await context.new_page() if context is not None and proxy_pool is None else None

    try:
        for pool_index, (job_index, clean_url) in enumerate(pool, start=1):
            if should_stop and should_stop():
                break
            card_index += 1
            nav_url = normalize_org_nav_url(clean_url)
            started_at = time.monotonic()

            while not (should_stop and should_stop()):
                card_context = (
                    await proxy_pool.context_for_card(card_index) if proxy_pool else context
                )
                if card_context is None:
                    raise RuntimeError("Не удалось открыть контекст Chromium для карточки.")
                page = shared_page or await card_context.new_page()
                try:
                    await open_maps_org_page(page, nav_url, logger)
                    await page.wait_for_timeout(750 if not light_parse else 450)
                    lead = await parse_org_page(page, clean_url, light=light_parse)
                    if is_yandex_service_lead(lead):
                        raise YandexServicePageError("Служебная страница Яндекса")
                    break
                except YandexServicePageError:
                    logger(
                        f"Яндекс показал капчу на карточке. Браузер остаётся открыт. "
                        f"Повтор через {int(captcha_delay // 60)} мин."
                    )
                finally:
                    if shared_page is None:
                        await page.close()
                if not await _wait_with_browser_open(captcha_delay, should_stop=should_stop):
                    return checked, card_index
            else:
                break

            card_logger.append(pool_index, len(pool), nav_url, lead)
            if lead.name:
                checked.append((job_index, lead))
                if on_lead_checked:
                    on_lead_checked(lead)
            if lead_matches_target(lead, contact_filter):
                current_no_site += 1
            if current_no_site >= target_no_site:
                break
            await _polite_card_pause(
                len(checked),
                delay_seconds,
                logger,
                elapsed_seconds=time.monotonic() - started_at,
            )
    finally:
        if shared_page:
            await shared_page.close()

    card_logger.flush()
    return checked, card_index


async def _scrape_yandex_maps_long_pool(
    jobs: list[MapsScrapeJob],
    *,
    context: Any | None,
    proxy_pool: _PlaywrightProxyPool | None,
    skip_urls: set[str],
    logger: LogFn,
    delay_seconds: float,
    light_parse: bool,
    contact_filter: str,
    overall_no_site_limit: int,
    on_lead_checked: LeadCheckedFn | None,
    should_stop: Callable[[], bool] | None,
) -> list[list[Lead]]:
    results: list[list[Lead]] = [[] for _ in jobs]
    seen_urls = set(skip_urls)
    seen_slugs = {slug for url in skip_urls if (slug := org_slug_identity(url))}
    depths = [int(_runtime_setting("LONG_INITIAL_SEARCH_LINKS", 100)) for _ in jobs]
    pool_searches = max(1, int(_runtime_setting("LONG_POOL_SEARCHES", 4)))
    depth_step = max(1, int(_runtime_setting("LONG_NEXT_SEARCH_LINKS", 50)))
    cursor = 0
    card_index = 0
    empty_searches = 0

    priority = [
        (0, canonical_org_url(url))
        for url in (jobs[0].priority_urls or [])
        if is_yandex_maps_org_url(url)
    ] if jobs else []

    while _eligible_target_leads(
        [lead for group in results for lead in group],
        contact_filter,
    ) < overall_no_site_limit:
        if should_stop and should_stop():
            break

        pool: list[tuple[int, str]] = []
        if priority:
            for job_index, url in priority:
                identity = normalize_url_identity(url)
                slug = org_slug_identity(url)
                if not identity or identity in seen_urls or (slug and slug in seen_slugs):
                    continue
                seen_urls.add(identity)
                if slug:
                    seen_slugs.add(slug)
                pool.append((job_index, url))
            priority = []

        batch_indices: list[int] = []
        for _ in range(min(pool_searches, len(jobs))):
            job_index = cursor % len(jobs)
            cursor += 1
            batch_indices.append(job_index)
            collected = await _collect_long_mode_urls(
                jobs[job_index],
                limit=depths[job_index],
                context=context,
                proxy_pool=proxy_pool,
                logger=logger,
                should_stop=should_stop,
            )
            depths[job_index] += depth_step
            for url in collected:
                clean_url = canonical_org_url(url)
                identity = normalize_url_identity(clean_url)
                slug = org_slug_identity(clean_url)
                if not identity or identity in seen_urls or (slug and slug in seen_slugs):
                    continue
                seen_urls.add(identity)
                if slug:
                    seen_slugs.add(slug)
                pool.append((job_index, clean_url))

        labels = ", ".join(str(index + 1) for index in batch_indices)
        logger(
            f"Пул выдач {labels}/{len(jobs)}: новых карточек после удаления дублей "
            f"и обработанных — {len(pool)}."
        )

        if not pool:
            empty_searches += len(batch_indices)
            if empty_searches >= len(jobs):
                logger(
                    "Полный круг выдач не дал новых карточек. Браузер остаётся открыт; "
                    "следующий круг через 10 минут."
                )
                empty_searches = 0
                if not await _wait_with_browser_open(600, should_stop=should_stop):
                    break
            continue

        empty_searches = 0
        logger(f"Начинаю проверку пула: {len(pool)} карточек.")
        checked, card_index = await _check_long_mode_pool(
            pool,
            context=context,
            proxy_pool=proxy_pool,
            logger=logger,
            delay_seconds=delay_seconds,
            light_parse=light_parse,
            on_lead_checked=on_lead_checked,
            should_stop=should_stop,
            card_index_start=card_index,
            current_no_site=_eligible_target_leads(
                [lead for group in results for lead in group],
                contact_filter,
            ),
            target_no_site=overall_no_site_limit,
            contact_filter=contact_filter,
        )
        for job_index, lead in checked:
            results[job_index].append(lead)
        found = _eligible_target_leads(
            [lead for group in results for lead in group],
            contact_filter,
        )
        logger(
            f"Пул проверен: {len(checked)} карточек; подтверждено без сайта "
            f"{found}/{overall_no_site_limit}."
        )

    return results


async def scrape_yandex_maps_batch(
    jobs: list[MapsScrapeJob],
    *,
    headless: bool = True,
    delay_seconds: float = settings.PAGE_DELAY,
    skip_yandex_urls: Iterable[str] | None = None,
    log: LogFn | None = None,
    light_parse: bool = False,
    prefer_no_site_stop: bool = False,
    contact_filter: str = "all",
    overall_no_site_limit: int | None = None,
    on_lead_checked: LeadCheckedFn | None = None,
    runtime_settings: dict[str, float | int | str] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[list[Lead]]:
    if not jobs:
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run: pip install -r requirements.txt") from exc

    logger = log or (lambda _: None)
    skip_urls = {normalize_url_identity(url) for url in (skip_yandex_urls or [])}
    captcha_detected = False

    def tracked_logger(message: str) -> None:
        nonlocal captcha_detected
        lower = message.casefold()
        if "капч" in lower or ("служебн" in lower and "яндекс" in lower):
            captcha_detected = True
        logger(message)

    launch_kwargs, internal_proxy, pool_proxies = _chromium_browser_launch_plan(
        headless=headless,
        logger=tracked_logger,
    )

    browser: Any | None = None
    runtime_token = _RUNTIME_SETTINGS.set(runtime_settings or {})
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            proxy_pool = (
                _PlaywrightProxyPool(browser, pool_proxies, logger=logger) if pool_proxies else None
            )
            context = None if proxy_pool else await _new_browser_context(browser)
            if int(_runtime_setting("LONG_POOL_MODE", 0)):
                keeper_context = await proxy_pool.context_for_card(1) if proxy_pool else context
                keeper_page = await keeper_context.new_page() if keeper_context else None
                try:
                    results = await _scrape_yandex_maps_long_pool(
                        jobs,
                        context=context,
                        proxy_pool=proxy_pool,
                        skip_urls=skip_urls,
                        logger=logger,
                        delay_seconds=delay_seconds,
                        light_parse=light_parse,
                        contact_filter=contact_filter,
                        overall_no_site_limit=max(1, int(overall_no_site_limit or 1)),
                        on_lead_checked=on_lead_checked,
                        should_stop=should_stop,
                    )
                finally:
                    if keeper_page:
                        await keeper_page.close()
                if proxy_pool:
                    await proxy_pool.close()
                await browser.close()
                return results
            results: list[list[Lead]] = []
            remaining_by_group: dict[str, int] = {}
            for job_index, job in enumerate(jobs):
                if should_stop and should_stop():
                    results.extend([[] for _ in jobs[job_index:]])
                    break
                group_key = job.group_key or "__all__"
                remaining = (
                    remaining_by_group.setdefault(group_key, overall_no_site_limit)
                    if overall_no_site_limit is not None
                    else None
                )
                if remaining is not None and remaining <= 0:
                    results.append([])
                    continue
                target = remaining if remaining is not None else job.limit
                job_target = min(job.limit, target)
                job_leads: list[Lead] = []
                scanned_for_job = 0
                chunk_size = _no_site_scan_limit(job_target) if prefer_no_site_stop else job_target
                max_scan = max(job_target, int(getattr(settings, "NO_SITE_SCAN_MAX_CARDS", 1000)))
                query_variants = list(SEARCH_QUERY_VARIANTS.get(job.query.casefold(), (job.query,)))
                variant_index = 0

                while True:
                    if should_stop and should_stop():
                        break
                    found_no_site = _eligible_target_leads(job_leads, contact_filter)
                    target_left = max(0, job_target - found_no_site)
                    if target_left <= 0 or scanned_for_job >= max_scan:
                        break
                    if variant_index >= len(query_variants):
                        tracked_logger(
                            f"Варианты поиска по категории «{job.query}» закончились; "
                            f"найдено без сайта: {found_no_site} из {job_target}."
                        )
                        break
                    pass_scan_limit = min(chunk_size, max_scan - scanned_for_job)
                    variant_query = query_variants[variant_index]
                    effective_job = MapsScrapeJob(
                        query=variant_query,
                        location=job.location,
                        limit=target_left if prefer_no_site_stop else job_target,
                        search_url=job.search_url if variant_index == 0 else "",
                        priority_urls=job.priority_urls if scanned_for_job == 0 else None,
                    )
                    if variant_index > 0:
                        tracked_logger(
                            f"Цель не набрана, продолжаю поиск: «{variant_query}» "
                            f"(ещё нужно {target_left})."
                        )
                    variant_index += 1
                    leads = await _collect_leads_for_job(
                        effective_job,
                        context=context,
                        proxy_pool=proxy_pool,
                        skip_urls=skip_urls,
                        logger=tracked_logger,
                        delay_seconds=delay_seconds,
                        light_parse=light_parse,
                        stop_after_no_site=target_left if prefer_no_site_stop else None,
                        contact_filter=contact_filter,
                        scan_limit_override=pass_scan_limit if prefer_no_site_stop else None,
                        on_lead_checked=on_lead_checked,
                        should_stop=should_stop,
                    )
                    job_leads.extend(leads)
                    scanned_for_job += len(leads)
                    for lead in leads:
                        identity = normalize_url_identity(canonical_org_url(lead.yandex_url))
                        if identity:
                            skip_urls.add(identity)
                    if captcha_detected or not prefer_no_site_stop:
                        break
                    if _eligible_target_leads(job_leads, contact_filter) < job_target:
                        tracked_logger(
                            f"Р¦РµР»СЊ РµС‰С‘ РЅРµ РЅР°Р±СЂР°РЅР°: РїСЂРѕРІРµСЂРµРЅРѕ {scanned_for_job}, "
                            f"РґРѕР±Р°РІР»СЏСЋ СЃР»РµРґСѓСЋС‰СѓСЋ РїРѕСЂС†РёСЋ РґРѕ {chunk_size} РєР°СЂС‚РѕС‡РµРє."
                        )

                results.append(job_leads)
                if remaining is not None:
                    remaining -= _eligible_target_leads(job_leads, contact_filter)
                    remaining_by_group[group_key] = remaining
                if captcha_detected:
                    results.extend([[] for _ in jobs[job_index + 1 :]])
                    break
            if proxy_pool:
                await proxy_pool.close()
            await browser.close()
            return results
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if internal_proxy:
            internal_proxy.shutdown()
            internal_proxy.server_close()
        _RUNTIME_SETTINGS.reset(runtime_token)


def is_yandex_maps_org_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host or "yandex" not in host:
        return False
    return "/org/" in parsed.path


async def scrape_yandex_org_url(
    url: str,
    *,
    headless: bool = True,
    log: LogFn | None = None,
    light_parse: bool = False,
) -> Lead:
    if not is_yandex_maps_org_url(url):
        raise ValueError("Нужна ссылка на карточку организации Яндекс.Карт (/org/).")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run: pip install -r requirements.txt") from exc

    clean_url = canonical_org_url(url)
    nav_url = normalize_org_nav_url(clean_url)
    logger = log or (lambda _: None)

    launch_kwargs, internal_proxy, pool_proxies = _chromium_browser_launch_plan(
        headless=headless,
        logger=logger,
    )

    browser: Any | None = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            proxy_pool = (
                _PlaywrightProxyPool(browser, pool_proxies, logger=logger) if pool_proxies else None
            )
            context = None if proxy_pool else await _new_browser_context(browser)
            page_context = await proxy_pool.context_for_card(1) if proxy_pool else context
            if page_context is None:
                raise RuntimeError("Не удалось открыть контекст Chromium.")
            page = await page_context.new_page()
            try:
                logger(f"Открываю карточку: {nav_url}")
                await open_maps_org_page(page, nav_url, logger)
                await accept_cookies(page)
                await page.wait_for_timeout(750)
                lead = await parse_org_page(page, clean_url, light=light_parse)
            finally:
                await page.close()
            if proxy_pool:
                await proxy_pool.close()
            await browser.close()
            if not lead.name:
                raise RuntimeError("Не удалось прочитать название организации на странице.")
            return lead
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if internal_proxy:
            internal_proxy.shutdown()
            internal_proxy.server_close()


async def scrape_yandex_maps(
    query: str,
    location: str = "",
    limit: int = 20,
    headless: bool = True,
    delay_seconds: float = settings.PAGE_DELAY,
    skip_yandex_urls: Iterable[str] | None = None,
    search_url: str = "",
    log: LogFn | None = None,
    *,
    light_parse: bool = False,
    prefer_no_site_stop: bool = False,
    contact_filter: str = "all",
    on_lead_checked: LeadCheckedFn | None = None,
    runtime_settings: dict[str, float | int | str] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[Lead]:
    job = MapsScrapeJob(query=query, location=location, limit=limit, search_url=search_url)
    batches = await scrape_yandex_maps_batch(
        [job],
        headless=headless,
        delay_seconds=delay_seconds,
        skip_yandex_urls=skip_yandex_urls,
        log=log,
        light_parse=light_parse,
        prefer_no_site_stop=prefer_no_site_stop,
        contact_filter=contact_filter,
        overall_no_site_limit=limit if prefer_no_site_stop else None,
        on_lead_checked=on_lead_checked,
        runtime_settings=runtime_settings,
        should_stop=should_stop,
    )
    return batches[0] if batches else []


async def accept_cookies(page) -> None:
    for label in ("Принять", "Accept", "Хорошо", "Понятно"):
        try:
            button = page.get_by_role("button", name=re.compile(label, re.IGNORECASE)).first
            if await button.count():
                await button.click(timeout=1_000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


async def collect_org_urls(
    page,
    limit: int,
    log: LogFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[str]:
    urls: list[str] = []
    canonical_seen: set[str] = set()
    stable_rounds = 0
    last_logged_count = 0
    logger = log or (lambda _: None)
    max_rounds = min(600, max(40, limit // 2 + 30))

    current = await page.locator('a[href*="/org/"], a[href*="/maps/org/"]').evaluate_all(
        "anchors => anchors.map(a => a.href).filter(Boolean)"
    )
    urls = unique([*urls, *current])
    for raw in current:
        if "/org/" not in raw and "/maps/org/" not in raw:
            continue
        canonical_seen.add(canonical_org_url(raw))
    unique_count = len(canonical_seen)

    for _ in range(max_rounds):
        if should_stop and should_stop():
            logger("Сбор ссылок остановлен пользователем.")
            break
        if unique_count != last_logged_count and (
            unique_count <= 1
            or unique_count >= limit
            or unique_count - last_logged_count >= 20
        ):
            last_logged_count = unique_count
            logger(f"Поиск: собрано организаций {unique_count}/{limit}.")
        if unique_count >= limit:
            break

        for phrase in ("Показать ещё", "Ещё результаты", "Показать все", "Ещё"):
            try:
                btn = page.get_by_role("button", name=re.compile(phrase, re.IGNORECASE)).first
                if await btn.count():
                    await btn.click(timeout=900)
                    await page.wait_for_timeout(450)
                    break
            except Exception:
                continue

        before_count = len(canonical_seen)
        scroll_step = int(_runtime_setting("SEARCH_SCROLL_STEP_PX", 390))
        try:
            await page.evaluate(
                """
                step => {
                    const anchors = Array.from(
                        document.querySelectorAll('a[href*="/org/"], a[href*="/maps/org/"]')
                    );
                    const candidates = [];
                    for (const anchor of anchors) {
                        let node = anchor.parentElement;
                        while (node && node !== document.body) {
                            if (node.scrollHeight > node.clientHeight + 40) {
                                candidates.push(node);
                            }
                            node = node.parentElement;
                        }
                    }
                    const uniqueCandidates = Array.from(new Set(candidates));
                    uniqueCandidates.sort((a, b) => {
                        const aLinks = a.querySelectorAll('a[href*="/org/"], a[href*="/maps/org/"]').length;
                        const bLinks = b.querySelectorAll('a[href*="/org/"], a[href*="/maps/org/"]').length;
                        return bLinks - aLinks || b.scrollHeight - a.scrollHeight;
                    });
                    const fallback = Array.from(document.querySelectorAll('*'))
                        .filter(el => el.scrollHeight > el.clientHeight + 100)
                        .sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
                    const target = uniqueCandidates[0] || fallback || document.scrollingElement || document.body;
                    target.scrollBy({ top: step, behavior: 'auto' });
                }
                """,
                scroll_step,
            )
        except Exception:
            pass
        try:
            await page.mouse.wheel(0, scroll_step)
        except Exception:
            pass
        scroll_pause_ms = int(_runtime_setting("SEARCH_SCROLL_PAUSE_MS", 450))
        await page.wait_for_timeout(scroll_pause_ms)

        current = await page.locator('a[href*="/org/"], a[href*="/maps/org/"]').evaluate_all(
            "anchors => anchors.map(a => a.href).filter(Boolean)"
        )
        urls = unique([*urls, *current])
        for raw in current:
            if "/org/" not in raw and "/maps/org/" not in raw:
                continue
            canonical_seen.add(canonical_org_url(raw))
        unique_count = len(canonical_seen)

        if unique_count != last_logged_count and (
            unique_count <= 1 or unique_count >= limit or unique_count - last_logged_count >= 20
        ):
            last_logged_count = unique_count
            logger(f"Поиск: собрано организаций {unique_count}/{limit}.")
        if unique_count >= limit:
            break

        if unique_count > before_count:
            stable_rounds = 0
        else:
            stable_rounds += 1
            if stable_rounds in {4, 8}:
                try:
                    await page.evaluate(
                        """
                        () => {
                            const target = document.querySelector('[data-maps-parser-scroll-target]')
                                || document.scrollingElement
                                || document.body;
                            target.scrollTo({ top: target.scrollHeight, behavior: 'auto' });
                        }
                        """
                    )
                    await page.wait_for_timeout(scroll_pause_ms * 2)
                except Exception:
                    pass
        if stable_rounds >= 12:
            break

    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if "/org/" not in url and "/maps/org/" not in url:
            continue
        clean = canonical_org_url(url)
        if clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


async def parse_org_page(page, yandex_url: str, *, light: bool = False) -> Lead:
    await _raise_if_yandex_service_page(page, context=yandex_url)
    await wait_for_org_card_content(page, light=light)
    await reveal_phone(page, light=light)

    title = await first_text(
        page,
        [
            'h1[class*="card-title"]',
            '[class*="card-title-view__title"]',
            '[class*="orgpage-header-view__header"] h1',
            "h1",
        ],
    )
    address = await first_text(
        page,
        [
            '[class*="business-contacts-view__address"]',
            '[class*="card-feature-view__main-content"]',
            '[aria-label*="Адрес"]',
        ],
    )
    address = clean_address_text(address)
    rating = ""
    reviews = ""
    hours = ""
    categories: list[str] = []
    if not light:
        rating = await first_text(
            page,
            [
                '[class*="business-rating-badge-view__rating-text"]',
                '[class*="business-summary-rating-badge-view__rating"]',
                '[class*="rating"]',
            ],
        )
        reviews = await first_text(
            page,
            [
                '[class*="business-rating-amount-view"]',
                '[class*="reviews"]',
            ],
        )
        hours = await first_text(
            page,
            [
                '[class*="business-working-status-view"]',
                '[class*="business-card-working-status-view"]',
            ],
        )
        categories = await category_texts(page)
        rating = clean_rating_text(rating)
        reviews = clean_reviews_text(reviews)

    snapshots: list[OrgPageSnapshot] = []
    attempts = max(0, int(_runtime_setting("WEBSITE_RECHECK_ATTEMPTS", 2)))
    delay_ms = max(0, int(_runtime_setting("WEBSITE_RECHECK_DELAY_MS", 1500)))
    for attempt in range(attempts + 1):
        snapshot = await collect_org_page_snapshot(page)
        snapshots.append(snapshot)
        if snapshot.websites:
            break
        if attempt < attempts:
            await page.wait_for_timeout(delay_ms if not light else max(400, delay_ms // 2))
            await reveal_phone(page, light=light)

    merged_snapshot = merge_org_snapshots(snapshots)
    website_status = strict_website_status(title, merged_snapshot, len(snapshots))
    website = merged_snapshot.websites[0] if merged_snapshot.websites else ""
    website_platform = await audit_website_platform(page, website)
    contact_status = (
        "present"
        if (merged_snapshot.phones or merged_snapshot.emails or merged_snapshot.social_links)
        else ("absent" if title else "unknown")
    )

    return Lead(
        name=title,
        categories=categories,
        address=address,
        phone=", ".join(unique(merged_snapshot.phones)),
        email=", ".join(unique(merged_snapshot.emails)),
        website=website,
        website_status=website_status,
        contact_status=contact_status,
        social_links=merged_snapshot.social_links,
        rating=rating,
        reviews=reviews,
        hours=hours,
        yandex_url=yandex_url,
        raw={
            "external_links": merged_snapshot.external_links,
            "unresolved_redirects": merged_snapshot.unresolved_redirects,
            "website_candidates": merged_snapshot.websites,
            "website_link_signal": merged_snapshot.website_link_signal,
            "website_checks": len(snapshots),
            "website_status": website_status,
            "contact_status": contact_status,
            "website_platform": website_platform,
        },
    )


async def reveal_phone(page, *, light: bool = False) -> None:
    labels = (
        "Показать телефон",
        "Показать номер",
        "Показать телефоны",
        "Показать все телефоны",
        "Показать контакт",
        "Показать контакты",
    )
    selectors = [
        f'button:has-text("{label}")' for label in labels
    ] + [
        f'[role="button"]:has-text("{label}")' for label in labels
    ]
    pause_ms = 500 if light else 900
    clicked = False

    # Кнопка телефона иногда находится ниже видимой части карточки; мягко прокручиваем возможные панели.
    for scroll_round in range(3):
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    await locator.click(timeout=1_500)
                    clicked = True
                    await page.wait_for_timeout(pause_ms)
            except Exception:
                continue
        if clicked:
            # После раскрытия одной кнопки могут появиться дополнительные номера.
            clicked = False
            continue
        try:
            await page.evaluate(
                """
                step => {
                    const candidates = Array.from(document.querySelectorAll('*'))
                        .filter(el => el.scrollHeight > el.clientHeight + 80)
                        .sort((a, b) => b.clientHeight - a.clientHeight);
                    const target = candidates[0] || document.scrollingElement || document.body;
                    target.scrollBy({ top: step, behavior: 'auto' });
                }
                """,
                450,
            )
            await page.wait_for_timeout(250 if light else 400)
        except Exception:
            break


async def first_text(page, selectors: list[str]) -> str:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                text = normalize_text(await locator.inner_text(timeout=1_500))
                if text:
                    return text
        except Exception:
            continue
    return ""


async def href_values(page, prefix: str) -> list[str]:
    values = await page.locator(f'a[href^="{prefix}"]').evaluate_all(
        """
        (anchors, prefix) => anchors
            .map(anchor => (anchor.getAttribute('href') || '').replace(prefix, '').trim())
            .filter(Boolean)
        """,
        prefix,
    )
    return unique(values)


def _messenger_phone_digits(raw: str) -> str | None:
    digits = re.sub(r"\D+", "", raw)
    if 10 <= len(digits) <= 15:
        return digits
    return None


def canonical_social_url(url: str) -> str:
    base = url.split("?")[0].strip()
    wa_match = re.search(r"https?://(?:[a-z0-9.-]*\.)?wa\.me/([^/?#]+)", base, re.I)
    if wa_match:
        digits = _messenger_phone_digits(wa_match.group(1))
        if digits:
            return f"https://wa.me/{digits}"
    tg_match = re.search(r"https?://(?:[a-z0-9.-]*\.)?t\.me/\+([^/?#]+)", base, re.I)
    if tg_match:
        digits = _messenger_phone_digits(tg_match.group(1))
        if digits:
            return f"https://t.me/+{digits}"
    return base


def phones_from_links(urls: Iterable[str]) -> list[str]:
    phones: list[str] = []
    for url in urls:
        normalized = canonical_social_url(url)
        match = re.search(r"https?://(?:[a-z0-9.-]*\.)?wa\.me/(\d+)", normalized, re.I)
        if match:
            phones.append(f"+{match.group(1)}")
            continue
        match = re.search(r"https?://(?:[a-z0-9.-]*\.)?t\.me/\+(\d+)", normalized, re.I)
        if match:
            phones.append(f"+{match.group(1)}")
    return unique(phones)


async def category_texts(page) -> list[str]:
    selectors = [
        '[class*="business-card-title-view__categories"] a',
        '[class*="business-categories"] a',
        '[class*="card-title-view__categories"] a',
        '[class*="breadcrumbs"] a',
    ]
    values: list[str] = []
    for selector in selectors:
        try:
            items = await page.locator(selector).evaluate_all(
                "elements => elements.map(element => element.innerText).filter(Boolean)"
            )
            values.extend(normalize_text(item) for item in items)
        except Exception:
            continue
    return unique(value for value in values if value and value.lower() not in {"карты", "организации"})
