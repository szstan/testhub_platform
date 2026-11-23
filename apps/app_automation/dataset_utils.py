"""Bounded, side-effect-free data helpers for APP automation tests.

The parser accepts UTF-8 (with optional BOM) CSV and XLSX bytes. It never
reads files, environment variables, or network resources. Secret references
are resolved only by :func:`bind_row_variables` through an injected resolver.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import posixpath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any


class DatasetError(ValueError):
    """Raised when a dataset or binding request violates the safe input contract."""


@dataclass(frozen=True)
class DatasetLimits:
    """Configurable limits that may be tightened but never raised above hard caps."""

    max_bytes: int = 5 * 1024 * 1024
    max_rows: int = 2_000
    max_columns: int = 100
    max_cell_chars: int = 16_384
    max_uncompressed_bytes: int = 20 * 1024 * 1024
    max_zip_entries: int = 256
    max_compression_ratio: int = 1_000

    def __post_init__(self) -> None:
        caps = {
            "max_bytes": 5 * 1024 * 1024,
            "max_rows": 2_000,
            "max_columns": 100,
            "max_cell_chars": 16_384,
            "max_uncompressed_bytes": 20 * 1024 * 1024,
            "max_zip_entries": 256,
            "max_compression_ratio": 1_000,
        }
        for field, cap in caps.items():
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= cap:
                raise ValueError(f"{field} must be an integer between 1 and {cap}.")


DEFAULT_LIMITS = DatasetLimits()
_SECRET_REFERENCE = re.compile(r"secret://([A-Za-z0-9][A-Za-z0-9_.-]{0,127})\Z")
_ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def parse_dataset(
    data: bytes,
    filename: str,
    *,
    limits: DatasetLimits = DEFAULT_LIMITS,
    sheet_name: str | None = None,
) -> list[dict[str, Any]]:
    """Parse CSV or XLSX bytes into ordered row dictionaries.

    CSV fields are strings; XLSX scalar cells retain their scalar type except
    dates/times, which become ISO strings. The first worksheet is used unless
    ``sheet_name`` is provided. Blank XLSX rows are skipped.
    """
    if not isinstance(limits, DatasetLimits):
        raise DatasetError("limits must be a DatasetLimits instance.")
    if not isinstance(data, bytes):
        raise DatasetError("dataset content must be supplied as bytes.")
    if len(data) > limits.max_bytes:
        raise DatasetError(f"dataset exceeds the {limits.max_bytes}-byte limit.")
    if not isinstance(filename, str):
        raise DatasetError("filename must end in .csv or .xlsx.")

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension == "csv":
        return _parse_csv(data, limits)
    if extension == "xlsx":
        return _parse_xlsx(data, limits, sheet_name)
    raise DatasetError("unsupported dataset format; use a .csv or .xlsx file.")


def select_dataset_row(
    rows: Sequence[Mapping[str, Any]],
    *,
    index: int | None = None,
    key: str | bytes | None = None,
) -> dict[str, Any]:
    """Select a row by zero-based index, or deterministically by a stable key.

    Key-based selection maps SHA-256(key) to a row index. It is independent of
    process randomness and does not depend on Python's salted ``hash()``. With
    neither argument, the first row is selected.
    """
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise DatasetError("rows must be a non-empty sequence of row mappings.")
    if not rows:
        raise DatasetError("cannot select a row from an empty dataset.")
    if len(rows) > DEFAULT_LIMITS.max_rows:
        raise DatasetError(f"rows exceeds the {DEFAULT_LIMITS.max_rows}-row limit.")
    if key is not None and index is not None:
        raise DatasetError("provide either index or key, not both.")

    if key is None:
        selected_index = 0 if index is None else index
        if isinstance(selected_index, bool) or not isinstance(selected_index, int) or selected_index < 0:
            raise DatasetError("row index must be a non-negative integer.")
    else:
        if isinstance(key, str):
            encoded_key = key.encode("utf-8")
        elif isinstance(key, bytes):
            encoded_key = key
        else:
            raise DatasetError("selection key must be text or bytes.")
        if len(encoded_key) > 1_024:
            raise DatasetError("selection key exceeds the 1024-byte limit.")
        selected_index = int.from_bytes(hashlib.sha256(encoded_key).digest(), "big") % len(rows)

    if selected_index >= len(rows):
        raise DatasetError(f"row index {selected_index} is outside the dataset (0-{len(rows) - 1}).")
    row = rows[selected_index]
    if not isinstance(row, Mapping):
        raise DatasetError(f"row {selected_index} is not a mapping.")
    if len(row) > DEFAULT_LIMITS.max_columns:
        raise DatasetError(f"row {selected_index} exceeds the {DEFAULT_LIMITS.max_columns}-column limit.")
    for name, value in row.items():
        _validate_name(name, "column name")
        _validate_scalar(value, name, DEFAULT_LIMITS.max_cell_chars)
    return dict(row)


def bind_row_variables(
    row: Mapping[str, Any],
    variables: Sequence[Mapping[str, Any]],
    *,
    secret_aliases: Set[str] | Sequence[str] | None = None,
    secret_resolver: Callable[[str], Any] | None = None,
    max_cell_chars: int = DEFAULT_LIMITS.max_cell_chars,
) -> list[dict[str, Any]]:
    """Overlay a row onto an APP runner ``variables`` list.

    Existing variable definitions retain their order and metadata; matching
    row columns replace their values, and remaining columns are appended as
    local variables. Values are not truthiness-filtered, so ``0``, ``False``,
    ``""`` and ``None`` are preserved. Exact ``secret://alias`` values require
    both an explicit alias allowlist and a caller-owned resolver. No environment
    lookup, logging, or serialization is performed.
    """
    _validate_cell_limit(max_cell_chars)
    if not isinstance(row, Mapping):
        raise DatasetError("row must be a mapping of column names to scalar values.")
    if not isinstance(variables, Sequence) or isinstance(variables, (str, bytes, bytearray)):
        raise DatasetError("variables must be a sequence of variable mappings.")
    if len(variables) > DEFAULT_LIMITS.max_rows:
        raise DatasetError(f"variables exceeds the {DEFAULT_LIMITS.max_rows}-item limit.")
    if len(row) > DEFAULT_LIMITS.max_columns:
        raise DatasetError(f"row exceeds the {DEFAULT_LIMITS.max_columns}-column limit.")

    allowed_aliases = _validate_secret_configuration(secret_aliases, secret_resolver)
    clean_row: dict[str, Any] = {}
    for name, value in row.items():
        _validate_name(name, "column name")
        _validate_scalar(value, name, max_cell_chars)
        clean_row[name] = value

    result: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    bound_names: set[str] = set()
    for position, item in enumerate(variables):
        if not isinstance(item, Mapping):
            raise DatasetError(f"variable at position {position} must be a mapping.")
        if len(item) > 32:
            raise DatasetError(f"variable at position {position} exceeds the 32-field metadata limit.")
        name = item.get("name")
        _validate_name(name, f"variable name at position {position}")
        if name in seen_names:
            raise DatasetError(f"duplicate variable name {name!r}.")
        seen_names.add(name)

        output = dict(item)
        value = clean_row[name] if name in clean_row else item.get("value")
        _validate_variable_metadata(output, name, max_cell_chars)
        output["value"] = _resolve_secret(value, allowed_aliases, secret_resolver, name, max_cell_chars)
        result.append(output)
        if name in clean_row:
            bound_names.add(name)

    for name, value in clean_row.items():
        if name not in bound_names and name not in seen_names:
            result.append({
                "name": name,
                "value": _resolve_secret(value, allowed_aliases, secret_resolver, name, max_cell_chars),
                "scope": "local",
            })
    return result


def _parse_csv(data: bytes, limits: DatasetLimits) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise DatasetError("CSV must be UTF-8 or UTF-8 with BOM encoding.") from None
    _preflight_csv(text, limits)

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        headers = next(reader)
        while not headers:
            headers = next(reader)
    except StopIteration:
        raise DatasetError("CSV is empty; include a header row.") from None
    except csv.Error as exc:
        _raise_csv_error(reader, limits, exc)

    headers = _validate_headers(headers, limits)
    rows: list[dict[str, Any]] = []
    data_record_count = 0
    try:
        for values in reader:
            data_record_count += 1
            if data_record_count > limits.max_rows:
                raise DatasetError(f"CSV exceeds the {limits.max_rows}-row limit.")
            if not values:
                continue
            if len(values) > limits.max_columns:
                raise DatasetError(f"CSV row exceeds the {limits.max_columns}-column limit.")
            if len(values) != len(headers):
                raise DatasetError(
                    f"CSV row ending at physical line {reader.line_num} has {len(values)} fields; "
                    f"expected {len(headers)}."
                )
            for column, value in zip(headers, values):
                _validate_scalar(value, column, limits.max_cell_chars)
            rows.append(dict(zip(headers, values)))
    except csv.Error as exc:
        _raise_csv_error(reader, limits, exc)
    return rows


def _preflight_csv(text: str, limits: DatasetLimits) -> None:
    in_quotes = False
    at_field_start = True
    field_count = 1
    field_length = 0
    record_count = 0
    position = 0

    def finish_record() -> None:
        nonlocal record_count, field_count, field_length, at_field_start
        record_count += 1
        if record_count > limits.max_rows + 1:
            raise DatasetError(f"CSV exceeds the {limits.max_rows}-row limit.")
        field_count = 1
        field_length = 0
        at_field_start = True

    while position < len(text):
        char = text[position]
        if in_quotes:
            if char == '"':
                if position + 1 < len(text) and text[position + 1] == '"':
                    field_length += 1
                    position += 1
                else:
                    in_quotes = False
            else:
                field_length += 1
        elif char == ",":
            field_count += 1
            if field_count > limits.max_columns:
                raise DatasetError(f"CSV row exceeds the {limits.max_columns}-column limit.")
            field_length = 0
            at_field_start = True
        elif char == '"' and at_field_start:
            in_quotes = True
            at_field_start = False
        elif char in "\r\n":
            finish_record()
            if char == "\r" and position + 1 < len(text) and text[position + 1] == "\n":
                position += 1
        else:
            field_length += 1
            at_field_start = False

        if field_length > limits.max_cell_chars:
            raise DatasetError(f"CSV cell exceeds the {limits.max_cell_chars}-character cell limit.")
        position += 1

    if field_count > 1 or field_length or not at_field_start or in_quotes:
        finish_record()


def _raise_csv_error(reader: Any, limits: DatasetLimits, error: csv.Error) -> None:
    if "field larger than field limit" in str(error).casefold():
        raise DatasetError(
            f"CSV field exceeds parser capacity; each cell is limited to {limits.max_cell_chars} characters."
        ) from None
    raise DatasetError(f"malformed CSV near physical line {reader.line_num}.") from None


def _parse_xlsx(data: bytes, limits: DatasetLimits, sheet_name: str | None) -> list[dict[str, Any]]:
    _validate_xlsx_archive(data, limits)
    try:
        from openpyxl import load_workbook
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise DatasetError("XLSX support requires the openpyxl package.") from None

    workbook = None
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
        if sheet_name is None:
            if not workbook.worksheets:
                raise DatasetError("XLSX workbook contains no worksheets.")
            worksheet = workbook.worksheets[0]
        else:
            if not isinstance(sheet_name, str) or not sheet_name or len(sheet_name) > 31:
                raise DatasetError("sheet_name must be a non-empty worksheet name of at most 31 characters.")
            if sheet_name not in workbook.sheetnames:
                raise DatasetError(f"worksheet {sheet_name!r} was not found in the XLSX workbook.")
            worksheet = workbook[sheet_name]

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            worksheet_part = _worksheet_part_path(archive, worksheet.title)
            actual_max_row, actual_max_column = _validate_worksheet_xml(archive, worksheet_part, limits)

        max_row = worksheet.max_row
        max_column = worksheet.max_column
        if max_row is None or max_column is None or max_row < 1 or max_column < 1:
            raise DatasetError("XLSX worksheet is empty; include a header row.")
        if actual_max_row > max_row or actual_max_column > max_column:
            raise DatasetError("XLSX worksheet dimensions do not include all populated cells.")
        if max_row > limits.max_rows + 1:
            raise DatasetError(f"XLSX worksheet exceeds the {limits.max_rows}-data-row limit.")
        if max_column > limits.max_columns:
            raise DatasetError(f"XLSX worksheet exceeds the {limits.max_columns}-column limit.")

        iterator = worksheet.iter_rows(min_row=1, max_row=max_row, max_col=max_column)
        try:
            header_cells = next(iterator)
        except StopIteration:
            raise DatasetError("XLSX worksheet is empty; include a header row.") from None
        header_values = [
            _xlsx_value(cell, limits, f"{get_column_letter(column)}1")
            for column, cell in enumerate(header_cells, start=1)
        ]
        headers = _validate_headers(_trim_trailing_none(header_values), limits)

        rows: list[dict[str, Any]] = []
        for row_number, cells in enumerate(iterator, start=2):
            values = [
                _xlsx_value(cell, limits, f"{get_column_letter(column)}{row_number}")
                for column, cell in enumerate(cells, start=1)
            ]
            values = _trim_trailing_none(values)
            if not values or all(value is None for value in values):
                continue
            if len(rows) >= limits.max_rows:
                raise DatasetError(f"XLSX worksheet exceeds the {limits.max_rows}-data-row limit.")
            if len(values) > len(headers):
                raise DatasetError(
                    f"XLSX row {row_number} has {len(values)} populated columns; expected at most {len(headers)}."
                )
            values.extend([None] * (len(headers) - len(values)))
            rows.append(dict(zip(headers, values)))
        return rows
    except DatasetError:
        raise
    except Exception:
        raise DatasetError("XLSX workbook is malformed or could not be read safely.") from None
    finally:
        if workbook is not None:
            workbook.close()


def _worksheet_part_path(archive: zipfile.ZipFile, worksheet_name: str) -> str:
    workbook_root = _read_xml_part(archive, "xl/workbook.xml")
    relationships_root = _read_xml_part(archive, "xl/_rels/workbook.xml.rels")
    relationship_id = None
    for element in workbook_root.iter():
        if _xml_local_name(element.tag) == "sheet" and element.attrib.get("name") == worksheet_name:
            relationship_id = element.attrib.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            break
    if not relationship_id:
        raise DatasetError("XLSX worksheet relationship is missing.")

    target = None
    relationship_type = None
    for element in relationships_root.iter():
        if _xml_local_name(element.tag) == "Relationship" and element.attrib.get("Id") == relationship_id:
            target = element.attrib.get("Target")
            relationship_type = element.attrib.get("Type", "")
            if element.attrib.get("TargetMode") == "External":
                target = None
            break
    if not target or not relationship_type.endswith("/worksheet"):
        raise DatasetError("XLSX worksheet relationship is invalid.")

    path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
    if not path.startswith("xl/") or path.startswith("../") or path not in archive.namelist():
        raise DatasetError("XLSX worksheet part is missing or has an unsafe path.")
    return path


def _validate_worksheet_xml(
    archive: zipfile.ZipFile,
    part_path: str,
    limits: DatasetLimits,
) -> tuple[int, int]:
    content = _read_xml_bytes(archive, part_path)
    current_row = 0
    previous_row = 0
    current_column = 0
    actual_max_row = 0
    actual_max_column = 0

    try:
        for event, element in ET.iterparse(io.BytesIO(content), events=("start", "end")):
            if event == "start":
                local_name = _xml_local_name(element.tag)
                if local_name == "row":
                    raw_row = element.attrib.get("r")
                    if raw_row is None:
                        current_row = previous_row + 1
                    else:
                        if not raw_row.isdecimal() or len(raw_row) > len(str(limits.max_rows + 1)):
                            raise DatasetError("XLSX worksheet contains an invalid row reference.")
                        current_row = int(raw_row)
                    if current_row <= previous_row:
                        raise DatasetError("XLSX worksheet row references are not strictly increasing.")
                    if current_row > limits.max_rows + 1:
                        raise DatasetError(f"XLSX worksheet exceeds the {limits.max_rows}-data-row limit.")
                    previous_row = current_row
                    current_column = 0
                    actual_max_row = current_row
                elif local_name == "c":
                    if current_row == 0:
                        raise DatasetError("XLSX worksheet contains a cell outside a row.")
                    reference = element.attrib.get("r")
                    if reference is None:
                        column = current_column + 1
                    else:
                        match = re.fullmatch(r"([A-Za-z]{1,3})([1-9][0-9]*)", reference)
                        if match is None or len(match.group(2)) > len(str(limits.max_rows + 1)):
                            raise DatasetError("XLSX worksheet contains an invalid cell reference.")
                        referenced_row = int(match.group(2))
                        if referenced_row != current_row:
                            raise DatasetError("XLSX cell reference does not match its worksheet row.")
                        column = 0
                        for char in match.group(1).upper():
                            column = column * 26 + ord(char) - ord("A") + 1
                    if column <= current_column:
                        raise DatasetError("XLSX worksheet cell references are not strictly increasing.")
                    if column > limits.max_columns:
                        raise DatasetError(f"XLSX worksheet exceeds the {limits.max_columns}-column limit.")
                    current_column = column
                    actual_max_column = max(actual_max_column, column)
            else:
                element.clear()
    except DatasetError:
        raise
    except Exception:
        raise DatasetError("XLSX worksheet XML is malformed or unreadable.") from None
    return actual_max_row, actual_max_column


def _read_xml_part(archive: zipfile.ZipFile, part_path: str) -> ET.Element:
    content = _read_xml_bytes(archive, part_path)
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        raise DatasetError("XLSX workbook metadata is malformed or unreadable.") from None


def _read_xml_bytes(archive: zipfile.ZipFile, part_path: str) -> bytes:
    try:
        content = archive.read(part_path)
    except Exception:
        raise DatasetError("XLSX workbook metadata is missing or unreadable.") from None
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", content, re.IGNORECASE):
        raise DatasetError("XLSX XML must not contain document type or entity declarations.")
    return content


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _validate_xlsx_archive(data: bytes, limits: DatasetLimits) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        raise DatasetError("XLSX archive is malformed or unreadable.") from None

    with archive:
        entries = archive.infolist()
        if len(entries) > limits.max_zip_entries:
            raise DatasetError(f"XLSX archive exceeds the {limits.max_zip_entries}-entry limit.")
        total_uncompressed = 0
        names: set[str] = set()
        for entry in entries:
            name = entry.filename
            if name in names:
                raise DatasetError("XLSX archive contains duplicate entry names.")
            names.add(name)
            if entry.flag_bits & 0x1:
                raise DatasetError("encrypted XLSX archives are not supported.")
            if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                raise DatasetError("XLSX archive contains an unsafe entry path.")
            total_uncompressed += entry.file_size
            if total_uncompressed > limits.max_uncompressed_bytes:
                raise DatasetError(
                    f"XLSX archive expands beyond the {limits.max_uncompressed_bytes}-byte limit."
                )
            if entry.file_size and not entry.compress_size:
                raise DatasetError("XLSX archive contains an invalid compressed entry.")
            if entry.compress_size and entry.file_size / entry.compress_size > limits.max_compression_ratio:
                raise DatasetError(
                    f"XLSX archive entry exceeds the {limits.max_compression_ratio}:1 compression-ratio limit."
                )
        if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
            raise DatasetError("file is not a valid XLSX workbook.")
        for entry in entries:
            if entry.filename.lower().endswith((".xml", ".rels")):
                try:
                    content = archive.read(entry)
                except Exception:
                    raise DatasetError("XLSX archive contains a corrupt XML part.") from None
                if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", content, re.IGNORECASE):
                    raise DatasetError("XLSX XML must not contain document type or entity declarations.")


def _validate_headers(values: Sequence[Any], limits: DatasetLimits) -> list[str]:
    while values and values[-1] is None:
        values = values[:-1]
    if not values:
        raise DatasetError("dataset header row is empty.")
    if len(values) > limits.max_columns:
        raise DatasetError(f"dataset exceeds the {limits.max_columns}-column limit.")

    headers: list[str] = []
    seen: set[str] = set()
    for position, value in enumerate(values, start=1):
        if not isinstance(value, str):
            raise DatasetError(f"header at column {position} must be text.")
        header = value.strip()
        _validate_scalar(header, f"header at column {position}", limits.max_cell_chars)
        if not header:
            raise DatasetError(f"header at column {position} is empty.")
        if any(unicodedata.category(char) == "Cc" for char in header):
            raise DatasetError(f"header at column {position} contains a control character.")
        normalized = unicodedata.normalize("NFKC", header).casefold()
        if normalized in seen:
            raise DatasetError(f"duplicate header {header!r}; headers must be unique ignoring case and spacing.")
        seen.add(normalized)
        headers.append(header)
    return headers


def _xlsx_value(cell: Any, limits: DatasetLimits, coordinate: str) -> Any:
    if cell.data_type == "f":
        raise DatasetError(f"formula cell {coordinate} is not allowed; replace it with a literal value.")
    if cell.data_type == "e":
        raise DatasetError(f"error cell {coordinate} is not allowed; correct the source workbook.")

    value = cell.value
    if isinstance(value, (datetime, date, time)):
        value = value.isoformat()
    elif isinstance(value, timedelta):
        value = str(value)
    _validate_scalar(value, coordinate, limits.max_cell_chars)
    return value


def _trim_trailing_none(values: list[Any]) -> list[Any]:
    while values and values[-1] is None:
        values.pop()
    return values


def _validate_scalar(value: Any, label: str, max_chars: int) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > max_chars:
            raise DatasetError(f"{label} exceeds the {max_chars}-character cell limit.")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DatasetError(f"{label} must be a finite number.")
        return
    raise DatasetError(f"{label} must be a scalar value; nested or unsupported values are not allowed.")


def _validate_name(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise DatasetError(f"{label} must be non-empty text of at most 128 characters.")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise DatasetError(f"{label} contains a control character.")


def _validate_variable_metadata(item: Mapping[str, Any], name: str, max_chars: int) -> None:
    for field, value in item.items():
        if not isinstance(field, str) or not field or len(field) > 64:
            raise DatasetError(f"variable {name!r} contains an invalid metadata field name.")
        if field == "value":
            _validate_scalar(value, f"variable {name!r}", max_chars)
        elif isinstance(value, (dict, list, tuple, set, frozenset)):
            raise DatasetError(f"variable {name!r} contains nested metadata in field {field!r}.")
        elif value is not None:
            _validate_scalar(value, f"variable {name!r} field {field!r}", max_chars)


def _validate_cell_limit(max_cell_chars: int) -> None:
    if isinstance(max_cell_chars, bool) or not isinstance(max_cell_chars, int):
        raise DatasetError("max_cell_chars must be an integer.")
    if not 1 <= max_cell_chars <= DEFAULT_LIMITS.max_cell_chars:
        raise DatasetError(f"max_cell_chars must be between 1 and {DEFAULT_LIMITS.max_cell_chars}.")


def _validate_secret_configuration(
    secret_aliases: Set[str] | Sequence[str] | None,
    secret_resolver: Callable[[str], Any] | None,
) -> frozenset[str]:
    if (secret_aliases is None) != (secret_resolver is None):
        raise DatasetError("secret_aliases and secret_resolver must be supplied together.")
    if secret_aliases is None:
        return frozenset()
    if isinstance(secret_aliases, (str, bytes, bytearray)) or not isinstance(secret_aliases, (Set, Sequence)):
        raise DatasetError("secret_aliases must be a set or sequence of server-owned aliases.")
    if len(secret_aliases) > 256:
        raise DatasetError("secret_aliases exceeds the 256-alias limit.")

    aliases: set[str] = set()
    for alias in secret_aliases:
        if not isinstance(alias, str) or not _ALIAS_PATTERN.fullmatch(alias):
            raise DatasetError("secret_aliases contains an invalid alias.")
        aliases.add(alias)
    if not callable(secret_resolver):
        raise DatasetError("secret_resolver must be callable.")
    return frozenset(aliases)


def _resolve_secret(
    value: Any,
    allowed_aliases: frozenset[str],
    secret_resolver: Callable[[str], Any] | None,
    variable_name: str,
    max_cell_chars: int,
) -> Any:
    if not isinstance(value, str) or not value.startswith("secret://"):
        _validate_scalar(value, f"variable {variable_name!r}", max_cell_chars)
        return value

    match = _SECRET_REFERENCE.fullmatch(value)
    if match is None:
        raise DatasetError(f"variable {variable_name!r} has a malformed secret reference.")
    alias = match.group(1)
    if alias not in allowed_aliases:
        raise DatasetError(f"secret alias {alias!r} is not in the caller-supplied allowlist.")
    if secret_resolver is None:
        raise DatasetError("secret references require a caller-supplied resolver.")
    try:
        resolved = secret_resolver(alias)
    except Exception:
        raise DatasetError(f"configured secret alias {alias!r} could not be resolved.") from None
    _validate_scalar(resolved, f"resolved secret for variable {variable_name!r}", max_cell_chars)
    return resolved
