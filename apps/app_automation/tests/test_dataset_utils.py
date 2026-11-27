"""Unit tests for bounded APP automation dataset helpers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from apps.app_automation.dataset_utils import (
    DEFAULT_LIMITS,
    DatasetError,
    DatasetLimits,
    bind_row_variables,
    parse_dataset,
    select_dataset_row,
)


def make_xlsx(rows: list[list[object]], *, sheet_name: str = "Data") -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name
    for row in rows:
        worksheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def test_parse_csv_utf8_bom_preserves_order_and_empty_values() -> None:
    rows = parse_dataset(
        "\ufeffname,count,enabled,empty\nAlice,0,false,\n".encode("utf-8"),
        "data.CSV",
    )

    assert rows == [{"name": "Alice", "count": "0", "enabled": "false", "empty": ""}]
    assert list(rows[0]) == ["name", "count", "enabled", "empty"]


def test_csv_preflight_respects_quoted_delimiters_and_newlines() -> None:
    rows = parse_dataset(b'name,note\nalice,"x,y\nz"\n', "data.csv")

    assert rows == [{"name": "alice", "note": "x,y\nz"}]


def test_parse_csv_rejects_invalid_encoding_duplicate_headers_and_bad_width() -> None:
    with pytest.raises(DatasetError, match="UTF-8"):
        parse_dataset(b"name\n\xff", "data.csv")
    with pytest.raises(DatasetError, match="duplicate header"):
        parse_dataset(b"Name,name\na,b\n", "data.csv")
    with pytest.raises(DatasetError, match="expected 2"):
        parse_dataset(b"name,count\nAlice\n", "data.csv")


def test_parse_csv_rejects_malformed_quotes_and_unsupported_format() -> None:
    with pytest.raises(DatasetError, match="malformed CSV"):
        parse_dataset(b'name\n"unterminated', "data.csv")
    with pytest.raises(DatasetError, match="unsupported dataset format"):
        parse_dataset(b"name\nAlice\n", "data.json")


def test_parse_csv_enforces_byte_row_column_and_cell_limits() -> None:
    limits = replace(DEFAULT_LIMITS, max_bytes=32, max_rows=1, max_columns=2, max_cell_chars=4)
    with pytest.raises(DatasetError, match="byte limit"):
        parse_dataset(b"name\n" + b"x" * 40, "data.csv", limits=limits)
    with pytest.raises(DatasetError, match="row limit"):
        parse_dataset(b"name\na\nb\n", "data.csv", limits=limits)
    with pytest.raises(DatasetError, match="column limit"):
        parse_dataset(b"a,b,c\n1,2,3\n", "data.csv", limits=limits)
    with pytest.raises(DatasetError, match="column limit"):
        parse_dataset(
            b"name\n" + b"," * 1_000 + b"\n",
            "data.csv",
            limits=replace(limits, max_bytes=2_000),
        )
    with pytest.raises(DatasetError, match="character cell limit"):
        parse_dataset(b"name\n12345\n", "data.csv", limits=limits)
    with pytest.raises(DatasetError, match="row limit"):
        parse_dataset(b"a\n\nb\n", "data.csv", limits=limits)


def test_csv_large_field_has_a_bounded_actionable_error() -> None:
    content = b"value\n" + b"x" * 140_000 + b"\n"
    with pytest.raises(DatasetError, match="16384-character cell limit"):
        parse_dataset(content, "data.csv")


def test_parse_xlsx_returns_ordered_rows_and_converts_dates_to_iso() -> None:
    workbook_bytes = make_xlsx([
        ["name", "count", "active", "when"],
        ["Alice", 0, False, datetime(2025, 1, 2, 3, 4, 5)],
        ["Bob", 3, True, None],
    ])

    rows = parse_dataset(workbook_bytes, "data.xlsx", sheet_name="Data")

    assert rows == [
        {"name": "Alice", "count": 0, "active": False, "when": "2025-01-02T03:04:05"},
        {"name": "Bob", "count": 3, "active": True, "when": None},
    ]
    assert list(rows[0]) == ["name", "count", "active", "when"]


def test_parse_xlsx_rejects_formulas_duplicate_headers_and_bad_rows() -> None:
    with pytest.raises(DatasetError, match="formula cell"):
        parse_dataset(make_xlsx([["value"], ["=1+1"]]), "data.xlsx")
    with pytest.raises(DatasetError, match="duplicate header"):
        parse_dataset(make_xlsx([["Name", "name"], ["a", "b"]]), "data.xlsx")
    with pytest.raises(DatasetError, match="expected at most 2"):
        parse_dataset(make_xlsx([["a", "b"], ["one", "two", "extra"]]), "data.xlsx")


def test_parse_xlsx_rejects_underreported_dimensions() -> None:
    source = make_xlsx([["value"], ["outside-dimension"]])
    rewritten = BytesIO()
    with ZipFile(BytesIO(source), "r") as source_zip, ZipFile(rewritten, "w", ZIP_DEFLATED) as output_zip:
        for entry in source_zip.infolist():
            content = source_zip.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                content = content.replace(b'ref="A1:A2"', b'ref="A1:A1"')
            output_zip.writestr(entry.filename, content)

    with pytest.raises(DatasetError, match="dimensions do not include"):
        parse_dataset(rewritten.getvalue(), "data.xlsx")


def test_parse_xlsx_enforces_dimensions_and_rejects_non_workbooks() -> None:
    limits = replace(DEFAULT_LIMITS, max_rows=1, max_columns=2)
    with pytest.raises(DatasetError, match="data-row limit"):
        parse_dataset(make_xlsx([["a"], ["1"], ["2"]]), "data.xlsx", limits=limits)
    with pytest.raises(DatasetError, match="column limit"):
        parse_dataset(make_xlsx([["a", "b", "c"]]), "data.xlsx", limits=limits)
    with pytest.raises(DatasetError, match="archive is malformed"):
        parse_dataset(b"not a zip archive", "data.xlsx")


def test_dataset_limits_cannot_be_configured_above_hard_caps() -> None:
    with pytest.raises(ValueError, match="max_rows"):
        DatasetLimits(max_rows=2_001)


def test_select_dataset_row_is_stable_and_validates_bounds() -> None:
    rows = [{"id": "first"}, {"id": "second"}, {"id": "third"}]

    assert select_dataset_row(rows, index=1) == rows[1]
    assert select_dataset_row(rows, key="execution-123") == select_dataset_row(rows, key="execution-123")
    assert select_dataset_row(rows, key=b"execution-123") == select_dataset_row(rows, key="execution-123")
    with pytest.raises(DatasetError, match="empty dataset"):
        select_dataset_row([])
    with pytest.raises(DatasetError, match="outside the dataset"):
        select_dataset_row(rows, index=3)
    with pytest.raises(DatasetError, match="non-negative integer"):
        select_dataset_row(rows, index=True)
    with pytest.raises(DatasetError, match="either index or key"):
        select_dataset_row(rows, index=2, key="execution-123")


def test_bind_row_variables_overlays_existing_and_appends_local_variables() -> None:
    variables = [
        {"name": "username", "value": "old", "scope": "global", "description": "account"},
        {"name": "retry", "value": 5, "scope": "local"},
    ]

    bound = bind_row_variables(
        {"username": "", "retry": 0, "enabled": False, "optional": None},
        variables,
    )

    assert bound == [
        {"name": "username", "value": "", "scope": "global", "description": "account"},
        {"name": "retry", "value": 0, "scope": "local"},
        {"name": "enabled", "value": False, "scope": "local"},
        {"name": "optional", "value": None, "scope": "local"},
    ]


def test_bind_secret_references_only_through_allowlisted_resolver() -> None:
    calls: list[str] = []
    synthetic_secret = "synthetic-secret-value"

    def resolve(alias: str) -> str:
        calls.append(alias)
        return synthetic_secret

    bound = bind_row_variables(
        {"password": "secret://qa.login_password", "literal": "${ENV:HOME}"},
        [],
        secret_aliases={"qa.login_password"},
        secret_resolver=resolve,
    )

    assert bound == [
        {"name": "password", "value": synthetic_secret, "scope": "local"},
        {"name": "literal", "value": "${ENV:HOME}", "scope": "local"},
    ]
    assert calls == ["qa.login_password"]


def test_bind_rejects_unknown_malformed_and_unconfigured_secret_references() -> None:
    calls: list[str] = []
    resolver = lambda alias: calls.append(alias) or "synthetic-secret"

    with pytest.raises(DatasetError, match="not in the caller-supplied allowlist"):
        bind_row_variables(
            {"password": "secret://unknown"}, [],
            secret_aliases={"known"}, secret_resolver=resolver,
        )
    with pytest.raises(DatasetError, match="malformed secret reference"):
        bind_row_variables(
            {"password": "secret://bad/alias"}, [],
            secret_aliases={"known"}, secret_resolver=resolver,
        )
    with pytest.raises(DatasetError, match="must be supplied together"):
        bind_row_variables({"password": "secret://known"}, [], secret_aliases={"known"})
    assert calls == []


def test_bind_does_not_expose_resolver_exception_or_accept_nested_values() -> None:
    secret_marker = "resolver-private-diagnostic"

    def broken_resolver(alias: str) -> str:
        raise RuntimeError(secret_marker)

    with pytest.raises(DatasetError) as error:
        bind_row_variables(
            {"password": "secret://known"}, [],
            secret_aliases={"known"}, secret_resolver=broken_resolver,
        )
    assert secret_marker not in str(error.value)

    with pytest.raises(DatasetError, match="nested or unsupported"):
        bind_row_variables({"payload": {"nested": "value"}}, [],)
    with pytest.raises(DatasetError, match="duplicate variable"):
        bind_row_variables({}, [{"name": "id", "value": 1}, {"name": "id", "value": 2}])
