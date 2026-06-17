from __future__ import annotations

import http.client
import json
import logging
import os
import queue
import re
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

from .crm import create_manager_batch, update_batch_status
from .models import GeneratedMessage, Lead
from .pipeline import LeadJobSpec, run_lead_job, run_lead_jobs_batch, run_td_draft_job
from .run_modes import get_run_mode
from .storage import lead_identity_key
from .yandex_maps import _resolve_a_via_public_dns, is_yandex_maps_org_url
from . import settings


logger = logging.getLogger(__name__)

_TELEGRAM_BEST_EFFORT_SEND_TIMEOUT_SEC = 6.0
_TELEGRAM_API_FALLBACK_IPS = (
    "149.154.167.220",
    "149.154.167.40",
    "149.154.175.50",
    "91.108.4.149",
)
_JOB_NOTIFY_QUEUE_MAX = 120
_JOB_NOTIFY_SENTINEL = object()
START_BUTTON_TEXT = "▶️ Старт"
STOP_BUTTON_TEXT = "⏹ Стоп"


def network_retry_delay(failure_count: int) -> float:
    if failure_count <= 1:
        return 10.0
    if failure_count == 2:
        return 30.0
    return 600.0


def is_network_error(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return isinstance(exc, (ConnectionError, OSError, TimeoutError, URLError)) or any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "network",
            "connection",
            "connect",
            "socket",
            "dns",
            "net::err",
            "name_not_resolved",
        )
    )


def _is_job_progress_ping(text: str) -> bool:
    head = text.strip().split("\n", 1)[0]
    return head.startswith("Поиск:") or (head.startswith("[") and "]" in head)


class JobNotifyQueue:
    """Фоновая очередь Telegram: парсер не ждёт sendMessage при обрывах сети."""

    def __init__(self, bot: TelegramLeadBot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._worker,
            name=f"tg-notify-{chat_id}",
            daemon=True,
        )
        self._started = False
        self._start_lock = threading.Lock()
        self._cancelled = threading.Event()

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._thread.start()
            self._started = True

    def enqueue(self, text: str) -> None:
        if self._cancelled.is_set():
            return
        self._ensure_started()
        if self._queue.qsize() >= _JOB_NOTIFY_QUEUE_MAX and _is_job_progress_ping(text):
            return
        self._queue.put(text)

    def enqueue_generated_card(self, message_item: GeneratedMessage) -> None:
        if self._cancelled.is_set():
            return
        self._ensure_started()
        self._queue.put(message_item)

    def enqueue_document(self, path: Path, caption: str = "") -> None:
        if self._cancelled.is_set():
            return
        self._ensure_started()
        self._queue.put(("document", path, caption))

    def cancel_pending(self) -> None:
        self._cancelled.set()

    def close(self, drain_timeout: float = 90.0) -> None:
        if not self._started:
            return
        deadline = time.time() + drain_timeout
        while self._queue.qsize() > 0 and time.time() < deadline:
            time.sleep(0.12)
        self._queue.put(_JOB_NOTIFY_SENTINEL)
        self._thread.join(timeout=max(1.0, deadline - time.time()))

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is _JOB_NOTIFY_SENTINEL:
                return
            if self._cancelled.is_set():
                continue
            try:
                if isinstance(item, GeneratedMessage):
                    self._bot.send_generated_message_card(self._chat_id, item, best_effort=True)
                elif isinstance(item, tuple) and len(item) == 3 and item[0] == "document":
                    self._bot.send_document(self._chat_id, item[1], item[2])
                else:
                    self._bot.send_message(self._chat_id, str(item), best_effort=True)
            except Exception as exc:
                logger.warning("Сообщение в Telegram не доставлено (задача продолжается): %s", exc)


HELP_TEXT = """Команды:
/menu или /start — меню с кнопками (категории, город, лимит, режим, окно браузера, затем «Запустить поиск»)
/city Волгоград — задать город текстом (если не из списка кнопок)
/limit 20 — лимит текстом (1–50)
/manager Иван — назначить менеджера для следующей пачки
/contacts phone|any|all — только с телефоном, с любым контактом или все
/leadstatus НОМЕР статус комментарий — обновить статус лида последней пачки
/resume — повторить прерванную капчей задачу
/td https://yandex.ru/maps/org/... — пробный черновик по одной карточке
/mode run — поиск + генерация ИИ; /mode scrape — список организаций без сайта
/status — город, лимит, режим, браузер, последняя задача
/help — это сообщение

Поиск по Яндекс.Картам запускается только кнопкой «▶️ Запустить поиск» в меню.
В меню переключатель «Окно Chromium» / «Без окна». Стартовое значение — в maps_parser/settings.py (TELEGRAM_BROWSER_VISIBLE).
"""


@dataclass(frozen=True, slots=True)
class CategoryChoice:
    label: str
    query: str
    path: str = ""
    category_slug: str = ""


@dataclass(frozen=True, slots=True)
class CityRoute:
    city_id: int
    slug: str


CATEGORY_CHOICES: list[CategoryChoice] = [
    CategoryChoice("Хорошие места", "хорошие места", "Хорошие места"),
    CategoryChoice("Где поесть", "где поесть", "Где поесть"),
    CategoryChoice("Отели", "отели", category_slug="hotels_housing"),
    CategoryChoice("Продукты", "продукты", "Продукты"),
    CategoryChoice("Аптеки", "аптеки", "Аптеки"),
    CategoryChoice("Торговые центры", "торговые центры", "Торговые центры"),
    CategoryChoice("Кафе", "кафе", "Кафе"),
    CategoryChoice("АЗС", "АЗС", "АЗС"),
    CategoryChoice("Музеи", "музеи", "Музеи"),
    CategoryChoice("Банкоматы", "банкоматы", "Банкоматы"),
    CategoryChoice("Автосервисы", "автосервисы", "Автосервисы"),
    CategoryChoice("Госуслуги", "госуслуги", path="Госуслуги"),
    CategoryChoice("Больницы", "больницы", "Больницы"),
    CategoryChoice("Салоны красоты", "салоны красоты", "Салоны красоты"),
    CategoryChoice("Спорт", "спорт", "Спорт"),
]


CITY_ROUTES = {
    "москва": CityRoute(213, "moscow"),
    "кисловодск": CityRoute(11062, "kislovodsk"),
    "волгоград": CityRoute(38, "volgograd"),
    "казань": CityRoute(43, "kazan"),
    "астрахань": CityRoute(37, "astrahan"),
    "санкт-петербург": CityRoute(2, "saint-petersburg"),
    "питер": CityRoute(2, "saint-petersburg"),
}

# Короткий суффикс callback_data (ASCII) → строка города для состояния и URL
CITY_SLUG_TO_CANONICAL: dict[str, str] = {
    "moscow": "Москва",
    "spb": "Санкт-Петербург",
    "kazan": "Казань",
    "volgograd": "Волгоград",
    "kislovodsk": "Кисловодск",
}

TELEGRAM_LIMIT_CAP = 50
LIMIT_PRESETS: tuple[int, ...] = (10, TELEGRAM_LIMIT_CAP)


def clamp_saved_limit(value: Any) -> int:
    try:
        limit = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if limit < 1:
        return 0
    return limit


def _default_show_browser_from_env() -> bool:
    return bool(settings.TELEGRAM_BROWSER_VISIBLE)


@dataclass(slots=True)
class ContactAction:
    lead_name: str
    card_text: str
    message_text: str
    contact_text: str
    sent: bool = False


@dataclass(slots=True)
class ChatState:
    city: str = ""
    limit: int = 0
    generate: bool = False
    last_summary: str = ""
    selected_category_indices: list[int] = field(default_factory=list)
    show_browser: bool = True
    contact_filter: str = "phone"
    manager: str = ""
    last_batch_id: str = ""
    resume_job: dict[str, Any] = field(default_factory=dict)
    priority_urls: list[str] = field(default_factory=list)
    awaiting_priority_urls: bool = False
    awaiting_city: bool = False
    awaiting_limit: bool = False
    run_profile: str = "fast"
    target_mode: str = "no_site"


