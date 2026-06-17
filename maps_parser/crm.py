from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Iterable

from .models import Lead, utc_now_iso
from .storage import ensure_parent, lead_identity_key


CRM_COLUMNS = [
    "batch_id",
    "item_no",
    "key",
    "status",
    "manager",
    "comment",
    "assigned_at",
    "contacted_at",
    "name",
    "phone",
    "email",
    "social_links",
    "address",
    "categories",
    "rating",
    "reviews",
    "hours",
    "yandex_url",
]

ALLOWED_STATUSES = {
    "new": "новая",
    "assigned": "передана",
    "contacted": "связались",
    "declined": "отказ",
    "interested": "заинтересована",
    "followup": "повторный контакт",
}


@dataclass(slots=True)
class BatchExport:
    batch_id: str
    csv_path: Path
    xlsx_path: Path | None
    rows: list[dict[str, str]]


def normalize_status(value: str) -> str:
    raw = value.strip().casefold()
    aliases = {
        "новая": "new",
        "передана": "assigned",
        "связались": "contacted",
        "отказ": "declined",
        "заинтересована": "interested",
        "повторный": "followup",
        "повторный контакт": "followup",
    }
    status = aliases.get(raw, raw)
    if status not in ALLOWED_STATUSES:
        allowed = ", ".join(ALLOWED_STATUSES)
        raise ValueError(f"Неизвестный статус. Допустимо: {allowed}")
    return status


def create_batch_id(city: str = "") -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    slug = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "", city)[:12]
    return f"{stamp}-{slug}" if slug else stamp


def _lead_row(
    lead: Lead,
    *,
    batch_id: str,
    item_no: int,
    manager: str,
    fallback_category: str,
) -> dict[str, str]:
    assigned_at = utc_now_iso()
    return {
        "batch_id": batch_id,
        "item_no": str(item_no),
        "key": lead_identity_key(lead),
        "status": "assigned" if manager else "new",
        "manager": manager,
        "comment": "",
        "assigned_at": assigned_at,
        "contacted_at": "",
        "name": lead.name,
        "phone": lead.phone,
        "email": lead.email,
        "social_links": "; ".join(lead.social_links),
        "address": lead.address,
        "categories": "; ".join(lead.categories) or fallback_category,
        "rating": lead.rating,
        "reviews": lead.reviews,
        "hours": lead.hours,
        "yandex_url": lead.yandex_url,
    }


def _write_rows(path: Path, rows: Iterable[dict[str, str]], *, append: bool = False) -> None:
    ensure_parent(path)
    mode = "a" if append else "w"
    needs_header = not append or not path.exists() or path.stat().st_size == 0
    with path.open(mode, newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CRM_COLUMNS, extrasaction="ignore")
        if needs_header:
            writer.writeheader()
        writer.writerows(rows)


