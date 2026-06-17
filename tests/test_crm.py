import csv
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from maps_parser.crm import create_manager_batch, update_batch_status
from maps_parser.models import Lead


class CrmTests(unittest.TestCase):
    def test_create_batch_and_update_status(self) -> None:
        lead = Lead(
            name="Кафе Тест",
            phone="+7 900 000-00-00",
            address="Тестовая, 1",
            yandex_url="https://yandex.ru/maps/org/test/1/",
        )
        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            export = create_manager_batch(
                output_dir,
                [(lead, "Кафе")],
                city="Пятигорск",
                manager="Иван",
                batch_id="batch-test",
            )

            self.assertTrue(export.csv_path.exists())
            self.assertIsNotNone(export.xlsx_path)
            assert export.xlsx_path is not None
            self.assertTrue(export.xlsx_path.exists())
            with zipfile.ZipFile(export.xlsx_path) as archive:
                self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
                self.assertIn("xl/worksheets/sheet2.xml", archive.namelist())
            self.assertTrue((output_dir / "crm_leads.csv").exists())
            self.assertEqual(export.rows[0]["manager"], "Иван")
            self.assertEqual(export.rows[0]["status"], "assigned")

            updated = update_batch_status(
                output_dir,
                batch_id="batch-test",
                item_no=1,
                status="interested",
                comment="Перезвонить завтра",
            )
            self.assertEqual(updated["status"], "interested")

            with (output_dir / "crm_leads.csv").open(
                "r", newline="", encoding="utf-8-sig"
            ) as file:
                row = next(csv.DictReader(file))
            self.assertEqual(row["comment"], "Перезвонить завтра")
            self.assertTrue(row["contacted_at"])


if __name__ == "__main__":
    unittest.main()