@dataclass(slots=True)
class TelegramLeadBot:
    token: str
    output_dir: Path
    default_limit: int = 10
    default_city: str = ""
    headless: bool = True
    delay_seconds: float = settings.PAGE_DELAY
    allowed_chat_ids: set[int] = field(default_factory=set)
    api_base: str = "https://api.telegram.org"
    reconnect_delay_seconds: float = 10.0
    state_path: Path = field(init=False)
    state: dict[str, ChatState] = field(init=False)
    active_jobs: set[int] = field(init=False)
    stop_events: dict[int, threading.Event] = field(init=False)
    active_notifiers: dict[int, JobNotifyQueue] = field(init=False)
    contact_actions: dict[str, ContactAction] = field(init=False)
    contact_action_counter: int = field(init=False)
    contact_action_lock: threading.Lock = field(init=False)
    progress_items: dict[int, list[tuple[Lead, str]]] = field(init=False)
    progress_cities: dict[int, str] = field(init=False)
    progress_lock: threading.Lock = field(init=False)

    def __post_init__(self) -> None:
        self.default_limit = min(max(1, int(self.default_limit)), TELEGRAM_LIMIT_CAP)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.output_dir / "telegram_state.json"
        self.state = self.load_state()
        self.active_jobs: set[int] = set()
        self.stop_events = {}
        self.active_notifiers = {}
        self.contact_actions = {}
        self.contact_action_counter = 0
        self.contact_action_lock = threading.Lock()
        self.progress_items = {}
        self.progress_cities = {}
        self.progress_lock = threading.Lock()

    def run(self) -> None:
        log_level = getattr(logging, settings.TELEGRAM_BOT_LOG_LEVEL, logging.INFO)
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        username = self.wait_until_ready()
        self.ensure_long_polling()
        logger.info(
            "Бот @%s запущен (long polling). Разрешённые chat_id: %s",
            username,
            sorted(self.allowed_chat_ids) if self.allowed_chat_ids else "все",
        )
        print(f"Telegram bot @{username} is running. Press Ctrl+C to stop.")

        offset = 0
        while True:
            try:
                offset_before = offset
                updates = self.api_request(
                    "getUpdates",
                    {
                        "timeout": 30,
                        "offset": offset,
                        "allowed_updates": ["message", "edited_message", "callback_query"],
                    },
                ).get("result", [])
                for update in updates:
                    uid = int(update["update_id"])
                    offset = max(offset, uid + 1)
                    self.log_update(update)
                    self.handle_update(update)
                logger.info(
                    "Long poll завершён: апдейтов=%s offset %s→%s. "
                    "Если жмёшь кнопки, а апдейтов всегда 0 — второй процесс с тем же токеном, webhook или не тот бот.",
                    len(updates),
                    offset_before,
                    offset,
                )
            except KeyboardInterrupt:
                logger.info("Останов по Ctrl+C")
                print("Telegram bot stopped.")
                return
            except Exception as exc:
                logger.exception("Ошибка в цикле бота: %s", exc)
                print(f"Telegram bot error: {exc}")
                time.sleep(3)

    def wait_until_ready(self) -> str:
        while True:
            try:
                me = self.api_request("getMe")
                result = me.get("result") or {}
                logger.info(
                    "getMe: bot_user_id=%s @%s (%s) — убедись, что в Telegram открыт именно этот бот",
                    result.get("id"),
                    result.get("username", "?"),
                    result.get("first_name", ""),
                )
                return str(result.get("username", "bot"))
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"Telegram API is unavailable: {exc}")
                print(network_help_text())
                print(f"Retrying in {self.reconnect_delay_seconds:g} seconds. Press Ctrl+C to stop.")
                time.sleep(self.reconnect_delay_seconds)

    def ensure_long_polling(self) -> None:
        """Если у бота включён webhook, getUpdates не получает апдейты — кнопки «не работают»."""
        info = self.api_request("getWebhookInfo").get("result") or {}
        url = str(info.get("url") or "").strip()
        pending = int(info.get("pending_update_count") or 0)
        logger.info("getWebhookInfo: url=%r pending_updates=%s", url or "(нет)", pending)
        if url:
            logger.warning("Отключаю webhook (%s): нужен long polling в этом процессе.", url)
            print(f"Отключаю webhook ({url}): этот процесс работает через long polling.")
        self.api_request("deleteWebhook")
        after = self.api_request("getWebhookInfo").get("result") or {}
        url_after = str(after.get("url") or "").strip()
        if url_after:
            logger.error(
                "Webhook всё ещё активен после deleteWebhook (%s). Другой сервис мог сразу выставить его снова.",
                url_after,
            )
        else:
            logger.info("Webhook снят, long polling возможен.")

    def log_update(self, update: dict[str, Any]) -> None:
        uid = update.get("update_id")
        if "callback_query" in update:
            cq = update["callback_query"]
            data = str(cq.get("data", ""))
            msg = cq.get("message") or {}
            chat = msg.get("chat") or {}
            logger.info(
                "Апдейт %s: callback_query id=%s chat_id=%s data=%r msg_id=%s",
                uid,
                cq.get("id"),
                chat.get("id"),
                data[:128] + ("..." if len(data) > 128 else ""),
                msg.get("message_id"),
            )
            return
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        text = str(message.get("text", "")).strip()
        preview = text[:100] + ("..." if len(text) > 100 else "")
        logger.info(
            "Апдейт %s: message chat_id=%s text=%r",
            uid,
            chat.get("id"),
            preview,
        )

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback_query(update["callback_query"])
            return

        message = update.get("message") or update.get("edited_message") or {}
        text = str(message.get("text", "")).strip()
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        if not chat_id or not text:
            logger.info(
                "Пропуск message: chat_id=%s len(text)=%s (медиа/пусто — не команда)",
                chat_id,
                len(text),
            )
            return

        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            self.send_message(chat_id, f"Этот чат не разрешен. Chat ID: {chat_id}")
            return

        state = self.chat_state(chat_id)
        if state.awaiting_city and not text.startswith("/"):
            city = re.sub(r"\s+", " ", text).strip()
            cities = parse_cities(city)
            if not cities or len(city) > 300:
                self.send_message(chat_id, "Введите корректное название города.")
                return
            state.city = ", ".join(cities)
            state.awaiting_city = False
            self.save_state()
            self.send_main_menu(chat_id, f"Город сохранён: {city}")
            return
        if state.awaiting_limit and not text.startswith("/"):
            try:
                limit = parse_limit(text)
            except ValueError as exc:
                self.send_message(chat_id, str(exc))
                return
            state.limit = limit
            state.awaiting_limit = False
            self.save_state()
            self.send_main_menu(chat_id, f"Лимит сохранён: {limit}")
            return
        if state.awaiting_priority_urls and not text.startswith("/"):
            urls = parse_yandex_org_urls(text)
            if not urls:
                self.send_message(
                    chat_id,
                    "Ссылки не найдены. Отправь ссылки Яндекс.Карт вида https://yandex.ru/maps/org/...",
                    reply_markup={"inline_keyboard": [[{"text": "Отмена", "callback_data": "links:cancel"}]]},
                )
                return
            state.priority_urls = urls
            state.awaiting_priority_urls = False
            self.save_state()
            self.send_main_menu(chat_id, f"Приоритетных ссылок сохранено: {len(urls)}. Они проверятся первыми.")
            return

        if text == START_BUTTON_TEXT:
            self.send_main_menu(chat_id, "Меню настроек и запуска парсинга:")
            return
        if text == STOP_BUTTON_TEXT:
            stop_event = self.stop_events.get(chat_id)
            if chat_id not in self.active_jobs or stop_event is None:
                self.send_message(chat_id, "Активного парсинга нет.")
                self.send_start_keyboard(chat_id)
                return
            if stop_event.is_set():
                return
            stop_event.set()
            notifier = self.active_notifiers.get(chat_id)
            if notifier:
                notifier.cancel_pending()
            self.send_message(chat_id, "Останавливаю текущий парсинг. Уже проверенные карточки сохранены.")
            return

        if text.startswith("/start"):
            self.send_main_menu(
                chat_id,
                "Привет! Выбери категории (можно несколько), город и лимит, затем нажми «▶️ Запустить поиск».\n/help — справка.",
            )
            self.send_start_keyboard(chat_id)
            return
        if text.startswith("/help"):
            self.send_message(chat_id, HELP_TEXT)
            self.send_main_menu(chat_id, "Меню:")
            self.send_start_keyboard(chat_id)
            return
        if text.startswith("/types") or text.startswith("/menu"):
            self.send_main_menu(chat_id, "Меню настроек и запуска поиска:")
            self.send_start_keyboard(chat_id)
            return
        if text.startswith("/manager"):
            manager = strip_command(text, "/manager")
            self.chat_state(chat_id).manager = manager
            self.save_state()
            self.send_main_menu(chat_id, f"Менеджер: {manager or 'не назначен'}")
            return
        if text.startswith("/resume"):
            if not self.schedule_resume_job(chat_id, delay_seconds=1):
                self.send_message(chat_id, "Нет сохранённой прерванной задачи.")
            else:
                self.send_message(chat_id, "Продолжение задачи запланировано.")
            return
        if text.startswith("/contacts"):
            contact_filter = strip_command(text, "/contacts").casefold()
            if contact_filter not in {"phone", "any", "all"}:
                self.send_message(chat_id, "Укажи: /contacts phone, /contacts any или /contacts all")
                return
            self.chat_state(chat_id).contact_filter = contact_filter
            self.save_state()
            self.send_main_menu(chat_id, f"Фильтр контактов: {contact_filter_label(contact_filter)}")
            return
        if text.startswith("/leadstatus"):
            payload = strip_command(text, "/leadstatus")
            parts = payload.split(maxsplit=2)
            state = self.chat_state(chat_id)
            if len(parts) < 2 or not parts[0].isdigit() or not state.last_batch_id:
                self.send_message(
                    chat_id,
                    "Формат: /leadstatus НОМЕР contacted комментарий. Нужна ранее созданная пачка.",
                )
                return
            try:
                updated = update_batch_status(
                    self.output_dir,
                    batch_id=state.last_batch_id,
                    item_no=int(parts[0]),
                    status=parts[1],
                    comment=parts[2] if len(parts) > 2 else "",
                )
            except ValueError as exc:
                self.send_message(chat_id, str(exc))
                return
            self.send_message(
                chat_id,
                f"Статус обновлён: №{updated['item_no']} {updated['name']} → {updated['status']}.",
            )
            return
        if text.startswith("/status"):
            state = self.chat_state(chat_id)
            city = state.city or self.default_city
            limit = state.limit or self.default_limit
            mode = target_mode_label(state.target_mode)
            active = "да" if chat_id in self.active_jobs else "нет"
            cat_line = format_selected_categories_line(self.chat_state(chat_id))
            lines = [
                f"Категории (для кнопки «Запустить»): {cat_line}",
                f"Город: {city or 'не задан'}",
                f"Лимит: {limit}",
                f"Скорость: {get_run_mode(state.run_profile).label}",
                f"Цель: {mode}",
                f"Контакты: {contact_filter_label(state.contact_filter)}",
                f"Менеджер: {state.manager or 'не назначен'}",
                f"Последняя пачка: {state.last_batch_id or 'нет'}",
                f"Chromium: {'окно' if state.show_browser else 'скрыто (headless)'}",
                f"Активная задача: {active}",
            ]
            if state.last_summary:
                lines.append("")
                lines.append("Последняя задача:")
                lines.append(state.last_summary)
            self.send_message(chat_id, "\n".join(lines))
            return
        if text.startswith("/limit"):
            limit_text = strip_command(text, "/limit")
            try:
                limit = parse_limit(limit_text)
            except ValueError as exc:
                self.send_message(chat_id, str(exc))
                return
            self.chat_state(chat_id).limit = limit
            self.save_state()
            self.send_main_menu(chat_id, f"Лимит сохранён: {limit}")
            return
        if text.startswith("/mode"):
            mode = strip_command(text, "/mode").casefold()
            if mode not in {"run", "scrape"}:
                self.send_message(chat_id, "Укажи режим: /mode run или /mode scrape")
                return
            self.chat_state(chat_id).generate = mode == "run"
            self.save_state()
            label = "поиск + генерация" if mode == "run" else "список без сайта"
            self.send_main_menu(chat_id, f"Режим сохранён: {label}")
            return
        if text.startswith("/city"):
            city = strip_command(text, "/city")
            if not city:
                self.send_message(
                    chat_id,
                    "Напиши города после команды, например: /city Волгоград, Сочи, Москва",
                )
                return
            cities = parse_cities(city)
            if not cities:
                self.send_message(chat_id, "Введите корректные названия городов.")
                return
            self.chat_state(chat_id).city = ", ".join(cities)
            self.save_state()
            self.send_main_menu(chat_id, f"Города сохранены: {', '.join(cities)}")
            return
        if text.startswith("/td"):
            url = parse_td_url(text)
            if not url:
                self.send_message(
                    chat_id,
                    "Пришли ссылку на карточку: /td https://yandex.ru/maps/org/название/1234567890/",
                )
                return
            if not is_yandex_maps_org_url(url):
                self.send_message(chat_id, "Нужна ссылка на карточку организации Яндекс.Карт (/org/).")
                return
            if chat_id in self.active_jobs:
                self.send_message(chat_id, "В этом чате уже идет задача. Дождись завершения.")
                return
            thread = threading.Thread(
                target=self.run_td_job_thread,
                args=(chat_id, url),
                daemon=True,
            )
            self.active_jobs.add(chat_id)
            thread.start()
            self.send_message(chat_id, "Пробный черновик: открываю карточку в Chromium и запрашиваю DeepSeek.")
            return
        if text.startswith("/run") or text.startswith("/scrape"):
            self.send_message(
                chat_id,
                "Поиск запускается только кнопкой «▶️ Запустить поиск» в меню ниже. Открой /menu",
            )
            self.send_main_menu(chat_id)
            return

        self.send_message(chat_id, "Не понял команду.\n\n" + HELP_TEXT)
        self.send_main_menu(chat_id)

    def handle_callback_query(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id", ""))
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        data = str(callback.get("data", "")).strip()
        message_id = int(message.get("message_id") or 0)
        logger.info(
            "handle_callback_query: callback_id=%s chat_id=%s data=%r inline_message_id=%s",
            callback_id,
            chat_id,
            data,
            callback.get("inline_message_id"),
        )

        if not chat_id:
            logger.warning("callback без chat_id (нет message.chat?) — отвечаю пустым answerCallbackQuery")
            if callback_id:
                self.answer_callback_query(callback_id)
            return
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            logger.warning(
                "callback от chat_id=%s не в allowed_chat_ids=%s",
                chat_id,
                sorted(self.allowed_chat_ids),
            )
            if callback_id:
                self.answer_callback_query(callback_id, "Этот чат не разрешён для бота.")
            self.send_message(chat_id, f"Этот чат не разрешен. Chat ID: {chat_id}")
            return

        if data.startswith("mark_sent:"):
            logger.info("callback mark_sent для chat_id=%s", chat_id)
            self.handle_mark_sent_callback(chat_id, callback, data)
            return

        if callback_id:
            logger.info("answerCallbackQuery (пусто) callback_id=%s data=%r", callback_id, data)
            self.answer_callback_query(callback_id)

        state = self.chat_state(chat_id)

        if data == "run:go":
            self.handle_run_search_click(chat_id, message_id)
            return
        if data == "progress:download":
            with self.progress_lock:
                items = list(self.progress_items.get(chat_id, []))
                cities = self.progress_cities.get(chat_id, state.city or self.default_city)
            if not items:
                self.send_message(chat_id, "Пока нет найденных организаций без сайта.")
                return
            exports = create_manager_target_txts(
                self.output_dir,
                items,
                city=cities,
                manager=state.manager,
                target_mode=state.target_mode,
                label="progress",
            )
            for mode, count, path in exports:
                self.send_document(
                    chat_id,
                    path,
                    f"Прогресс: {target_mode_file_label(mode)}, {count} организаций",
                )
            return
        if data == "profile:menu":
            self.edit_message_text(
                chat_id,
                message_id,
                "Выберите профиль парсинга.",
                reply_markup=run_profile_keyboard(state),
            )
            return
        if data in {"profile:fast", "profile:long"}:
            state.run_profile = data.removeprefix("profile:")
            state.awaiting_city = False
            self.save_state()
            self.edit_main_menu_message(chat_id, message_id)
            return
        if data in {"target:no_site", "target:redesign", "target:combined"}:
            state.target_mode = data.removeprefix("target:")
            self.save_state()
            self.try_refresh_main_menu(chat_id, message_id)
            return
        if data == "cat:menu":
            self.edit_message_text(
                chat_id,
                message_id,
                "Выберите категории. Можно отметить несколько.",
                reply_markup=category_keyboard(state=state, default_limit=self.default_limit),
            )
            return
        if data == "cat:done":
            self.edit_main_menu_message(chat_id, message_id)
            return
        if data == "city:edit":
            state.awaiting_city = True
            state.awaiting_limit = False
            state.awaiting_priority_urls = False
            self.save_state()
            self.send_message(
                chat_id,
                "Введите один или несколько городов через запятую, например: Москва, Волгоград, Калининград.",
            )
            return
        if data == "lim:custom":
            state.awaiting_limit = True
            state.awaiting_city = False
            state.awaiting_priority_urls = False
            self.save_state()
            self.send_message(chat_id, "Введите любое положительное число.")
            return
        if data == "links:add":
            state.awaiting_priority_urls = True
            self.save_state()
            self.send_message(
                chat_id,
                "Отправь одним сообщением список ссылок на организации Яндекс.Карт. Можно по одной ссылке в строке.",
                reply_markup={"inline_keyboard": [[{"text": "Отмена", "callback_data": "links:cancel"}]]},
            )
            return
        if data == "links:cancel":
            state.awaiting_priority_urls = False
            self.save_state()
            self.send_main_menu(chat_id, "Вставка ссылок отменена.")
            return
        if data == "links:clear":
            state.priority_urls = []
            state.awaiting_priority_urls = False
            self.save_state()
            self.try_refresh_main_menu(chat_id, message_id)
            return

        if data.startswith("city:p:"):
            slug = data.removeprefix("city:p:")
            city_name = CITY_SLUG_TO_CANONICAL.get(slug)
            if city_name:
                state.city = city_name
                self.save_state()
            else:
                self.send_message(chat_id, "Неизвестный пресет города.")
            self.try_refresh_main_menu(chat_id, message_id)
            return

        if data == "city:o":
            self.send_message(chat_id, "Отправь город текстом: /city Название")
            self.try_refresh_main_menu(chat_id, message_id)
            return

        if data.startswith("lim:"):
            try:
                lim = int(data.removeprefix("lim:"))
                if lim < 1:
                    raise ValueError
                state.limit = lim
                self.save_state()
            except ValueError:
                self.send_message(chat_id, "Лимит должен быть положительным числом.")
            self.try_refresh_main_menu(chat_id, message_id)
            return

        if data == "mode:run":
            state.generate = True
            self.save_state()
            self.try_refresh_main_menu(chat_id, message_id)
            return
        if data == "mode:scrape":
            state.generate = False
            self.save_state()
            self.try_refresh_main_menu(chat_id, message_id)
            return
        if data.startswith("contact:"):
            contact_filter = data.removeprefix("contact:")
            if contact_filter in {"phone", "any", "all"}:
                state.contact_filter = contact_filter
                self.save_state()
            self.try_refresh_main_menu(chat_id, message_id)
            return

        if data == "browser:headful":
            state.show_browser = True
            self.save_state()
            self.try_refresh_main_menu(chat_id, message_id)
            return
        if data == "browser:headless":
            state.show_browser = False
            self.save_state()
            self.try_refresh_main_menu(chat_id, message_id)
            return

        if data == "cat:clear":
            state.selected_category_indices = []
            self.save_state()
            self.edit_message_text(
                chat_id,
                message_id,
                "Выберите категории. Можно отметить несколько.",
                reply_markup=category_keyboard(state=state, default_limit=self.default_limit),
            )
            return

        cat_index = category_index_from_callback(data)
        if cat_index is not None:
            cur = list(state.selected_category_indices)
            if cat_index in cur:
                cur.remove(cat_index)
            else:
                cur.append(cat_index)
            state.selected_category_indices = sorted(set(cur))
            self.save_state()
            self.edit_message_text(
                chat_id,
                message_id,
                "Выберите категории. Можно отметить несколько.",
                reply_markup=category_keyboard(state=state, default_limit=self.default_limit),
            )
            return

        logger.warning("Неизвестный callback_data=%r", data)
        self.send_message(chat_id, "Не понял кнопку. Открой /menu.")
        self.try_refresh_main_menu(chat_id, message_id)

    def handle_mark_sent_callback(self, chat_id: int, callback: dict[str, Any], data: str) -> None:
        action_id = data.removeprefix("mark_sent:")
        action = self.contact_actions.get(action_id)
        if not action:
            self.answer_callback_query(str(callback.get("id", "")), "Черновик не найден")
            self.send_message(chat_id, "Черновик не найден. Возможно, бот был перезапущен.")
            return

        action.sent = True
        self.answer_callback_query(str(callback.get("id", "")), "отправлено")
        message = callback.get("message") or {}
        message_id = int(message.get("message_id", 0) or 0)
        text = f"{action.card_text}\n\nСтатус: отправлено"
        if message_id:
            self.edit_message_text(chat_id, message_id, text)
        else:
            self.send_message(chat_id, f"отправлено: {action.lead_name}")

    def start_job(self, chat_id: int, payload: str, generate: bool, search_url: str = "") -> None:
        if chat_id in self.active_jobs:
            self.send_message(chat_id, "В этом чате уже идет задача. Дождись завершения и запусти следующую.")
            return

        try:
            state = self.chat_state(chat_id)
            query, city, limit = parse_job_payload(
                payload,
                state.city or self.default_city,
                state.limit or self.default_limit,
            )
        except ValueError as exc:
            self.send_message(chat_id, str(exc))
            return

        thread = threading.Thread(
            target=self.run_job_thread,
            args=(chat_id, query, city, limit, generate, search_url),
            daemon=True,
        )
        self.active_jobs.add(chat_id)
        self.stop_events[chat_id] = threading.Event()
        thread.start()
        self.send_start_keyboard(chat_id, running=True)

    def start_categories_job(
        self,
        chat_id: int,
        category_indices: list[int],
        generate: bool,
    ) -> None:
        if chat_id in self.active_jobs:
            self.send_message(chat_id, "В этом чате уже идет задача. Дождись завершения и запусти следующую.")
            return
        try:
            state = self.chat_state(chat_id)
            city = state.city or self.default_city
            limit = state.limit or self.default_limit
            if not city:
                raise ValueError("Город не задан.")
            if limit < 1:
                raise ValueError("Лимит должен быть положительным числом.")
        except ValueError as exc:
            self.send_message(chat_id, str(exc))
            return

        thread = threading.Thread(
            target=self.run_categories_job_thread,
            args=(chat_id, category_indices, city, limit, generate, 0, state.run_profile),
            daemon=True,
        )
        self.active_jobs.add(chat_id)
        self.stop_events[chat_id] = threading.Event()
        thread.start()
        self.send_start_keyboard(chat_id, running=True)

    def schedule_saved_resumes(self) -> None:
        for chat_id_raw, state in self.state.items():
            if state.resume_job:
                try:
                    self.schedule_resume_job(int(chat_id_raw), delay_seconds=10)
                except ValueError:
                    continue

    def schedule_resume_job(self, chat_id: int, *, delay_seconds: float | None = None) -> bool:
        state = self.chat_state(chat_id)
        payload = state.resume_job
        if not payload:
            return False
        profile = get_run_mode(str(payload.get("run_profile", state.run_profile)))
        delay = profile.captcha_retry_seconds if delay_seconds is None else delay_seconds

        def resume() -> None:
            if chat_id in self.active_jobs:
                timer = threading.Timer(30.0, resume)
                timer.daemon = True
                timer.start()
                return
            current = self.chat_state(chat_id).resume_job
            if not current:
                return
            indices = [
                int(index)
                for index in current.get("category_indices", [])
                if str(index).isdigit() and 0 <= int(index) < len(CATEGORY_CHOICES)
            ]
            if not indices:
                self.chat_state(chat_id).resume_job = {}
                self.save_state()
                return
            self.active_jobs.add(chat_id)
            self.run_categories_job_thread(
                chat_id,
                indices,
                str(current.get("city", "")),
                int(current.get("remaining", 0) or 0),
                bool(current.get("generate", False)),
                int(current.get("attempt", 0) or 0),
                str(current.get("run_profile", "fast")),
            )

        timer = threading.Timer(max(0.1, delay), resume)
        timer.daemon = True
        timer.start()
        return True

    def run_job_thread(
        self,
        chat_id: int,
        query: str,
        city: str,
        limit: int,
        generate: bool,
        search_url: str = "",
    ) -> None:
        notifier = JobNotifyQueue(self, chat_id)
        self.active_notifiers[chat_id] = notifier
        stop_event = self.stop_events.setdefault(chat_id, threading.Event())

        def log(message: str) -> None:
            notifier.enqueue(message)

        try:
            target_mode = self.chat_state(chat_id).target_mode
            mode = target_mode_label(target_mode)
            notifier.enqueue(
                "\n".join(
                    [
                        "Задача запущена.",
                        f"Режим: {mode}",
                        f"Запрос: {query}",
                        f"Город: {city}",
                        f"Лимит: {limit}",
                        f"Ссылка поиска: {search_url}" if search_url else "",
                    ]
                ),
            )
            result = run_lead_job(
                query=query,
                location=city,
                limit=limit,
                output_dir=self.output_dir,
                generate=generate,
                headless=self.effective_headless(chat_id),
                delay_seconds=self.delay_seconds,
                search_url=search_url,
                log=log,
                on_message_generated=notifier.enqueue_generated_card,
                prefer_no_site_stop=True,
                contact_filter=self.chat_state(chat_id).contact_filter,
                light_parse=False,
                should_stop=stop_event.is_set,
            )
            if result.messages:
                notifier.enqueue(
                    f"Генерация завершена: {len(result.messages)} черновиков (каждый уже отправлен отдельным сообщением).",
                )
            selected_items = select_manager_leads(
                [(lead, query) for lead in result.no_site_leads],
                max_items=limit,
            )
            state = self.chat_state(chat_id)
            manager_batches = format_manager_lead_batches(
                selected_items,
                city=city,
                manager=state.manager,
            )
            for manager_batch in manager_batches:
                notifier.enqueue(manager_batch)
            if selected_items:
                export = create_manager_batch(
                    self.output_dir,
                    selected_items,
                    city=city,
                    manager=state.manager,
                )
                state.last_batch_id = export.batch_id
                notifier.enqueue(
                    f"Пачка: {export.batch_id}\nCSV: {export.csv_path}\n"
                    f"XLSX: {export.xlsx_path or 'не создан'}"
                )
                notifier.enqueue_document(export.csv_path, f"Пачка {export.batch_id}, CSV")
                if export.xlsx_path:
                    notifier.enqueue_document(export.xlsx_path, f"Пачка {export.batch_id}, Excel")

            message = [
                "Задача завершена.",
                f"Запрос: {result.query}",
                f"Город: {result.location or 'не указан'}",
                f"Всего найдено: {len(result.leads)}",
                f"Без сайта: {len(result.no_site_leads)}",
                f"CSV: {result.leads_path}",
                f"Реестр обработанных: {result.processed_path}",
            ]
            if result.messages_path:
                message.append(f"Черновики: {result.messages_path}")
            summary = "\n".join(message)
            self.chat_state(chat_id).last_summary = summary
            self.save_state()
            notifier.enqueue(summary)
            notifier.close()
            try:
                self.send_main_menu(chat_id, summary + "\n\nМожно снова настроить и нажать «▶️ Запустить поиск».")
            except Exception as exc:
                logger.warning("Финальное меню не отправлено: %s", exc)
        except Exception as exc:
            logger.exception("Фоновая задача: ошибка")
            notifier.enqueue(f"Задача упала с ошибкой: {exc}")
            notifier.close()
        finally:
            self.active_jobs.discard(chat_id)
            self.stop_events.pop(chat_id, None)
            self.active_notifiers.pop(chat_id, None)
            try:
                self.send_start_keyboard(chat_id)
            except Exception as exc:
                logger.warning("Не удалось вернуть клавиатуру «Старт»: %s", exc)

    def run_td_job_thread(self, chat_id: int, yandex_url: str) -> None:
        notifier = JobNotifyQueue(self, chat_id)

        def log(message: str) -> None:
            notifier.enqueue(message)

        try:
            notifier.enqueue(f"Пробный черновик по ссылке:\n{yandex_url}")
            message_item = run_td_draft_job(
                yandex_url,
                headless=self.effective_headless(chat_id),
                log=log,
            )
            notifier.enqueue_generated_card(message_item)
            notifier.enqueue("Промпт для DeepSeek настраивается в maps_parser/settings.py (OUTREACH_*).")
        except Exception as exc:
            logger.exception("Пробный черновик /td: ошибка")
            notifier.enqueue(f"Пробный черновик не получился: {exc}")
        finally:
            notifier.close()
            self.active_jobs.discard(chat_id)

    def run_categories_job_thread(
        self,
        chat_id: int,
        category_indices: list[int],
        city: str,
        limit: int,
        generate: bool,
        retry_attempt: int = 0,
        run_profile: str = "fast",
    ) -> None:
        notifier = JobNotifyQueue(self, chat_id)
        self.active_notifiers[chat_id] = notifier
        captcha_detected = False
        stop_event = self.stop_events.setdefault(chat_id, threading.Event())

        def log(message: str) -> None:
            nonlocal captcha_detected
            lower = message.casefold()
            if "капч" in lower or "служебн" in lower and "яндекс" in lower:
                captcha_detected = True
            if profile.key == "long" and (
                message.startswith("DNS-")
                or message.startswith("Реестр обработанных:")
                or message.startswith("Старт поиска:")
                or message.startswith("Повторно найдено")
                or message.startswith("Организаций без сайта нет")
                or "Уже обработано, пропускаю:" in message
            ):
                return
            notifier.enqueue(message)

        profile = get_run_mode(run_profile)
        target_mode = self.chat_state(chat_id).target_mode
        categories = [CATEGORY_CHOICES[i] for i in category_indices if 0 <= i < len(CATEGORY_CHOICES)]
        cities = parse_cities(city)
        tasks = [(task_city, category) for task_city in cities for category in categories]
        total = len(tasks)
        with self.progress_lock:
            self.progress_items[chat_id] = []
            self.progress_cities[chat_id] = ", ".join(cities)
        try:
            mode = target_mode_label(target_mode)
            labels = ", ".join(category.label for category in categories)
            notifier.enqueue(
                "\n".join(
                    [
                        f"Задача запущена: {total} поисков.",
                        f"Цель: {mode}",
                        f"Скорость: {profile.label}",
                        f"Категории: {labels}",
                        f"Города: {', '.join(cities)}",
                        f"Общая цель: {limit} подходящих организаций",
                    ]
                ),
            )
            priority_urls = list(self.chat_state(chat_id).priority_urls)
            specs = [
                LeadJobSpec(
                    query=category.query,
                    location=task_city,
                    limit=limit,
                    search_url=build_yandex_category_url(category, task_city),
                    priority_urls=priority_urls if step == 0 else None,
                    group_key="__all__",
                )
                for step, (task_city, category) in enumerate(tasks)
            ]
            if priority_urls:
                notifier.enqueue(f"Приоритетных ссылок перед поиском: {len(priority_urls)}.")
            if profile.key == "long":
                notifier.enqueue(
                    f"Подготовлено поисковых выдач: {total}. "
                    "Сбор идёт группами по 4 выдачи, максимум по 100 новых ссылок с каждой."
                )
            else:
                notifier.enqueue(f"Подготовлено поисковых выдач: {total}.")

            contact_filter = self.chat_state(chat_id).contact_filter
            streamed_items: list[tuple[Lead, str]] = []
            collected_stream_keys: set[str] = set()

            def on_lead_checked(lead: Lead) -> None:
                if (
                    not lead_matches_manager_target(lead, target_mode)
                    or not lead.name.strip()
                    or not lead.matches_contact_filter(contact_filter)
                ):
                    return
                key = lead_identity_key(lead) or lead.yandex_url
                if not key or key in collected_stream_keys:
                    return
                collected_stream_keys.add(key)
                category = ", ".join(lead.categories[:2]) or "не указана"
                streamed_items.append((lead, category))
                with self.progress_lock:
                    self.progress_items[chat_id] = list(streamed_items)
                if len(streamed_items) % 10 == 0:
                    group = streamed_items[-10:]
                    messages = format_manager_lead_batches(
                        group,
                        city=", ".join(cities),
                        manager=self.chat_state(chat_id).manager,
                        target_mode=target_mode,
                    )
                    for message_index, message in enumerate(messages, start=1):
                        notifier.enqueue(message)
                        txt_path = create_manager_txt(
                            self.output_dir,
                            message,
                            label=f"batch_{len(streamed_items) // 10}_{message_index}",
                        )
                        notifier.enqueue_document(txt_path, "Пачка организаций без сайта, TXT")

            parser_runtime = profile.parser_settings()
            parser_runtime["TARGET_MODE"] = target_mode
            parser_runtime["WEBSITE_PLATFORM_AUDIT"] = 0 if target_mode == "no_site" else 1
            network_failures = 0
            while True:
                if stop_event.is_set():
                    batch_results = []
                    break
                try:
                    batch_results = run_lead_jobs_batch(
                        specs,
                        output_dir=self.output_dir,
                        generate=generate,
                        headless=self.effective_headless(chat_id),
                        delay_seconds=profile.page_delay,
                        log=log,
                        on_message_generated=notifier.enqueue_generated_card,
                        prefer_no_site_stop=True,
                        contact_filter=contact_filter,
                        overall_no_site_limit=limit,
                        light_parse=False,
                        on_lead_checked=on_lead_checked,
                        runtime_settings=parser_runtime,
                        should_stop=stop_event.is_set,
                    )
                    break
                except Exception as exc:
                    if not is_network_error(exc):
                        raise
                    network_failures += 1
                    retry_delay = network_retry_delay(network_failures)
                    notifier.enqueue(
                        f"Сбой сети: {exc}. Повтор подключения через {int(retry_delay)} сек."
                    )
                    if stop_event.wait(retry_delay):
                        batch_results = []
                        break

            if stop_event.is_set():
                notifier.enqueue("Парсинг остановлен пользователем. Уже проверенные карточки сохранены.")
                notifier.close()
                return

            total_leads = 0
            total_no_site = 0
            total_messages = 0
            last_processed: Path | None = None
            manager_items: list[tuple[Lead, str]] = []

            for step, ((task_city, category), result) in enumerate(zip(tasks, batch_results, strict=True), start=1):
                total_leads += len(result.leads)
                total_no_site += len(result.no_site_leads)
                last_processed = result.processed_path
                manager_items.extend((lead, category.label) for lead in result.no_site_leads)

                if result.messages:
                    total_messages += len(result.messages)
                    notifier.enqueue(
                        f"[{step}/{total}] Сгенерировано черновиков: {len(result.messages)} (отправлены по одному сразу после DeepSeek).",
                    )

            target_total = limit
            selected_items = select_manager_leads(
                manager_items,
                max_items=target_total,
                target_mode=target_mode,
            )
            with self.progress_lock:
                self.progress_items[chat_id] = list(selected_items)
            state = self.chat_state(chat_id)
            manager_batches = format_manager_lead_batches(
                selected_items,
                city=", ".join(cities),
                manager=state.manager,
                target_mode=target_mode,
            )
            if manager_batches:
                notifier.enqueue("Итоговый список после завершения парсинга:")
                if target_mode == "combined":
                    for mode, count, txt_path in create_manager_target_txts(
                        self.output_dir,
                        selected_items,
                        city=", ".join(cities),
                        manager=state.manager,
                        target_mode=target_mode,
                        label="final",
                    ):
                        notifier.enqueue_document(
                            txt_path,
                            f"Итог: {target_mode_file_label(mode)}, {count} организаций",
                        )
                elif len(selected_items) > 50:
                    all_text = format_manager_leads_single_text(
                        selected_items,
                        city=", ".join(cities),
                        manager=state.manager,
                        target_mode=target_mode,
                    )
                    txt_path = create_manager_txt(self.output_dir, all_text, label="final_all")
                    notifier.enqueue_document(
                        txt_path,
                        f"Итоговый список: {len(selected_items)} организаций",
                    )
                else:
                    for batch_index, manager_batch in enumerate(manager_batches, start=1):
                        notifier.enqueue(manager_batch)
                        txt_path = create_manager_txt(
                            self.output_dir,
                            manager_batch,
                            label=f"final_{batch_index}",
                        )
                        notifier.enqueue_document(txt_path, f"Итоговая пачка {batch_index}, TXT")
                export = create_manager_batch(
                    self.output_dir,
                    selected_items,
                    city=", ".join(cities),
                    manager=state.manager,
                )
                state.last_batch_id = export.batch_id
                if target_mode != "combined":
                    notifier.enqueue(
                        f"Пачка: {export.batch_id}\nCSV: {export.csv_path}\n"
                        f"XLSX: {export.xlsx_path or 'не создан'}"
                    )
                    notifier.enqueue_document(export.csv_path, f"Пачка {export.batch_id}, CSV")
                    if export.xlsx_path:
                        notifier.enqueue_document(export.xlsx_path, f"Пачка {export.batch_id}, Excel")
            elif not streamed_items:
                notifier.enqueue("Организации без сайта для передачи менеджерам не найдены.")

            state = self.chat_state(chat_id)
            remaining = max(0, target_total - len(selected_items))
            if (
                profile.key != "long"
                and
                captcha_detected
                and remaining > 0
                and retry_attempt < profile.captcha_retry_max
            ):
                state.resume_job = {
                    "category_indices": category_indices,
                    "city": city,
                    "remaining": remaining,
                    "generate": generate,
                    "attempt": retry_attempt + 1,
                    "run_profile": profile.key,
                }
                notifier.enqueue(
                    f"Обнаружена капча. Прогресс сохранён. Автопродолжение через "
                    f"{profile.captcha_retry_seconds // 60} мин.; осталось найти: {remaining}."
                )
                self.schedule_resume_job(chat_id)
            else:
                state.resume_job = {}

            message = [
                "Задача завершена.",
                f"Проверено организаций: {total_leads}",
                f"Найдено подходящих: {len(selected_items)}",
            ]
            if target_mode in {"no_site", "combined"}:
                message.append(f"База без сайта: {self.output_dir / 'no_site_leads.csv'}")
            if target_mode in {"redesign", "combined"}:
                message.append(f"База для переработки: {self.output_dir / 'redesign_leads.csv'}")
            if total_messages:
                message.append(f"Всего черновиков отправлено в чат: {total_messages}")
            summary = "\n".join(message)
            state.last_summary = summary
            state.priority_urls = []
            state.awaiting_priority_urls = False
            self.save_state()
            notifier.enqueue(summary)
            notifier.close()
            try:
                self.send_main_menu(chat_id, summary + "\n\nМожно снова настроить и нажать «▶️ Запустить поиск».")
            except Exception as exc:
                logger.warning("Финальное меню не отправлено: %s", exc)
        except Exception as exc:
            logger.exception("Фоновая задача (несколько категорий): ошибка")
            notifier.enqueue(f"Задача упала с ошибкой: {exc}")
            notifier.close()
        finally:
            self.active_jobs.discard(chat_id)
            self.stop_events.pop(chat_id, None)
            self.active_notifiers.pop(chat_id, None)
            try:
                self.send_start_keyboard(chat_id)
            except Exception as exc:
                logger.warning("Не удалось вернуть клавиатуру «Старт»: %s", exc)

    def send_main_menu(self, chat_id: int, prefix: str = "") -> None:
        state = self.chat_state(chat_id)
        body = format_menu_body(chat_id, state, self.default_city, self.default_limit)
        text = "\n\n".join(part for part in (prefix.strip(), body) if part)
        self.send_message(chat_id, text, reply_markup=main_menu_keyboard(state, self.default_limit))

    def send_start_keyboard(self, chat_id: int, *, running: bool = False) -> None:
        self.send_message(
            chat_id,
            "Управление:",
            reply_markup=start_reply_keyboard(running=running),
        )

    def edit_main_menu_message(self, chat_id: int, message_id: int) -> None:
        state = self.chat_state(chat_id)
        body = format_menu_body(chat_id, state, self.default_city, self.default_limit)
        self.edit_message_text(
            chat_id,
            message_id,
            body,
            reply_markup=main_menu_keyboard(state, self.default_limit),
        )

    def try_refresh_main_menu(self, chat_id: int, message_id: int) -> None:
        if not message_id:
            self.send_main_menu(chat_id)
            return
        try:
            self.edit_main_menu_message(chat_id, message_id)
        except RuntimeError as exc:
            logger.warning("Не удалось обновить меню (message_id=%s): %s", message_id, exc)
            self.send_main_menu(chat_id)

    def handle_run_search_click(self, chat_id: int, message_id: int) -> None:
        state = self.chat_state(chat_id)
        indices = sorted(set(state.selected_category_indices))
        if not indices:
            self.send_message(chat_id, "Выбери хотя бы одну категорию (повторное нажатие снимает выбор).")
            self.try_refresh_main_menu(chat_id, message_id)
            return
        city = state.city or self.default_city
        if not city:
            self.send_message(chat_id, "Выбери город кнопкой или отправь: /city Название")
            self.try_refresh_main_menu(chat_id, message_id)
            return
        if chat_id in self.active_jobs:
            self.send_message(chat_id, "В этом чате уже идёт задача. Дождись завершения.")
            self.try_refresh_main_menu(chat_id, message_id)
            return
        categories = [CATEGORY_CHOICES[i] for i in indices]
        mode = target_mode_label(state.target_mode)
        self.send_message(
            chat_id,
            "\n".join(
                [
                    "Запуск по кнопке.",
                    f"Категорий: {len(categories)} — " + ", ".join(c.label for c in categories),
                    f"Город: {city}",
                    f"Скорость: {get_run_mode(state.run_profile).label}",
                    f"Цель: {mode}",
                    f"Лимит итогового списка: {state.limit or self.default_limit}",
                ]
            ),
        )
        logger.info(
            "Запуск задачи по кнопке: chat_id=%s categories=%r city=%r generate=%s",
            chat_id,
            [c.query for c in categories],
            city,
            state.generate,
        )
        self.start_categories_job(chat_id, indices, generate=state.generate)
        self.try_refresh_main_menu(chat_id, message_id)

    def push_generated_card(self, chat_id: int, message_item: GeneratedMessage) -> None:
        """Сразу после ответа DeepSeek — карточка с текстом и кнопками в Telegram."""
        try:
            self.send_generated_message_card(chat_id, message_item, best_effort=True)
        except Exception as exc:
            logger.warning("Карточка после генерации не отправлена: %s", exc)

    def send_generated_message_card(
        self,
        chat_id: int,
        message_item: GeneratedMessage,
        *,
        best_effort: bool = False,
    ) -> None:
        lead = message_item.lead
        card_text = format_generated_message_card(message_item)
        contacts = lead_contacts(lead)
        action_id = self.register_contact_action(
            ContactAction(
                lead_name=lead.name,
                card_text=card_text,
                message_text=message_item.message,
                contact_text=contacts,
            )
        )
        self.send_message(
            chat_id,
            card_text,
            reply_markup=lead_action_keyboard(action_id, lead),
            best_effort=best_effort,
        )

    def register_contact_action(self, action: ContactAction) -> str:
        with self.contact_action_lock:
            self.contact_action_counter += 1
            action_id = str(self.contact_action_counter)
            self.contact_actions[action_id] = action
            return action_id

    def effective_headless(self, chat_id: int) -> bool:
        """Видимое окно Chromium при show_browser=True; иначе как при запуске бота (--headful или headless)."""
        if self.chat_state(chat_id).show_browser:
            return False
        return self.headless

    def chat_state(self, chat_id: int) -> ChatState:
        key = str(chat_id)
        if key not in self.state:
            self.state[key] = ChatState(show_browser=_default_show_browser_from_env())
        return self.state[key]

    def load_state(self) -> dict[str, ChatState]:
        if not self.state_path.exists():
            return {}
        with self.state_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        def load_selected(raw: Any) -> int | None:
            if raw is None:
                return None
            try:
                index = int(raw)
            except (TypeError, ValueError):
                return None
            if 0 <= index < len(CATEGORY_CHOICES):
                return index
            return None

        def load_category_indices(item: dict[str, Any]) -> list[int]:
            raw_list = item.get("selected_category_indices")
            if isinstance(raw_list, list):
                out: list[int] = []
                for x in raw_list:
                    try:
                        index = int(x)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= index < len(CATEGORY_CHOICES):
                        out.append(index)
                return sorted(set(out))
            single = load_selected(item.get("selected_category_index"))
            return [single] if single is not None else []

        def load_run_profile(item: dict[str, Any]) -> str:
            value = str(item.get("run_profile", "fast"))
            return value if value in {"fast", "long"} else "fast"

        def load_target_mode(item: dict[str, Any]) -> str:
            value = str(item.get("target_mode", "")).strip()
            if value in {"no_site", "redesign", "combined"}:
                return value
            if str(item.get("run_profile", "")) == "redesign":
                legacy = str(item.get("redesign_scope", "combined"))
                return legacy if legacy in {"redesign", "combined"} else "combined"
            return "no_site"

        return {
            chat_id: ChatState(
                city=str(item.get("city", "")),
                limit=clamp_saved_limit(item.get("limit", 0)),
                generate=bool(item.get("generate", False)),
                last_summary=str(item.get("last_summary", "")),
                selected_category_indices=load_category_indices(item),
                show_browser=(bool(item["show_browser"]) if "show_browser" in item else True),
                contact_filter=(
                    str(item.get("contact_filter", "phone"))
                    if str(item.get("contact_filter", "phone")) in {"phone", "any", "all"}
                    else "phone"
                ),
                manager=str(item.get("manager", "")),
                last_batch_id=str(item.get("last_batch_id", "")),
                resume_job=item.get("resume_job") if isinstance(item.get("resume_job"), dict) else {},
                priority_urls=[
                    str(url)
                    for url in item.get("priority_urls", [])
                    if isinstance(url, str) and is_yandex_maps_org_url(url)
                ],
                awaiting_priority_urls=bool(item.get("awaiting_priority_urls", False)),
                awaiting_city=bool(item.get("awaiting_city", False)),
                awaiting_limit=bool(item.get("awaiting_limit", False)),
                run_profile=load_run_profile(item),
                target_mode=load_target_mode(item),
            )
            for chat_id, item in payload.items()
        }

    def save_state(self) -> None:
        payload = {
            chat_id: {
                "city": state.city,
                "limit": state.limit,
                "generate": state.generate,
                "last_summary": state.last_summary,
                "selected_category_indices": state.selected_category_indices,
                "show_browser": state.show_browser,
                "contact_filter": state.contact_filter,
                "manager": state.manager,
                "last_batch_id": state.last_batch_id,
                "resume_job": state.resume_job,
                "priority_urls": state.priority_urls,
                "awaiting_priority_urls": state.awaiting_priority_urls,
                "awaiting_city": state.awaiting_city,
                "awaiting_limit": state.awaiting_limit,
                "run_profile": state.run_profile,
                "target_mode": state.target_mode,
            }
            for chat_id, state in self.state.items()
        }
        with self.state_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def safe_notify(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        """Отправка без падения фоновой задачи при сбое сети Telegram."""
        try:
            self.send_message(chat_id, text, reply_markup=reply_markup, best_effort=True)
        except Exception as exc:
            logger.warning("Сообщение в Telegram не доставлено (задача продолжается): %s", exc)

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        *,
        best_effort: bool = False,
    ) -> None:
        text = compact_blank_lines(text)
        chunks = split_telegram_text(text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if reply_markup and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            self.api_request("sendMessage", payload, best_effort=best_effort)

    def send_document(self, chat_id: int, path: Path, caption: str = "") -> None:
        if not path.exists():
            return
        boundary = f"----maps-parser-{int(time.time() * 1000)}"
        file_bytes = path.read_bytes()
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode(),
        ]
        if caption:
            parts.append(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n"
                    f"{caption}\r\n"
                ).encode("utf-8")
            )
        parts.extend(
            [
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
                    f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                ).encode("utf-8"),
                file_bytes,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        body = b"".join(parts)
        url = f"{self.api_base}/bot{self.token}/sendDocument"
        api_hostname = _telegram_api_hostname(self.api_base)
        connect_ips = _telegram_api_connect_ips(api_hostname)
        attempt = 0
        while True:
            attempt += 1
            try:
                raw = _telegram_api_post_bytes(
                    url,
                    body,
                    timeout=30.0,
                    api_hostname=api_hostname,
                    connect_ip=connect_ips[(attempt - 1) % len(connect_ips)] if connect_ips else None,
                    content_type=f"multipart/form-data; boundary={boundary}",
                )
                parsed = json.loads(raw.decode("utf-8"))
                if not parsed.get("ok"):
                    raise RuntimeError(str(parsed.get("description") or parsed))
                return
            except Exception as exc:
                if not is_network_error(exc):
                    logger.warning("Не удалось отправить документ %s: %s", path, exc)
                    return
                delay = network_retry_delay(attempt)
                logger.warning(
                    "Сбой сети при отправке %s: %s — повтор через %s сек.",
                    path,
                    exc,
                    int(delay),
                )
                time.sleep(delay)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        if not callback_query_id:
            return
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self.api_request("answerCallbackQuery", payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": compact_blank_lines(text),
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self.api_request("editMessageText", payload)

    def api_request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        best_effort: bool = False,
    ) -> dict[str, Any]:
        payload = payload or {}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.api_base}/bot{self.token}/{method}"
        transient_http = {429, 500, 502, 503, 504}
        if method == "getUpdates":
            read_timeout = 48.0
        elif method == "sendMessage":
            read_timeout = (
                _TELEGRAM_BEST_EFFORT_SEND_TIMEOUT_SEC if best_effort else 18.0
            )
        else:
            read_timeout = 25.0

        api_hostname = _telegram_api_hostname(self.api_base)
        connect_ips = _telegram_api_connect_ips(api_hostname)
        if connect_ips:
            logger.debug(
                "Telegram API: %s через %s",
                api_hostname,
                connect_ips[0] if len(connect_ips) == 1 else f"{len(connect_ips)} IP",
            )

        attempt = 0
        while True:
            attempt += 1
            if method == "getUpdates":
                logger.debug(
                    "API getUpdates offset=%s timeout=%s попытка=%s",
                    payload.get("offset"),
                    payload.get("timeout"),
                    attempt,
                )
            elif attempt == 1:
                if not best_effort or method != "sendMessage":
                    logger.info("API %s %s", method, _telegram_api_payload_preview(payload))
            elif best_effort:
                logger.debug("Повтор API %s (попытка %s) после сбоя", method, attempt)
            else:
                logger.warning("Повтор API %s (попытка %s) после сбоя", method, attempt)

            connect_ip = None
            if connect_ips:
                connect_ip = connect_ips[(attempt - 1) % len(connect_ips)]
            try:
                raw = _telegram_api_post_bytes(
                    url,
                    body,
                    timeout=read_timeout,
                    api_hostname=api_hostname,
                    connect_ip=connect_ip,
                )
                parsed: dict[str, Any] = json.loads(raw.decode("utf-8"))
            except HTTPError as exc:
                body_err = exc.read().decode("utf-8", errors="replace")
                code = int(exc.code or 0)
                low = body_err.lower()
                if method == "editMessageText" and code == 400 and "message is not modified" in low:
                    logger.debug("editMessageText: содержимое не менялось — ок")
                    return {"ok": True, "result": None}

                if method == "answerCallbackQuery" and code == 400 and (
                    "query is too old" in low or "query id is invalid" in low
                ):
                    logger.warning("answerCallbackQuery: callback уже устарел или недействителен — пропускаю.")
                    return {"ok": True, "result": None}

                if method == "getUpdates" and (
                    code == 409 or "getupdates" in low or "terminated" in low or "conflict" in low
                ):
                    logger.error(
                        "Конфликт long polling: где-то ещё getUpdates с этим TELEGRAM_BOT_TOKEN "
                        "(вторая копия бота, Docker, systemd, другой ПК). Оставь один процесс."
                    )
                    raise RuntimeError(f"Telegram API HTTP {code}: {body_err}") from exc

                if code in transient_http:
                    delay = network_retry_delay(attempt)
                    logger.warning(
                        "HTTP %s при %s — повтор через %s сек.",
                        code,
                        method,
                        int(delay),
                    )
                    time.sleep(delay)
                    continue

                logger.error("HTTP %s при %s: %s", code, method, body_err[:800])
                raise RuntimeError(f"Telegram API HTTP {code}: {body_err}") from exc
            except URLError as exc:
                delay = network_retry_delay(attempt)
                logger.warning(
                    "Сбой соединения Telegram (%s): %s — повтор через %s сек.",
                    method,
                    exc,
                    int(delay),
                )
                time.sleep(delay)
                continue
            except TimeoutError as exc:
                delay = network_retry_delay(attempt)
                logger.warning(
                    "Нет ответа от Telegram (%s): %s — повтор через %s сек.",
                    method,
                    exc,
                    int(delay),
                )
                time.sleep(delay)
                continue
            except OSError as exc:
                delay = network_retry_delay(attempt)
                logger.warning(
                    "Сокет Telegram (%s): %s — повтор через %s сек.",
                    method,
                    exc,
                    int(delay),
                )
                time.sleep(delay)
                continue

            if not parsed.get("ok"):
                desc = str(parsed.get("description") or parsed)
                if method == "editMessageText" and "message is not modified" in desc.lower():
                    logger.debug("editMessageText: содержимое не менялось — ок")
                    return parsed
                logger.error("Telegram ok=false при %s: %s", method, desc)
                raise RuntimeError(f"Telegram API error: {desc}")
            return parsed