def _write_xlsx(path: Path, rows: list[dict[str, str]], *, city: str) -> Path | None:
    def column_name(index: int) -> str:
        out = ""
        while index:
            index, rem = divmod(index - 1, 26)
            out = chr(65 + rem) + out
        return out

    def cell_xml(ref: str, value: object, style: int = 0) -> str:
        if isinstance(value, (int, float)):
            return f'<c r="{ref}" s="{style}" t="n"><v>{value}</v></c>'
        text = escape(str(value or ""))
        return (
            f'<c r="{ref}" s="{style}" t="inlineStr">'
            f'<is><t xml:space="preserve">{text}</t></is></c>'
        )

    def sheet_xml(data: list[list[object]], widths: list[float], *, auto_filter: bool) -> str:
        max_col = max((len(row) for row in data), default=1)
        max_row = max(1, len(data))
        cols = "".join(
            f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
            for index, width in enumerate(widths, start=1)
        )
        row_xml: list[str] = []
        for row_index, values in enumerate(data, start=1):
            cells = "".join(
                cell_xml(
                    f"{column_name(column_index)}{row_index}",
                    value,
                    1 if row_index == 1 else (2 if row_index % 2 == 0 else 0),
                )
                for column_index, value in enumerate(values, start=1)
            )
            row_xml.append(f'<row r="{row_index}">{cells}</row>')
        filter_xml = (
            f'<autoFilter ref="A1:{column_name(max_col)}{max_row}"/>'
            if auto_filter and max_row > 1
            else ""
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>'
            f'<cols>{cols}</cols><sheetData>{"".join(row_xml)}</sheetData>{filter_xml}'
            '<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
            '</worksheet>'
        )

    summary_rows: list[list[object]] = [
        ["Показатель", "Значение"],
        ["Город", city],
        ["Всего лидов", len(rows)],
        *[
            [label.capitalize(), sum(row["status"] == status for row in rows)]
            for status, label in ALLOWED_STATUSES.items()
        ],
    ]
    lead_rows: list[list[object]] = [
        CRM_COLUMNS,
        *[[row.get(column, "") for column in CRM_COLUMNS] for row in rows],
    ]
    lead_widths = [
        25, 9, 42, 16, 18, 30, 22, 22, 30, 22, 24, 30, 32, 24, 12, 16, 20, 55
    ]
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0"><alignment vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="top" wrapText="1"/></xf>'
        '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )
    ensure_parent(path)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Сводка" sheetId="1" r:id="rId1"/>'
            '<sheet name="Лиды" sheetId="2" r:id="rId2"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        )
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml(summary_rows, [24, 24], auto_filter=False))
        archive.writestr("xl/worksheets/sheet2.xml", sheet_xml(lead_rows, lead_widths, auto_filter=True))
    return path


def create_manager_batch(
    output_dir: Path,
    items: list[tuple[Lead, str]],
    *,
    city: str,
    manager: str = "",
    batch_id: str = "",
) -> BatchExport:
    batch_id = batch_id or create_batch_id(city)
    rows = [
        _lead_row(
            lead,
            batch_id=batch_id,
            item_no=index,
            manager=manager.strip(),
            fallback_category=category,
        )
        for index, (lead, category) in enumerate(items, start=1)
    ]
    batch_dir = output_dir / "batches"
    csv_path = batch_dir / f"batch_{batch_id}.csv"
    xlsx_path = batch_dir / f"batch_{batch_id}.xlsx"
    _write_rows(csv_path, rows)
    _write_rows(output_dir / "crm_leads.csv", rows, append=True)
    written_xlsx = _write_xlsx(xlsx_path, rows, city=city)
    return BatchExport(batch_id=batch_id, csv_path=csv_path, xlsx_path=written_xlsx, rows=rows)


def update_crm_status(
    path: Path,
    *,
    batch_id: str,
    item_no: int,
    status: str,
    comment: str = "",
) -> dict[str, str]:
    normalized_status = normalize_status(status)
    if not path.exists():
        raise ValueError("CRM-реестр пока не создан.")
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
    updated: dict[str, str] | None = None
    for row in rows:
        if row.get("batch_id") == batch_id and int(row.get("item_no", 0) or 0) == item_no:
            row["status"] = normalized_status
            row["comment"] = comment.strip()
            if normalized_status in {"contacted", "declined", "interested", "followup"}:
                row["contacted_at"] = utc_now_iso()
            updated = row
            break
    if updated is None:
        raise ValueError(f"Лид {batch_id} №{item_no} не найден.")
    _write_rows(path, rows)
    return updated


def update_batch_status(
    output_dir: Path,
    *,
    batch_id: str,
    item_no: int,
    status: str,
    comment: str = "",
) -> dict[str, str]:
    updated = update_crm_status(
        output_dir / "crm_leads.csv",
        batch_id=batch_id,
        item_no=item_no,
        status=status,
        comment=comment,
    )
    batch_csv = output_dir / "batches" / f"batch_{batch_id}.csv"
    if batch_csv.exists():
        update_crm_status(
            batch_csv,
            batch_id=batch_id,
            item_no=item_no,
            status=status,
            comment=comment,
        )
        with batch_csv.open("r", newline="", encoding="utf-8-sig") as file:
            rows = list(csv.DictReader(file))
        _write_xlsx(output_dir / "batches" / f"batch_{batch_id}.xlsx", rows, city="")
    return updated
