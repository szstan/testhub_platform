"""Focused regressions for the parsed dataset serialization budget."""

from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from apps.app_automation.dataset_utils import DEFAULT_LIMITS, DatasetError, parse_dataset


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def make_shared_string_xlsx(row_count: int, value: str) -> bytes:
    """Create a tiny workbook whose data cells all reference one shared string."""
    worksheet_rows = []
    for row_number in range(1, row_count + 2):
        shared_index = 0 if row_number == 1 else 1
        worksheet_rows.append(
            f'<row r="{row_number}"><c r="A{row_number}" t="s">'
            f"<v>{shared_index}</v></c></row>"
        )
    worksheet = (
        f'<worksheet xmlns="{MAIN_NS}"><dimension ref="A1:A{row_count + 1}"/>'
        f"<sheetData>{''.join(worksheet_rows)}</sheetData></worksheet>"
    )
    shared_strings = (
        f'<sst xmlns="{MAIN_NS}" count="{row_count + 1}" uniqueCount="2">'
        '<si><t>payload</t></si>'
        f"<si><t>{value}</t></si></sst>"
    )
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )
    workbook = (
        f'<workbook xmlns="{MAIN_NS}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        "</Relationships>"
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
        archive.writestr("xl/sharedStrings.xml", shared_strings)
    return output.getvalue()


def make_unicode_xlsx(rows: list[list[str]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def test_shared_string_references_are_charged_to_the_decoded_json_budget() -> None:
    row_count = 48
    shared_value = "x" * 384
    content = make_shared_string_xlsx(row_count, shared_value)
    rows = [{"payload": shared_value} for _ in range(row_count)]
    budget = 8 * 1024

    assert len(content) < budget
    assert len(json.dumps(rows).encode("ascii")) > budget
    limits = replace(DEFAULT_LIMITS, max_serialized_bytes=budget)

    with pytest.raises(DatasetError, match="JSON serialization limit"):
        parse_dataset(content, "expanded.xlsx", limits=limits)


def test_normal_unicode_csv_and_xlsx_fit_exact_json_budget() -> None:
    expected = [
        {"name": "张三", "note": "café ☕"},
        {"name": "Zoë", "note": "多行\n値"},
    ]
    csv_content = 'name,note\n张三,café ☕\nZoë,"多行\n値"\n'.encode("utf-8")
    xlsx_content = make_unicode_xlsx([
        ["name", "note"],
        ["张三", "café ☕"],
        ["Zoë", "多行\n値"],
    ])
    exact_size = len(json.dumps(expected).encode("ascii"))

    for filename, content in (("normal.csv", csv_content), ("normal.xlsx", xlsx_content)):
        assert parse_dataset(
            content,
            filename,
            limits=replace(DEFAULT_LIMITS, max_serialized_bytes=exact_size),
        ) == expected
        with pytest.raises(DatasetError, match="JSON serialization limit"):
            parse_dataset(
                content,
                filename,
                limits=replace(DEFAULT_LIMITS, max_serialized_bytes=exact_size - 1),
            )