def _telegram_api_hostname(api_base: str) -> str:
    host = (urlparse(api_base).hostname or "").strip().lower()
    return host or "api.telegram.org"


def _telegram_api_connect_ips(hostname: str) -> list[str]:
    if _telegram_socks5_proxy_from_env():
        return []
    if settings.SKIP_DNS_CHECK:
        return []
    ip = _resolve_a_via_public_dns(hostname)
    if ip:
        return [ip]
    if hostname == "api.telegram.org":
        return list(_TELEGRAM_API_FALLBACK_IPS)
    return []


def _telegram_api_post_bytes(
    url: str,
    body: bytes,
    *,
    timeout: float,
    api_hostname: str,
    connect_ip: str | None,
    content_type: str = "application/json; charset=utf-8",
) -> bytes:
    socks_proxy = _telegram_socks5_proxy_from_env()
    if socks_proxy:
        return _telegram_api_post_bytes_via_socks5(
            url,
            body,
            timeout=timeout,
            api_hostname=api_hostname,
            proxy_url=socks_proxy,
            content_type=content_type,
        )

    if connect_ip:
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        context = ssl.create_default_context()
        with socket.create_connection((connect_ip, 443), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=api_hostname) as ssock:
                conn = http.client.HTTPSConnection(api_hostname)
                conn.sock = ssock
                conn.request(
                    "POST",
                    path,
                    body=body,
                    headers={
                        "Host": api_hostname,
                        "Content-Type": content_type,
                    },
                )
                response = conn.getresponse()
                raw = response.read()
        if response.status >= 400:
            raise HTTPError(
                url,
                response.status,
                response.reason,
                response.headers,
                BytesIO(raw),
            )
        return raw

    request = Request(
        url,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _telegram_socks5_proxy_from_env() -> str:
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        raw = os.environ.get(name, "").strip()
        if raw.lower().startswith(("socks5://", "socks5h://")):
            return raw
    return ""


def _telegram_api_post_bytes_via_socks5(
    url: str,
    body: bytes,
    *,
    timeout: float,
    api_hostname: str,
    proxy_url: str,
    content_type: str = "application/json; charset=utf-8",
) -> bytes:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    context = ssl.create_default_context()
    with _socks5_connect(proxy_url, api_hostname, 443, timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=api_hostname) as ssock:
            conn = http.client.HTTPSConnection(api_hostname)
            conn.sock = ssock
            conn.request(
                "POST",
                path,
                body=body,
                headers={
                    "Host": api_hostname,
                    "Content-Type": content_type,
                },
            )
            response = conn.getresponse()
            raw = response.read()
    if response.status >= 400:
        raise HTTPError(
            url,
            response.status,
            response.reason,
            response.headers,
            BytesIO(raw),
        )
    return raw


def _socks5_connect(proxy_url: str, target_host: str, target_port: int, *, timeout: float) -> socket.socket:
    parsed = urlparse(proxy_url)
    if parsed.scheme.lower() not in {"socks5", "socks5h"}:
        raise ValueError(f"Unsupported proxy scheme for Telegram API: {parsed.scheme}")
    if not parsed.hostname or parsed.port is None:
        raise ValueError(f"Invalid SOCKS5 proxy URL: {proxy_url}")

    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        methods = b"\x00"
        if username or password:
            methods += b"\x02"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        version, method = _recv_exact(sock, 2)
        if version != 5:
            raise OSError("SOCKS5 proxy returned an invalid greeting.")
        if method == 0xFF:
            raise OSError("SOCKS5 proxy rejected authentication methods.")
        if method == 0x02:
            user_b = username.encode("utf-8")
            pass_b = password.encode("utf-8")
            if len(user_b) > 255 or len(pass_b) > 255:
                raise ValueError("SOCKS5 username/password is too long.")
            sock.sendall(b"\x01" + bytes([len(user_b)]) + user_b + bytes([len(pass_b)]) + pass_b)
            auth_version, auth_status = _recv_exact(sock, 2)
            if auth_version != 1 or auth_status != 0:
                raise OSError("SOCKS5 proxy authentication failed.")

        host_b = target_host.encode("idna")
        if len(host_b) > 255:
            raise ValueError(f"Target host is too long for SOCKS5: {target_host}")
        request = b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + struct.pack("!H", target_port)
        sock.sendall(request)

        header = _recv_exact(sock, 4)
        if header[0] != 5:
            raise OSError("SOCKS5 proxy returned an invalid response.")
        if header[1] != 0:
            raise OSError(f"SOCKS5 proxy connect failed with code {header[1]}.")
        atyp = header[3]
        if atyp == 1:
            _recv_exact(sock, 4)
        elif atyp == 3:
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length)
        elif atyp == 4:
            _recv_exact(sock, 16)
        else:
            raise OSError(f"SOCKS5 proxy returned an unknown address type: {atyp}.")
        _recv_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("SOCKS5 proxy closed the connection.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _telegram_api_payload_preview(payload: dict[str, Any], max_len: int = 600) -> str:
    preview: dict[str, Any] = dict(payload)
    if "reply_markup" in preview:
        preview["reply_markup"] = "<inline_keyboard>"
    text_val = preview.get("text")
    if isinstance(text_val, str) and len(text_val) > 160:
        preview["text"] = text_val[:160] + "..."
    raw = json.dumps(preview, ensure_ascii=False)
    return raw if len(raw) <= max_len else raw[:max_len] + "..."


def format_selected_categories_line(state: ChatState) -> str:
    indices = sorted(set(state.selected_category_indices))
    if not indices:
        return "не выбраны"
    labels = [CATEGORY_CHOICES[i].label for i in indices if 0 <= i < len(CATEGORY_CHOICES)]
    return "; ".join(labels) if labels else "не выбраны"


def contact_filter_label(value: str) -> str:
    return {
        "phone": "только с телефоном",
        "any": "с любым контактом",
        "all": "все организации",
    }.get((value or "").casefold(), "только с телефоном")


def target_mode_label(value: str) -> str:
    return {
        "no_site": "только без сайта",
        "redesign": "только переработка сайтов",
        "combined": "без сайта + переработка",
    }.get((value or "").casefold(), "только без сайта")


def strip_command(text: str, command: str) -> str:
    first = text.split(maxsplit=1)
    if not first:
        return ""
    if first[0].split("@", 1)[0] != command:
        return ""
    return first[1].strip() if len(first) > 1 else ""


def parse_td_url(text: str) -> str:
    payload = strip_command(text, "/td")
    match = re.search(r"https?://\S+", payload)
    if match:
        return match.group(0).rstrip(").,>\"'")
    return payload.strip()

def parse_yandex_org_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"https?://\S+", text or ""):
        url = raw.rstrip(").,;]>\"'")
        if not is_yandex_maps_org_url(url) or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def parse_cities(value: str) -> list[str]:
    cities: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        city = re.sub(r"\s+", " ", raw).strip()
        key = city.casefold()
        if len(city) < 2 or key in seen:
            continue
        seen.add(key)
        cities.append(city)
    return cities


