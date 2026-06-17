import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse

from maps_parser import settings
from maps_parser.models import Lead
from maps_parser.telegram_bot import (
    CATEGORY_CHOICES,
    ChatState,
    TELEGRAM_LIMIT_CAP,
    TelegramLeadBot,
    _is_job_progress_ping,
    _TELEGRAM_API_FALLBACK_IPS,
    _telegram_api_connect_ips,
    _telegram_api_hostname,
    _telegram_socks5_proxy_from_env,
    build_yandex_category_url,
    category_keyboard,
    category_query_from_callback,
    city_route,
    create_manager_txt,
    create_manager_target_txts,
    format_manager_lead_batches,
    format_manager_leads_single_text,
    format_selected_categories_line,
    main_menu_keyboard,
    maps_public_origin,
    network_retry_delay,
    parse_job_payload,
    parse_cities,
    parse_limit,
    parse_td_url,
    parse_yandex_org_urls,
    select_manager_leads,
    split_telegram_text,
    start_reply_keyboard,
    strip_command,
)
from maps_parser.yandex_maps import is_yandex_maps_org_url


class TelegramBotTests(unittest.TestCase):
    def test_is_job_progress_ping(self) -> None:
        self.assertTrue(_is_job_progress_ping("Поиск: собрано организаций 20/100."))
        self.assertTrue(_is_job_progress_ping("[21/67] ❌ есть сайт — Веранда\nhttps://example"))
        self.assertFalse(_is_job_progress_ping("Задача завершена."))

    def test_parse_job_with_default_city_and_limit(self) -> None:
        self.assertEqual(parse_job_payload("кофейни", "Волгоград", 10), ("кофейни", "Волгоград", 10))

    def test_parse_job_with_city_and_limit(self) -> None:
        self.assertEqual(parse_job_payload("отели | Казань | 20", "Волгоград", 10), ("отели", "Казань", 20))

    def test_parse_limit_has_no_upper_cap(self) -> None:
        self.assertEqual(parse_limit(str(TELEGRAM_LIMIT_CAP)), TELEGRAM_LIMIT_CAP)
        self.assertEqual(parse_limit("123"), 123)

    def test_network_retry_schedule(self) -> None:
        self.assertEqual(network_retry_delay(1), 10.0)
        self.assertEqual(network_retry_delay(2), 30.0)
        self.assertEqual(network_retry_delay(3), 600.0)
        self.assertEqual(network_retry_delay(100), 600.0)

    def test_parse_job_requires_city(self) -> None:
        with self.assertRaises(ValueError):
            parse_job_payload("кафе", "", 10)

    def test_parse_cities_for_long_mode(self) -> None:
        self.assertEqual(
            parse_cities("Москва, Волгоград, Москва, Калининград"),
            ["Москва", "Волгоград", "Калининград"],
        )

    def test_parse_cities_is_available_for_every_profile(self) -> None:
        for profile in ("fast", "long"):
            state = ChatState(run_profile=profile)
            state.city = ", ".join(parse_cities("Сочи, Адлер, Сочи"))
            self.assertEqual(state.city, "Сочи, Адлер")

    def test_parse_td_url_extracts_yandex_org_link(self) -> None:
        url = "https://yandex.ru/maps/org/test/1234567890/"
        self.assertEqual(parse_td_url(f"/td {url}"), url)
        self.assertTrue(is_yandex_maps_org_url(url))

    def test_parse_yandex_org_urls_deduplicates_links(self) -> None:
        first = "https://yandex.ru/maps/org/one/1/"
        second = "https://yandex.ru/maps/org/two/2/?z=10"
        self.assertEqual(
            parse_yandex_org_urls(f"{first}\n{second}\n{first}\nhttps://example.org/"),
            [first, second],
        )

    def test_strip_command_accepts_bot_suffix(self) -> None:
        self.assertEqual(strip_command("/run@my_bot кафе", "/run"), "кафе")

    def test_split_telegram_text(self) -> None:
        chunks = split_telegram_text("a" * 5000, limit=1000)
        self.assertEqual(len(chunks), 5)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))

    def test_manager_lead_batches_group_by_ten_and_filter_websites(self) -> None:
        items = [
            (
                Lead(
                    name=f"Организация {index}",
                    phone=f"+7 900 000-00-{index:02d}",
                    address=f"Адрес {index}",
                    yandex_url=f"https://yandex.ru/maps/org/test/{index}/",
                ),
                "Кафе",
            )
            for index in range(1, 13)
        ]
        items.append((items[0][0], "Где поесть"))
        items.append(
            (
                Lead(
                    name="С сайтом",
                    website="https://example.org",
                    yandex_url="https://yandex.ru/maps/org/site/99/",
                ),
                "Кафе",
            )
        )

        batches = format_manager_lead_batches(items, city="Кисловодск")

        self.assertEqual(len(batches), 2)
        self.assertIn("Пакет 1/2 · организаций: 10 · всего: 12", batches[0])
        self.assertIn("Контакт: +7 900 000-00-01", batches[0])
        self.assertIn("Адрес: Адрес 1", batches[0])
        self.assertIn("Ссылка: https://yandex.ru/maps/org/test/1/", batches[0])
        self.assertLess(batches[0].index("Ссылка:"), batches[0].index("Контакт:"))
        self.assertLess(batches[0].index("Контакт:"), batches[0].index("Адрес:"))
        self.assertNotIn("С сайтом", "\n".join(batches))

    def test_manager_lead_batches_respect_total_limit(self) -> None:
        items = [
            (
                Lead(name=f"Организация {index}", yandex_url=f"https://yandex.ru/maps/org/test/{index}/"),
                "Кафе",
            )
            for index in range(1, 16)
        ]

        batches = format_manager_lead_batches(items, city="Кисловодск", max_items=10)

        self.assertEqual(len(batches), 1)
        self.assertIn("организаций: 10 · всего: 10", batches[0])
        self.assertNotIn("11. Организация 11", batches[0])

    def test_telegram_api_hostname(self) -> None:
        self.assertEqual(_telegram_api_hostname("https://api.telegram.org"), "api.telegram.org")
        self.assertEqual(_telegram_api_hostname(""), "api.telegram.org")

    def test_telegram_api_connect_ips_fallback(self) -> None:
        with patch.object(settings, "SKIP_DNS_CHECK", 0):
            with patch("maps_parser.telegram_bot._resolve_a_via_public_dns", return_value=None):
                self.assertEqual(
                    _telegram_api_connect_ips("api.telegram.org"),
                    list(_TELEGRAM_API_FALLBACK_IPS),
                )

    def test_telegram_api_uses_socks_proxy_env_instead_of_direct_ips(self) -> None:
        with patch.dict("os.environ", {"ALL_PROXY": "socks5h://172.18.0.2:10808"}, clear=True):
            self.assertEqual(_telegram_socks5_proxy_from_env(), "socks5h://172.18.0.2:10808")
            self.assertEqual(_telegram_api_connect_ips("api.telegram.org"), [])

    def test_category_keyboard_contains_all_choices(self) -> None:
        keyboard = category_keyboard(columns=2, state=ChatState(), default_limit=10)
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        cat_buttons = [
            b
            for b in buttons
            if b["callback_data"].startswith("cat:")
            and b["callback_data"] not in {"cat:clear", "cat:done"}
        ]

        self.assertEqual(len(cat_buttons), len(CATEGORY_CHOICES))
        self.assertEqual(cat_buttons[0]["callback_data"], "cat:0")
        self.assertTrue(any(b["callback_data"] == "cat:clear" for b in buttons))
        self.assertTrue(any(b["callback_data"] == "cat:done" for b in buttons))

    def test_main_menu_is_compact_and_colored(self) -> None:
        keyboard = main_menu_keyboard(
            ChatState(city="Сочи", limit=30, selected_category_indices=[1, 2]),
            default_limit=10,
        )
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        self.assertTrue(any(b["callback_data"] == "cat:menu" for b in buttons))
        self.assertTrue(any(b["callback_data"] == "profile:menu" for b in buttons))
        self.assertTrue(any(b["text"] == "Город: Сочи" for b in buttons))
        self.assertTrue(any(b["callback_data"] == "lim:custom" for b in buttons))
        self.assertTrue(any(b["callback_data"] == "run:go" for b in buttons))
        self.assertTrue(any(b["callback_data"] == "progress:download" for b in buttons))
        self.assertTrue(any(b["callback_data"] == "target:no_site" for b in buttons))
        self.assertTrue(any(b["callback_data"] == "target:redesign" for b in buttons))
        self.assertTrue(any(b["callback_data"] == "target:combined" for b in buttons))
        self.assertTrue(all("style" in b for b in buttons))

    def test_format_selected_categories_line(self) -> None:
        self.assertFalse(ChatState().generate)
        self.assertEqual(format_selected_categories_line(ChatState()), "не выбраны")
        self.assertEqual(
            format_selected_categories_line(ChatState(selected_category_indices=[0, 2])),
            "Хорошие места; Отели",
        )

    def test_start_button_is_reply_keyboard(self) -> None:
        keyboard = start_reply_keyboard()
        self.assertEqual(keyboard["keyboard"][0][0]["text"], "▶️ Старт")
        self.assertTrue(keyboard["resize_keyboard"])
        running = start_reply_keyboard(running=True)
        self.assertEqual(running["keyboard"][0][0]["text"], "⏹ Стоп")

    def test_create_manager_txt(self) -> None:
        with TemporaryDirectory() as directory:
            path = create_manager_txt(Path(directory), "1. Организация\nСсылка: test", label="batch_1")
            self.assertTrue(path.exists())
            self.assertIn("1. Организация", path.read_text(encoding="utf-8-sig"))

    def test_single_progress_text_contains_all_items(self) -> None:
        items = [
            (
                Lead(
                    name=f"Организация {index}",
                    yandex_url=f"https://yandex.ru/maps/org/test/{index}/",
                    website_status="absent",
                ),
                "Кафе",
            )
            for index in range(1, 82)
        ]
        text = format_manager_leads_single_text(items, city="Сочи")
        self.assertIn("организаций: 81", text)
        self.assertIn("81. Организация 81", text)

    def test_combined_progress_creates_three_target_files(self) -> None:
        no_site = Lead(name="Без сайта", website_status="absent")
        redesign = Lead(
            name="На Tilda",
            website="https://example.ru",
            raw={"website_platform": "tilda"},
        )
        with TemporaryDirectory() as directory:
            exports = create_manager_target_txts(
                Path(directory),
                [(no_site, "Кафе"), (redesign, "Отели")],
                city="Сочи",
                manager="",
                target_mode="combined",
                label="progress",
            )
        self.assertEqual([(mode, count) for mode, count, _ in exports], [
            ("no_site", 1),
            ("redesign", 1),
            ("combined", 2),
        ])

    def test_select_manager_leads_supports_redesign_mode(self) -> None:
        lead = Lead(
            name="Сайт на Tilda",
            website="https://example.ru",
            raw={"website_platform": "tilda"},
        )
        self.assertEqual(
            len(select_manager_leads([(lead, "Кафе")], target_mode="redesign")),
            1,
        )

    def test_speed_and_target_are_independent(self) -> None:
        for speed in ("fast", "long"):
            for target in ("no_site", "redesign", "combined"):
                state = ChatState(run_profile=speed, target_mode=target)
                self.assertEqual(state.run_profile, speed)
                self.assertEqual(state.target_mode, target)

    def test_category_query_from_callback(self) -> None:
        self.assertEqual(category_query_from_callback("cat:1"), CATEGORY_CHOICES[1].query)
        self.assertEqual(category_query_from_callback("type:1"), CATEGORY_CHOICES[1].query)
        self.assertEqual(category_query_from_callback("bad"), "")
        self.assertEqual(category_query_from_callback("cat:999"), "")

    def test_categories_match_yandex_screenshot(self) -> None:
        self.assertEqual(
            [category.label for category in CATEGORY_CHOICES],
            [
                "Хорошие места",
                "Где поесть",
                "Отели",
                "Продукты",
                "Аптеки",
                "Торговые центры",
                "Кафе",
                "АЗС",
                "Музеи",
                "Банкоматы",
                "Автосервисы",
                "Госуслуги",
                "Больницы",
                "Салоны красоты",
                "Спорт",
            ],
        )

    def test_build_yandex_category_url_follows_city_route_and_category(self) -> None:
        """Ожидание собирается из maps_public_origin, CITY_ROUTES и полей категории."""
        origin = urlparse(maps_public_origin())
        eat = CATEGORY_CHOICES[1]
        route_k = city_route("Кисловодск")
        assert route_k is not None
        url_eat = build_yandex_category_url(eat, "Кисловодск")
        parsed = urlparse(url_eat)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, origin.netloc)
        expected_search = f"/maps/{route_k.city_id}/{route_k.slug}/search/{quote(eat.path or eat.label)}/"
        self.assertEqual(parsed.path, expected_search)
        segment = parsed.path.split("/search/", 1)[1].removesuffix("/")
        self.assertEqual(unquote(segment), eat.path or eat.label)

        hotels = CATEGORY_CHOICES[2]
        route_m = city_route("Москва")
        assert route_m is not None
        url_hotels = build_yandex_category_url(hotels, "Москва")
        ph = urlparse(url_hotels)
        self.assertEqual(ph.scheme, "https")
        self.assertEqual(ph.netloc, origin.netloc)
        expected_cat = f"/maps/{route_m.city_id}/{route_m.slug}/category/{quote(hotels.category_slug)}/"
        self.assertEqual(ph.path, expected_cat)

    def test_build_yandex_category_url_astrakhan_in_city_routes(self) -> None:
        route = city_route("Астрахань")
        self.assertIsNotNone(route)
        self.assertEqual(route.slug, "astrahan")
        cat = CATEGORY_CHOICES[1]
        parsed = urlparse(build_yandex_category_url(cat, "Астрахань"))
        self.assertEqual(parsed.path, f"/maps/{route.city_id}/{route.slug}/search/{quote(cat.path or cat.label)}/")

    def test_build_yandex_category_url_unknown_city_uses_text_query(self) -> None:
        """Города нет в CITY_ROUTES — ссылка через ?text=город + запрос категории."""
        cat = CATEGORY_CHOICES[1]
        parsed = urlparse(build_yandex_category_url(cat, "Тюмень"))
        self.assertEqual(parsed.path, "/maps/")
        self.assertIsNotNone(parsed.query)
        self.assertIn("text=", parsed.query)

    def test_bot_instantiates_with_slots(self) -> None:
        with TemporaryDirectory() as directory:
            bot = TelegramLeadBot(token="test", output_dir=Path(directory), default_city="Волгоград")
            self.assertEqual(bot.active_jobs, set())
            self.assertEqual(bot.active_notifiers, {})
            self.assertEqual(bot.default_city, "Волгоград")
            state = bot.chat_state(1)
            state.contact_filter = "any"
            state.manager = "Иван"
            state.last_batch_id = "batch-1"
            bot.save_state()

            restored = TelegramLeadBot(token="test", output_dir=Path(directory)).chat_state(1)
            self.assertEqual(restored.contact_filter, "any")
            self.assertEqual(restored.manager, "Иван")
            self.assertEqual(restored.last_batch_id, "batch-1")

    def test_stop_button_cancels_pending_notifications_once(self) -> None:
        with TemporaryDirectory() as directory:
            bot = TelegramLeadBot(token="test", output_dir=Path(directory))
            stop_event = __import__("threading").Event()
            notifier = __import__("maps_parser.telegram_bot", fromlist=["JobNotifyQueue"]).JobNotifyQueue(bot, 1)
            bot.active_jobs.add(1)
            bot.stop_events[1] = stop_event
            bot.active_notifiers[1] = notifier
            with patch.object(TelegramLeadBot, "send_message") as send:
                bot.handle_update({"message": {"chat": {"id": 1}, "text": "⏹ Стоп"}})
                bot.handle_update({"message": {"chat": {"id": 1}, "text": "⏹ Стоп"}})
            self.assertTrue(stop_event.is_set())
            self.assertTrue(notifier._cancelled.is_set())
            self.assertEqual(send.call_count, 1)

    def test_old_callback_answer_does_not_crash(self) -> None:
        body = b'{"ok":false,"error_code":400,"description":"Bad Request: query is too old and response timeout expired or query ID is invalid"}'
        error = HTTPError("https://api.telegram.org", 400, "Bad Request", {}, BytesIO(body))

        with TemporaryDirectory() as directory:
            bot = TelegramLeadBot(token="test", output_dir=Path(directory))
            with patch("maps_parser.telegram_bot._telegram_api_post_bytes", side_effect=error):
                bot.answer_callback_query("old-callback-id")


if __name__ == "__main__":
    unittest.main()
