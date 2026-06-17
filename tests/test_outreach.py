import unittest

from maps_parser.models import Lead
from maps_parser.outreach import build_user_prompt, system_prompt
from maps_parser import settings


class OutreachTests(unittest.TestCase):
    def test_prompts_use_settings(self) -> None:
        self.assertEqual(system_prompt(), settings.OUTREACH_SYSTEM_PROMPT)
        prompt = build_user_prompt(Lead(name="Кафе", address="Адрес"), "Подпись", "Стоп")
        self.assertIn("Кафе", prompt)
        self.assertIn(settings.OUTREACH_SENDER_NAME, prompt)
        self.assertIn("2–3 варианта", prompt)
        self.assertIn("Яндекс.Картам", prompt)
        self.assertIn("комплимента", prompt)
        self.assertIn("Подпись", prompt)
        self.assertIn("Стоп", prompt)


if __name__ == "__main__":
    unittest.main()