def format_menu_body(chat_id: int, state: ChatState, default_city: str, default_limit: int) -> str:
    city = state.city or default_city or ""
    limit_val = state.limit or default_limit
    cat_label = format_selected_categories_line(state)
    mode = target_mode_label(state.target_mode)
    browser_mode = "окно Chromium видимо" if state.show_browser else "скрыто (headless)"
    profile = get_run_mode(state.run_profile)
    return "\n".join(
        [
            f"Chat ID: {chat_id}",
            "",
            "Текущие настройки:",
            f"• Категории: {cat_label}",
            f"• Скорость: {profile.label}",
            f"• Город: {city or 'не выбран — кнопка или /city'}",
            f"• Лимит организаций в списке: {limit_val}",
            f"• Цель: {mode}",
            f"• Контакты: {contact_filter_label(state.contact_filter)}",
            f"• Менеджер: {state.manager or 'не назначен — /manager Имя'}",
            f"• Браузер: {browser_mode}",
            f"• Приоритетные ссылки: {len(state.priority_urls)}",
            "",
            "Настрой кнопками (категории — несколько), затем «▶️ Запустить поиск».",
        ]
    )


def main_menu_keyboard(state: ChatState, default_limit: int) -> dict[str, Any]:
    selected_count = len(set(state.selected_category_indices))
    city = state.city or "не выбран"
    lim_cur = state.limit or default_limit
    rows: list[list[dict[str, Any]]] = [
        [
            {
                "text": f"Скорость: {get_run_mode(state.run_profile).label}",
                "callback_data": "profile:menu",
                "style": "primary",
            }
        ],
        [
            {
                "text": f"Категории: {selected_count}",
                "callback_data": "cat:menu",
                "style": "primary",
            }
        ],
        [
            {
                "text": f"Город: {city}",
                "callback_data": "city:edit",
                "style": "primary",
            }
        ],
        [
            {
                "text": ("✓ " if lim_cur == 10 else "") + "10",
                "callback_data": "lim:10",
                "style": "success" if lim_cur == 10 else "primary",
            },
            {
                "text": ("✓ " if lim_cur == 50 else "") + "50",
                "callback_data": "lim:50",
                "style": "success" if lim_cur == 50 else "primary",
            },
            {
                "text": f"Своё: {lim_cur}" if lim_cur not in {10, 50} else "Своё число",
                "callback_data": "lim:custom",
                "style": "success" if lim_cur not in {10, 50} else "primary",
            },
        ],
    ]
    rows.append(
        [
            {
                "text": ("✓ " if state.target_mode == "no_site" else "") + "Без сайта",
                "callback_data": "target:no_site",
                "style": "success" if state.target_mode == "no_site" else "primary",
            },
            {
                "text": ("✓ " if state.target_mode == "redesign" else "") + "Переработка",
                "callback_data": "target:redesign",
                "style": "success" if state.target_mode == "redesign" else "primary",
            },
            {
                "text": ("✓ " if state.target_mode == "combined" else "") + "Вместе",
                "callback_data": "target:combined",
                "style": "success" if state.target_mode == "combined" else "primary",
            },
        ]
    )

    rows.append(
        [
            {
                "text": ("✓ " if state.generate else "") + "С ИИ-сообщениями",
                "callback_data": "mode:run",
                "style": "success" if state.generate else "primary",
            },
            {
                "text": ("✓ " if not state.generate else "") + "Без ИИ",
                "callback_data": "mode:scrape",
                "style": "success" if not state.generate else "primary",
            },
        ]
    )
    rows.append(
        [
            {
                "text": ("✓ " if state.contact_filter == "phone" else "") + "С телефоном",
                "callback_data": "contact:phone",
                "style": "success" if state.contact_filter == "phone" else "primary",
            },
            {
                "text": ("✓ " if state.contact_filter == "any" else "") + "Любой контакт",
                "callback_data": "contact:any",
                "style": "success" if state.contact_filter == "any" else "primary",
            },
            {
                "text": ("✓ " if state.contact_filter == "all" else "") + "Все",
                "callback_data": "contact:all",
                "style": "success" if state.contact_filter == "all" else "primary",
            },
        ]
    )

    rows.append(
        [
            {
                "text": ("✓ " if state.show_browser else "") + "Окно Chromium",
                "callback_data": "browser:headful",
                "style": "success" if state.show_browser else "primary",
            },
            {
                "text": ("✓ " if not state.show_browser else "") + "Без окна",
                "callback_data": "browser:headless",
                "style": "success" if not state.show_browser else "primary",
            },
        ]
    )

    link_row: list[dict[str, Any]] = [
        {"text": "📎 Вставить ссылки", "callback_data": "links:add", "style": "primary"}
    ]
    if state.priority_urls:
        link_row.append(
            {
                "text": f"✖ Очистить ({len(state.priority_urls)})",
                "callback_data": "links:clear",
                "style": "danger",
            }
        )
    rows.append(link_row)
    rows.append(
        [
            {
                "text": "📥 Скачать прогресс",
                "callback_data": "progress:download",
                "style": "primary",
            }
        ]
    )
    rows.append(
        [{"text": "▶️ Запустить парсинг", "callback_data": "run:go", "style": "success"}]
    )
    return {"inline_keyboard": rows}


