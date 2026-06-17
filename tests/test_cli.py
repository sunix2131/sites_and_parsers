import unittest

from maps_parser.cli import default_argv


class CliTests(unittest.TestCase):
    def test_default_argv_starts_bot(self) -> None:
        self.assertEqual(default_argv([]), ["bot"])

    def test_default_argv_preserves_explicit_command(self) -> None:
        self.assertEqual(default_argv(["run", "--query", "кафе"]), ["run", "--query", "кафе"])


if __name__ == "__main__":
    unittest.main()
