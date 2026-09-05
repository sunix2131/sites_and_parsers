import unittest

from maps_parser import settings
from maps_parser.run_modes import FAST_MODE, LONG_MODE


class SettingsTests(unittest.TestCase):
    def test_settings_are_plain_values(self) -> None:
        self.assertEqual(settings.PARALLEL_ORG_TABS, 1)
        self.assertEqual(settings.CARD_LOG_BATCH, 1)
        self.assertEqual(settings.PAGE_DELAY, 5.0)
        self.assertEqual(settings.SEARCH_SCROLL_STEP_PX, 390)
        self.assertEqual(settings.SEARCH_SCROLL_PAUSE_MS, 450)
        self.assertEqual(settings.PAGE_DELAY_JITTER_SECONDS, 7.0)
        self.assertEqual(settings.CARD_COOLDOWN_AFTER, 0)
        self.assertEqual(settings.CARD_COOLDOWN_SECONDS, 0.0)
        self.assertEqual(settings.NO_SITE_SCAN_MULTIPLIER, 20)
        self.assertEqual(settings.NO_SITE_SCAN_MAX_CARDS, 1000)
        self.assertEqual(settings.WEBSITE_RECHECK_ATTEMPTS, 1)
        self.assertEqual(settings.CAPTCHA_AUTO_RETRY_SECONDS, 900)
        self.assertEqual(settings.CAPTCHA_AUTO_RETRY_MAX, 3)
        self.assertFalse(settings.TELEGRAM_BROWSER_VISIBLE)

    def test_run_modes(self) -> None:
        self.assertEqual(FAST_MODE.page_delay, 2.0)
        self.assertEqual(LONG_MODE.page_delay, 30.0)
        self.assertEqual(LONG_MODE.captcha_retry_seconds, 600)
        self.assertLess(LONG_MODE.scroll_step_px, FAST_MODE.scroll_step_px)
        self.assertGreater(LONG_MODE.scroll_pause_ms, FAST_MODE.scroll_pause_ms)
        long_settings = LONG_MODE.parser_settings()
        self.assertEqual(long_settings["LONG_POOL_MODE"], 1)
        self.assertEqual(long_settings["LONG_POOL_SEARCHES"], 4)
        self.assertEqual(long_settings["LONG_INITIAL_SEARCH_LINKS"], 100)
        self.assertEqual(long_settings["LONG_NEXT_SEARCH_LINKS"], 50)


if __name__ == "__main__":
    unittest.main()