def run_profile_keyboard(state: ChatState) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": ("✓ " if state.run_profile == "fast" else "") + "Быстрый",
                    "callback_data": "profile:fast",
                    "style": "success" if state.run_profile == "fast" else "primary",
                },
                {
                    "text": ("✓ " if state.run_profile == "long" else "") + "Долгий",
                    "callback_data": "profile:long",
                    "style": "success" if state.run_profile == "long" else "primary",
                },
            ]
        ]
    }


def start_reply_keyboard(*, running: bool = False) -> dict[str, Any]:
    button = STOP_BUTTON_TEXT if running else START_BUTTON_TEXT
    style = "danger" if running else "success"
    return {
        "keyboard": [[{"text": button, "style": style}]],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def category_keyboard(columns: int = 2, state: ChatState | None = None, default_limit: int = 10) -> dict[str, Any]:
    _ = default_limit
    current = state or ChatState()
    rows: list[list[dict[str, Any]]] = []
    selected = set(current.selected_category_indices)
    for index, category in enumerate(CATEGORY_CHOICES):
        button = {
            "text": ("✓ " if index in selected else "") + category.label,
            "callback_data": f"cat:{index}",
            "style": "success" if index in selected else "primary",
        }
        if not rows or len(rows[-1]) >= columns:
            rows.append([button])
        else:
            rows[-1].append(button)
    rows.append(
        [
            {"text": "✖ Очистить", "callback_data": "cat:clear", "style": "danger"},
            {"text": "Готово", "callback_data": "cat:done", "style": "success"},
        ]
    )
    return {"inline_keyboard": rows}


def category_index_from_callback(data: str) -> int | None:
    for prefix in ("cat:", "type:"):
        if data.startswith(prefix):
            try:
                index = int(data[len(prefix) :])
            except ValueError:
                return None
            if 0 <= index < len(CATEGORY_CHOICES):
                return index
            return None
    return None


def category_from_callback(data: str) -> CategoryChoice | None:
    idx = category_index_from_callback(data)
    return CATEGORY_CHOICES[idx] if idx is not None else None


def category_query_from_callback(data: str) -> str:
    category = category_from_callback(data)
    return category.query if category else ""


def normalize_city_name(city: str) -> str:
    return city.strip().casefold().replace("ё", "е")


def city_route(city: str) -> CityRoute | None:
    return CITY_ROUTES.get(normalize_city_name(city))


def maps_public_origin() -> str:
    """yandex.ru обычно резолвится там, где yandex.com даёт ERR_NAME_NOT_RESOLVED."""
    return settings.YANDEX_MAPS_ORIGIN.rstrip("/")


def build_yandex_category_url(category: CategoryChoice, city: str) -> str:
    origin = maps_public_origin()
    route = city_route(city)
    if route:
        base = f"{origin}/maps/{route.city_id}/{route.slug}"
        if category.category_slug:
            return f"{base}/category/{quote(category.category_slug)}/"
        path = category.path or category.label
        return f"{base}/search/{quote(path)}/"

    text = " ".join(part for part in [city.strip(), category.query] if part)
    return f"{origin}/maps/?text={quote_plus(text)}"


def parse_limit(value: str) -> int:
    if not value or not value.isdigit():
        raise ValueError("Укажи лимит числом, например: /limit 20")
    limit = int(value)
    if limit < 1:
        raise ValueError("Лимит должен быть положительным числом.")
    return limit


def lead_contacts(lead: Lead) -> str:
    contacts: list[str] = []
    if lead.phone:
        contacts.append(f"Телефон: {lead.phone}")
    if lead.email:
        contacts.append(f"Email: {lead.email}")
    if lead.social_links:
        contacts.append("Соцсети/мессенджеры: " + ", ".join(lead.social_links[:3]))
    if lead.website:
        contacts.append(f"Сайт: {lead.website}")
    if lead.yandex_url:
        contacts.append(f"Яндекс.Карты: {lead.yandex_url}")
    return "\n".join(contacts) if contacts else "Контакты не найдены."


def _compact_field(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def create_manager_txt(output_dir: Path, text: str, *, label: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_") or "batch"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = output_dir / f"organizations_{safe_label}_{stamp}.txt"
    path.write_text(text.strip() + "\n", encoding="utf-8-sig")
    return path


def target_mode_file_label(target_mode: str) -> str:
    return {
        "no_site": "без сайта",
        "redesign": "для переработки",
        "combined": "общий список",
    }.get(target_mode, "организации")


def create_manager_target_txts(
    output_dir: Path,
    items: list[tuple[Lead, str]],
    *,
    city: str,
    manager: str,
    target_mode: str,
    label: str,
) -> list[tuple[str, int, Path]]:
    modes = ("no_site", "redesign", "combined") if target_mode == "combined" else (target_mode,)
    exports: list[tuple[str, int, Path]] = []
    for mode in modes:
        selected = select_manager_leads(items, target_mode=mode)
        text = format_manager_leads_single_text(
            selected,
            city=city,
            manager=manager,
            target_mode=mode,
        )
        if not text:
            text = f"{target_mode_file_label(mode).capitalize()}\n\nНичего не найдено."
        path = create_manager_txt(output_dir, text, label=f"{label}_{mode}")
        exports.append((mode, len(selected), path))
    return exports


def lead_matches_manager_target(lead: Lead, target_mode: str) -> bool:
    redesign = str(lead.raw.get("website_platform", "")).casefold() in {
        "tilda",
        "wordpress",
        "yandex_business",
    }
    if target_mode == "redesign":
        return redesign
    if target_mode == "combined":
        return lead.website_absent_verified or redesign
    return lead.website_absent_verified


def select_manager_leads(
    items: list[tuple[Lead, str]],
    *,
    max_items: int | None = None,
    target_mode: str = "no_site",
) -> list[tuple[Lead, str]]:
    unique_items: list[tuple[Lead, str]] = []
    seen: set[str] = set()
    for lead, category in items:
        if not lead_matches_manager_target(lead, target_mode) or not lead.name.strip():
            continue
        key = lead_identity_key(lead) or "|".join(
            [lead.name.casefold().strip(), lead.address.casefold().strip(), lead.phone.strip()]
        )
        if key in seen:
            continue
        seen.add(key)
        unique_items.append((lead, category))
        if max_items is not None and len(unique_items) >= max_items:
            break
    return unique_items


def format_manager_lead_batches(
    items: list[tuple[Lead, str]],
    *,
    city: str,
    batch_size: int = 10,
    message_limit: int = 3800,
    max_items: int | None = None,
    manager: str = "",
    target_mode: str = "no_site",
) -> list[str]:
    unique_items = select_manager_leads(
        items,
        max_items=max_items,
        target_mode=target_mode,
    )

    if not unique_items:
        return []

    records: list[str] = []
    for index, (lead, fallback_category) in enumerate(unique_items, start=1):
        category = ", ".join(lead.categories[:2]) or fallback_category
        lines = [
            f"{index}. {_compact_field(lead.name, 90)}",
        ]
        if lead.yandex_url:
            lines.append(f"Ссылка: {lead.yandex_url}")
        contact_parts = [lead.phone.strip(), lead.email.strip(), *lead.social_links[:2]]
        contacts = ", ".join(item for item in contact_parts if item) or "не указан"
        lines.append(f"Контакт: {_compact_field(contacts, 180)}")
        if lead.address:
            lines.append(f"Адрес: {_compact_field(lead.address, 150)}")
        if lead.rating or lead.reviews:
            rating_parts = []
            if lead.rating:
                rating_parts.append(lead.rating)
            if lead.reviews:
                rating_parts.append(lead.reviews)
            lines.append("Рейтинг: " + "; ".join(rating_parts))
        platform = str(lead.raw.get("website_platform", "")).casefold()
        if platform:
            platform_label = {
                "tilda": "Tilda",
                "wordpress": "WordPress",
                "yandex_business": "Яндекс.Бизнес",
            }.get(platform, platform)
            lines.append(f"Переработка сайта: {platform_label}")
            if lead.website:
                lines.append(f"Сайт: {_compact_field(lead.website, 180)}")
        if category:
            lines.append(f"Категория: {_compact_field(category, 90)}")
        records.append("\n".join(lines))

    groups: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for record in records:
        added_length = len(record) + (2 if current else 0)
        if current and (len(current) >= batch_size or current_length + added_length > message_limit - 160):
            groups.append(current)
            current = []
            current_length = 0
        current.append(record)
        current_length += len(record) + (2 if len(current) > 1 else 0)
    if current:
        groups.append(current)

    total = len(unique_items)
    return [
        "\n\n".join(
            [
                (
                    f"Организации для переработки сайта — {city or 'город не указан'}"
                    if target_mode == "redesign"
                    else f"Организации без сайта и для переработки — {city or 'город не указан'}"
                    if target_mode == "combined"
                    else f"Организации без сайта — {city or 'город не указан'}"
                ),
                f"Дата: {datetime.now().strftime('%d.%m.%Y')}",
                (
                    "Задача: предложить переработку сайта"
                    if target_mode == "redesign"
                    else "Задача: предложить сайт, переработку или автоматизацию"
                    if target_mode == "combined"
                    else "Задача: предложить сайт или автоматизацию рутинных процессов"
                ),
                f"Менеджер: {manager}" if manager else "",
                f"Пакет {batch_index}/{len(groups)} · организаций: {len(group)} · всего: {total}",
                *group,
            ]
        )
        for batch_index, group in enumerate(groups, start=1)
    ]


def format_manager_leads_single_text(
    items: list[tuple[Lead, str]],
    *,
    city: str,
    manager: str = "",
    target_mode: str = "no_site",
) -> str:
    batches = format_manager_lead_batches(
        items,
        city=city,
        batch_size=max(1, len(items)),
        message_limit=10_000_000,
        manager=manager,
        target_mode=target_mode,
    )
    return "\n\n".join(batches)


def lead_description(lead: Lead) -> str:
    details: list[str] = []
    if lead.categories:
        details.append("Категория: " + ", ".join(lead.categories[:3]))
    if lead.address:
        details.append(f"Адрес: {lead.address}")
    if lead.rating:
        details.append(f"Рейтинг: {lead.rating}")
    if lead.reviews:
        details.append(f"Отзывы: {lead.reviews}")
    if lead.hours:
        details.append(f"Время работы: {lead.hours}")
    if lead.website_absent_verified:
        details.append("Сайт в карточке не найден: отсутствие подтверждено строгой проверкой.")
    elif not lead.has_website:
        details.append("Сайт в карточке не подтверждён: организация не попадает в список без сайта.")
    return "\n".join(details) if details else "Описание в карточке почти пустое."


def format_generated_message_card(message_item: GeneratedMessage) -> str:
    lead = message_item.lead
    return "\n".join(
        [
            "Подходящее заведение найдено",
            "",
            lead.name,
            lead_description(lead),
            "",
            "Контакты:",
            lead_contacts(lead),
            "",
            "Текст, который будет отправлен:",
            f"Тема: {message_item.subject}" if message_item.subject else "Тема: без темы",
            message_item.message,
        ]
    )


def lead_action_keyboard(action_id: str, lead: Lead) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = [
        [{"text": "Написать на контакты", "callback_data": f"mark_sent:{action_id}"}]
    ]
    url_buttons: list[dict[str, str]] = []
    if lead.yandex_url:
        url_buttons.append({"text": "Открыть в Я.Картах", "url": lead.yandex_url})
    first_social = first_http_url(lead.social_links)
    if first_social:
        url_buttons.append({"text": "Открыть контакт", "url": first_social})
    if url_buttons:
        rows.append(url_buttons[:2])
    return {"inline_keyboard": rows}


def first_http_url(values: list[str]) -> str:
    for value in values:
        if value.startswith(("http://", "https://")):
            return value
    return ""


def compact_blank_lines(text: str) -> str:
    lines: list[str] = []
    previous_blank = False
    for line in text.splitlines():
        blank = not line.strip()
        if blank and previous_blank:
            continue
        lines.append(line)
        previous_blank = blank
    return "\n".join(lines).strip()


def parse_job_payload(payload: str, default_city: str, default_limit: int) -> tuple[str, str, int]:
    if not payload:
        raise ValueError("Напиши запрос после команды, например: /run кофейни")

    parts = [part.strip() for part in payload.split("|")]
    query = parts[0]
    city = default_city
    limit = default_limit

    if not query:
        raise ValueError("Запрос пустой. Пример: /run кофейни")

    if len(parts) == 2:
        if parts[1].isdigit():
            limit = int(parts[1])
        else:
            city = parts[1]
    elif len(parts) >= 3:
        city = parts[1] or city
        if parts[2]:
            if not parts[2].isdigit():
                raise ValueError("Лимит должен быть числом. Пример: /run кофейни | Волгоград | 20")
            limit = int(parts[2])

    if not city:
        raise ValueError("Сначала задай город: /city Волгоград")
    if limit < 1:
        raise ValueError("Лимит должен быть положительным числом.")

    return query, city, limit


def split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = text
    while len(current) > limit:
        split_at = current.rfind("\n", 0, limit)
        if split_at < 1:
            split_at = limit
        chunks.append(current[:split_at].strip())
        current = current[split_at:].strip()
    if current:
        chunks.append(current)
    return chunks


def network_help_text() -> str:
    return (
        "Проверь доступ из WSL: getent hosts api.telegram.org. "
        "Если адрес не находится, включи VPN/прокси, исправь DNS для WSL "
        "или проверь maps_parser/settings.py (DNS_SERVERS, SKIP_DNS_CHECK=0)."
    )
