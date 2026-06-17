import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from maps_parser.deepseek import parse_json_response
from maps_parser.models import Lead
from maps_parser.yandex_maps import (
    CardLogBatcher,
    MapsScrapeJob,
    _candidate_urls_from_dom_items,
    _feed_collection_target,
    _no_site_scan_limit,
    _maps_subdomain_variant,
    _maps_text_search_urls,
    _parse_playwright_proxy,
    _playwright_proxy_configs,
    _proxy_index_for_card,
    _scrape_should_stop,
    _scrape_yandex_maps_long_pool,
    canonical_org_url,
    canonical_social_url,
    clean_rating_text,
    clean_address_text,
    clean_reviews_text,
    detect_website_platform_from_html,
    is_yandex_service_lead,
    is_yandex_service_url,
    is_business_website,
    phones_from_links,
    phones_from_text,
    normalize_phone,
    org_slug_identity,
    extract_urls_from_text,
    _resolve_host_for_proxy,
)


class ParsingTests(unittest.TestCase):
    def test_rating_and_reviews_reject_dom_blob(self) -> None:
        blob = (
            "1 отзыв Ремонт оборудования Автосервис Поиск Маршруты "
            "Отзывы 1 Особенности Адрес"
        )
        self.assertEqual(clean_rating_text(blob), "")
        self.assertEqual(clean_reviews_text(blob), "1 отзыв")
        self.assertEqual(clean_reviews_text("огромный текст без количества отзывов"), "")
        self.assertEqual(
            clean_address_text("Адрес: Прямая ул., 40, Аликоновка Маршрут Контакты +7 900"),
            "Прямая ул., 40, Аликоновка",
        )

    def test_detect_website_platform(self) -> None:
        self.assertEqual(
            detect_website_platform_from_html("https://example.ru", '<script src="/wp-content/a.js">'),
            "wordpress",
        )
        self.assertEqual(
            detect_website_platform_from_html("https://example.ru", "Made on Tilda"),
            "tilda",
        )
        self.assertEqual(
            detect_website_platform_from_html("https://shop.clients.site", ""),
            "yandex_business",
        )

    def test_org_slug_identity_ignores_branch_id(self) -> None:
        first = "https://yandex.ru/maps/org/s_coffee/240550325422/"
        second = "https://yandex.ru/maps/org/s_coffee/155061127620/"
        self.assertEqual(org_slug_identity(first), "s_coffee")
        self.assertEqual(org_slug_identity(first), org_slug_identity(second))

    def test_long_mode_collects_four_searches_before_checking_pool(self) -> None:
        jobs = [
            MapsScrapeJob(
                query=f"q{index}",
                location="Сочи",
                limit=1000,
            )
            for index in range(5)
        ]
        collected = AsyncMock(
            side_effect=lambda job, **kwargs: [
                f"https://yandex.ru/maps/org/test/{job.query.removeprefix('q')}/"
            ]
        )
        checked = AsyncMock(
            return_value=(
                [(0, Lead(name="Без сайта", website_status="absent"))],
                1,
            )
        )
        with (
            patch("maps_parser.yandex_maps._collect_long_mode_urls", collected),
            patch("maps_parser.yandex_maps._check_long_mode_pool", checked),
        ):
            asyncio.run(
                _scrape_yandex_maps_long_pool(
                    jobs,
                    context=object(),
                    proxy_pool=None,
                    skip_urls=set(),
                    logger=lambda _: None,
                    delay_seconds=30,
                    light_parse=False,
                    contact_filter="all",
                    overall_no_site_limit=1,
                    on_lead_checked=None,
                    should_stop=None,
                )
            )

        self.assertEqual(collected.await_count, 4)
        self.assertTrue(all(call.kwargs["limit"] == 100 for call in collected.await_args_list))
        self.assertEqual(checked.await_count, 1)

    def test_scrape_should_stop_on_no_site_target(self) -> None:
        leads = [
            Lead(name="A", website="https://a.ru"),
            Lead(name="B", website=""),
        ]
        self.assertTrue(_scrape_should_stop(leads, limit=10, stop_after_no_site=1))
        self.assertFalse(
            _scrape_should_stop(
                [Lead(name="Без телефона", website="")],
                limit=10,
                stop_after_no_site=1,
                contact_filter="phone",
            )
        )

    def test_card_log_batcher_flushes_in_batches(self) -> None:
        lines: list[str] = []
        batcher = CardLogBatcher(lines.append, batch_size=2)
        lead = Lead(name="Кафе", website="")
        batcher.append(1, 3, "https://yandex.ru/maps/org/test/1/", lead)
        self.assertEqual(lines, [])
        batcher.append(2, 3, "https://yandex.ru/maps/org/test/2/", lead)
        self.assertEqual(len(lines), 1)
        self.assertIn("[1/3]", lines[0])
        self.assertIn("[2/3]", lines[0])

    def test_feed_collection_target_stays_near_card_limit(self) -> None:
        self.assertEqual(_feed_collection_target(100, 0), 116)
        self.assertLessEqual(_feed_collection_target(100, 0), 150)
        self.assertEqual(_feed_collection_target(100, 10_000), 166)
        self.assertLessEqual(_feed_collection_target(500, 10_000), 220)

    def test_no_site_scan_uses_five_card_batches(self) -> None:
        self.assertEqual(_no_site_scan_limit(10), 50)
        self.assertEqual(_no_site_scan_limit(35), 175)
        self.assertEqual(_no_site_scan_limit(300), 1000)

    def test_maps_subdomain_variant_does_not_duplicate_maps_path(self) -> None:
        primary = (
            "https://yandex.ru/maps/11062/kislovodsk/search/%D0%93%D0%B4%D0%B5%20%D0%BF%D0%BE%D0%B5%D1%81%D1%82%D1%8C/"
        )
        self.assertEqual(
            _maps_subdomain_variant(primary),
            "https://maps.yandex.ru/11062/kislovodsk/search/%D0%93%D0%B4%D0%B5%20%D0%BF%D0%BE%D0%B5%D1%81%D1%82%D1%8C/",
        )
        self.assertEqual(
            _maps_subdomain_variant("https://maps.yandex.ru/maps/11062/kislovodsk/search/foo/"),
            "https://maps.yandex.ru/11062/kislovodsk/search/foo/",
        )

    def test_maps_text_search_urls_use_correct_maps_prefix(self) -> None:
        urls = _maps_text_search_urls("где поесть")
        self.assertIn("https://yandex.ru/maps/?text=", urls[0])
        self.assertIn("https://maps.yandex.ru/?text=", urls[1])
        self.assertNotIn("/maps/maps/", " ".join(urls))

    def test_canonical_org_url_removes_tabs_and_query(self) -> None:
        self.assertEqual(
            canonical_org_url("https://yandex.com/maps/org/name/123/reviews/?ll=1"),
            "https://yandex.com/maps/org/name/123/",
        )


    def test_contact_links_are_not_business_websites(self) -> None:
        self.assertFalse(is_business_website("https://viber.click/79054100660"))
        self.assertFalse(is_business_website("https://wa.me/79054100660"))
        self.assertFalse(is_business_website("https://clck.ru/3QG9Rm"))
        self.assertFalse(is_business_website("http://ogp.me/ns"))
        self.assertFalse(is_business_website("http://www.w3.org/2000/svg"))
        self.assertFalse(is_business_website("https://schema.org/Restaurant"))
        self.assertTrue(is_business_website("https://kafekonushnya.ru/"))
        self.assertTrue(is_business_website("https://example-cafe.ru/"))

    def test_phone_normalization_from_visible_text(self) -> None:
        self.assertEqual(normalize_phone("8 (800) 550-35-35"), "+78005503535")
        self.assertEqual(normalize_phone("+7 968 270-00-13"), "+79682700013")
        self.assertEqual(normalize_phone("+7 938 453-29-773"), "")
        self.assertEqual(phones_from_text("Телефон: +7 (968) 270-00-13, id 237724793964"), ["+79682700013"])

    def test_extract_urls_from_escaped_yandex_state(self) -> None:
        payload = r'{"url":"https:\/\/example-cafe.ru\/contacts?utm=yandex"}'
        self.assertEqual(extract_urls_from_text(payload), ["https://example-cafe.ru/contacts?utm=yandex"])

    def test_candidate_urls_ignore_service_text_and_attributes(self) -> None:
        items = [
            {
                "href": "https://real-business.ru/",
                "text": "https://unrelated.example/",
                "aria": "",
                "title": "",
                "attrs": '{"schema":"https://another-unrelated.example/"}',
            }
        ]
        self.assertEqual(_candidate_urls_from_dom_items(items), ["https://real-business.ru/"])

    def test_yandex_captcha_url_is_service_page(self) -> None:
        self.assertTrue(is_yandex_service_url("https://yandex.ru/showcaptcha?retpath=maps"))
        self.assertFalse(is_yandex_service_url("https://yandex.ru/maps/org/test/123/"))

    def test_yandex_captcha_lead_is_service_page(self) -> None:
        lead = Lead(
            name="Подтвердите, что запросы отправляли вы, а не робот",
            yandex_url="https://yandex.ru/showcaptcha?retpath=maps",
        )
        self.assertTrue(is_yandex_service_lead(lead))

    def test_canonical_social_url_normalizes_whatsapp_link(self) -> None:
        self.assertEqual(
            canonical_social_url("https://wa.me/+7+(968)+270-00-13"),
            "https://wa.me/79682700013",
        )

    def test_phones_from_messenger_links(self) -> None:
        self.assertEqual(
            phones_from_links(
                [
                    "https://wa.me/+79275117004",
                    "https://t.me/+79275117004",
                    "https://wa.me/+7+(968)+270-00-13",
                ]
            ),
            ["+79275117004", "+79682700013"],
        )

    @patch("maps_parser.yandex_maps._resolve_a_via_public_dns", return_value=None)
    def test_resolve_host_yastatic_net_apex_without_dns(self, _: object) -> None:
        self.assertEqual(_resolve_host_for_proxy("yastatic.net"), "93.158.134.91")

    @patch("maps_parser.yandex_maps._resolve_a_via_public_dns", return_value=None)
    def test_resolve_host_yastatic_subdomain_maps_static_ip_not_maps_yandex_ru(self, _: object) -> None:
        self.assertEqual(_resolve_host_for_proxy("mc.yastatic.net"), "93.158.134.91")

    def test_parse_json_response_from_fence(self) -> None:
        self.assertEqual(
            parse_json_response('```json\n{"subject":"Тема","message":"Текст"}\n```'),
            {"subject": "Тема", "message": "Текст"},
        )

    def test_parse_playwright_proxy_with_auth(self) -> None:
        self.assertEqual(
            _parse_playwright_proxy("http://user:pass@1.2.3.4:8080"),
            {
                "server": "http://1.2.3.4:8080",
                "username": "user",
                "password": "pass",
            },
        )

    def test_proxy_index_for_card_rotates_every_five(self) -> None:
        self.assertEqual(_proxy_index_for_card(1, 5, links_per=5), 0)
        self.assertEqual(_proxy_index_for_card(5, 5, links_per=5), 0)
        self.assertEqual(_proxy_index_for_card(6, 5, links_per=5), 1)
        self.assertEqual(_proxy_index_for_card(26, 5, links_per=5), 0)

    @patch("maps_parser.yandex_maps.settings.PLAYWRIGHT_PROXIES", "http://1.1.1.1:1,http://2.2.2.2:2")
    @patch("maps_parser.yandex_maps.settings.PLAYWRIGHT_PROXY", "")
    def test_playwright_proxy_configs_reads_list(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            configs = _playwright_proxy_configs()
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[0]["server"], "http://1.1.1.1:1")
        self.assertEqual(configs[1]["server"], "http://2.2.2.2:2")

    @patch("maps_parser.yandex_maps.settings.PLAYWRIGHT_PROXIES", "http://1.1.1.1:1")
    @patch("maps_parser.yandex_maps.settings.PLAYWRIGHT_PROXY", "")
    def test_playwright_proxy_env_overrides_settings(self) -> None:
        with patch.dict("os.environ", {"PLAYWRIGHT_PROXY": "socks5://172.18.0.2:10808"}, clear=True):
            configs = _playwright_proxy_configs()
        self.assertEqual(configs, [{"server": "socks5://172.18.0.2:10808"}])

    @patch("maps_parser.yandex_maps.settings.PLAYWRIGHT_PROXIES", "")
    @patch("maps_parser.yandex_maps.settings.PLAYWRIGHT_PROXY", "")
    def test_playwright_proxy_configs_reads_all_proxy(self) -> None:
        with patch.dict("os.environ", {"ALL_PROXY": "socks5h://172.18.0.2:10808"}, clear=True):
            configs = _playwright_proxy_configs()
        self.assertEqual(configs, [{"server": "socks5h://172.18.0.2:10808"}])


if __name__ == "__main__":
    unittest.main()
