import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from maps_parser.models import Lead
from maps_parser.pipeline import (
    filter_collectable_leads,
    filter_new_leads,
    output_path,
    target_skip_urls,
)
from maps_parser.storage import (
    append_leads_unique_csv,
    append_processed_leads_csv,
    lead_identity_key,
    read_processed_lead_keys,
)


class ProcessedLeadTests(unittest.TestCase):
    def test_target_registries_are_independent(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory)
            lead_a = Lead(name="A", yandex_url="https://yandex.ru/maps/org/a/1/")
            lead_b = Lead(name="B", yandex_url="https://yandex.ru/maps/org/b/2/")
            append_processed_leads_csv(output / "processed_no_site.csv", [lead_a])
            append_processed_leads_csv(output / "processed_redesign.csv", [lead_b])

            self.assertIn("https://yandex.ru/maps/org/a/1", target_skip_urls(output, "no_site"))
            self.assertNotIn("https://yandex.ru/maps/org/a/1", target_skip_urls(output, "redesign"))
            self.assertEqual(target_skip_urls(output, "combined"), set())

    def test_result_database_deduplicates(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "redesign_leads.csv"
            lead = Lead(
                name="A",
                website="https://example.ru",
                yandex_url="https://yandex.ru/maps/org/a/1/",
                raw={"website_platform": "tilda"},
            )
            self.assertEqual(append_leads_unique_csv(path, [lead]), 1)
            self.assertEqual(append_leads_unique_csv(path, [lead]), 0)
            self.assertIn("website_platform", path.read_text(encoding="utf-8-sig"))

    def test_output_paths_are_unique_and_labeled(self) -> None:
        with TemporaryDirectory() as directory:
            first = output_path(Path(directory), "leads", "csv", "Где поесть")
            second = output_path(Path(directory), "leads", "csv", "Где поесть")
        self.assertNotEqual(first, second)
        self.assertIn("Где_поесть", first.name)

    def test_lead_contact_filters(self) -> None:
        phone = Lead(name="Телефон", phone="+7 900 000-00-00")
        email = Lead(name="Email", email="test@example.org")
        empty = Lead(name="Без контакта")

        self.assertTrue(phone.matches_contact_filter("phone"))
        self.assertTrue(email.matches_contact_filter("any"))
        self.assertFalse(email.matches_contact_filter("phone"))
        self.assertFalse(empty.matches_contact_filter("any"))
        self.assertTrue(empty.matches_contact_filter("all"))

    def test_lead_identity_prefers_yandex_url(self) -> None:
        lead = Lead(name="Кафе", address="Адрес", yandex_url="https://yandex.ru/maps/org/test/123/")

        self.assertEqual(lead_identity_key(lead), "yandex_url:https://yandex.ru/maps/org/test/123")

    def test_append_processed_leads_deduplicates(self) -> None:
        lead = Lead(name="Кафе", address="Адрес", yandex_url="https://yandex.ru/maps/org/test/123/")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "processed_leads.csv"

            self.assertEqual(append_processed_leads_csv(path, [lead]), 1)
            self.assertEqual(append_processed_leads_csv(path, [lead]), 0)
            self.assertEqual(len(read_processed_lead_keys(path)), 1)

    def test_filter_new_leads_skips_existing_and_run_duplicates(self) -> None:
        lead = Lead(name="Кафе", address="Адрес", yandex_url="https://yandex.ru/maps/org/test/123/")
        same_lead = Lead(name="Кафе", address="Адрес", yandex_url="https://yandex.ru/maps/org/test/123/")
        other_lead = Lead(name="Бар", address="Другой адрес")

        new_leads, skipped = filter_new_leads([lead, same_lead, other_lead], set())

        self.assertEqual(new_leads, [lead, other_lead])
        self.assertEqual(skipped, 1)

    def test_filter_collectable_leads_skips_yandex_service_pages(self) -> None:
        lead = Lead(name="Кафе", address="Адрес", yandex_url="https://yandex.ru/maps/org/test/123/")
        captcha = Lead(
            name="Подтвердите, что запросы отправляли вы, а не робот",
            yandex_url="https://yandex.ru/showcaptcha?retpath=maps",
        )
        cookie = Lead(name="Yandex uses cookies", yandex_url="https://yandex.ru/showcaptcha?retpath=maps")

        new_leads, skipped = filter_collectable_leads([lead, captcha, cookie])

        self.assertEqual(new_leads, [lead])
        self.assertEqual(skipped, 2)


if __name__ == "__main__":
    unittest.main()
