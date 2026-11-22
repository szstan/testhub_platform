from io import BytesIO
import zipfile
from unittest.mock import Mock

import pytest

from apps.llm_judge import file_parser


def _xlsx(rows):
    openpyxl = pytest.importorskip('openpyxl')
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _append_zip_members(content, members):
    output = BytesIO(content)
    with zipfile.ZipFile(output, 'a', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            archive.writestr(name, payload)
    return output.getvalue()


def _zip_sizes(content):
    with zipfile.ZipFile(BytesIO(content)) as archive:
        return [(info.filename, info.file_size, info.compress_size) for info in archive.infolist()]


def test_parse_xlsx_limits_rows_while_streaming_and_caps_errors(monkeypatch):
    monkeypatch.setattr(file_parser, 'MAX_XLSX_ROWS', 2)
    monkeypatch.setattr(file_parser, 'MAX_CASES', 10)
    monkeypatch.setattr(file_parser, 'MAX_PARSE_ERRORS', 4)
    content = _xlsx([
        ['question', 'answer'],
        ['valid 1', 'answer'],
        ['valid 2', 'answer'],
        ['', ''],
        ['', ''],
        ['', ''],
    ])

    result = file_parser.parse_xlsx(content, 'cases.xlsx')

    assert result['total_rows'] == 2
    assert result['valid_rows'] == 2
    assert len(result['cases']) == 2
    assert any('行数超过上限 2' in error for error in result['errors'])
    assert len(result['errors']) <= 4


def test_parse_xlsx_rejects_workbooks_over_column_limit(monkeypatch):
    monkeypatch.setattr(file_parser, 'MAX_XLSX_COLUMNS', 3)
    content = _xlsx([
        ['question', 'answer', 'ground_truth', 'unexpected'],
        ['q', 'a', '', 'extra'],
    ])

    result = file_parser.parse_xlsx(content, 'wide.xlsx')

    assert result['valid_rows'] == 0
    assert result['cases'] == []
    assert any('列数超过上限 3' in error for error in result['errors'])


def test_parse_xlsx_closes_workbook_on_early_column_rejection(monkeypatch):
    openpyxl = pytest.importorskip('openpyxl')
    workbook = Mock()
    workbook.active.max_column = file_parser.MAX_XLSX_COLUMNS + 1
    monkeypatch.setattr(file_parser, '_validate_xlsx_zip_resources', lambda content: None)
    monkeypatch.setattr(openpyxl, 'load_workbook', lambda *args, **kwargs: workbook)

    result = file_parser.parse_xlsx(b'xlsx', 'wide.xlsx')

    assert result['valid_rows'] == 0
    workbook.close.assert_called_once_with()


def test_parse_xlsx_closes_workbook_when_iteration_fails(monkeypatch):
    openpyxl = pytest.importorskip('openpyxl')
    workbook = Mock()
    workbook.active.max_column = 2
    workbook.active.max_row = 1
    workbook.active.iter_rows.side_effect = RuntimeError('broken worksheet')
    monkeypatch.setattr(file_parser, '_validate_xlsx_zip_resources', lambda content: None)
    monkeypatch.setattr(openpyxl, 'load_workbook', lambda *args, **kwargs: workbook)

    result = file_parser.parse_xlsx(b'xlsx', 'broken.xlsx')

    assert 'XLSX 解析失败' in result['errors'][0]
    workbook.close.assert_called_once_with()


def test_parse_xlsx_caps_retained_row_errors(monkeypatch):
    monkeypatch.setattr(file_parser, 'MAX_XLSX_ROWS', 4)
    monkeypatch.setattr(file_parser, 'MAX_PARSE_ERRORS', 4)
    content = _xlsx([['question', 'answer']] + [[' ', 'answer'] for _ in range(12)])

    result = file_parser.parse_xlsx(content, 'invalid.xlsx')

    assert len(result['errors']) == 4
    assert file_parser._ERROR_OVERFLOW in result['errors']
    assert '行数超过上限 4' in result['errors'][-1]


def test_parse_xlsx_accepts_exact_zip_resource_limits(monkeypatch):
    content = _xlsx([['question', 'answer'], ['q', 'a']])
    members = _zip_sizes(content)
    monkeypatch.setattr(file_parser, 'MAX_XLSX_ZIP_MEMBER_BYTES', max(size for _, size, _ in members))
    monkeypatch.setattr(file_parser, 'MAX_XLSX_ZIP_TOTAL_BYTES', sum(size for _, size, _ in members))
    monkeypatch.setattr(file_parser, 'MAX_XLSX_ZIP_MEMBERS', len(members))

    result = file_parser.parse_xlsx(content, 'boundary.xlsx')

    assert result['valid_rows'] == 1
    assert result['cases'] == [{'question': 'q', 'answer': 'a', 'auto_gt': True}]


def test_parse_xlsx_rejects_expanded_shared_strings_before_openpyxl(monkeypatch):
    openpyxl = pytest.importorskip('openpyxl')
    payload = (
        b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<si><t>' + b'A' * 4096 + b'</t></si></sst>'
    )
    content = _append_zip_members(_xlsx([['question', 'answer'], ['q', 'a']]), [
        ('xl/sharedStrings.xml', payload),
    ])
    shared_strings = next(item for item in _zip_sizes(content) if item[0] == 'xl/sharedStrings.xml')
    assert shared_strings[2] < 1024 < shared_strings[1]
    monkeypatch.setattr(file_parser, 'MAX_XLSX_ZIP_MEMBER_BYTES', 1024)
    monkeypatch.setattr(file_parser, 'MAX_XLSX_ZIP_TOTAL_BYTES', 1024 * 1024)
    monkeypatch.setattr(openpyxl, 'load_workbook', lambda *args, **kwargs: pytest.fail('openpyxl must not load'))

    result = file_parser.parse_xlsx(content, 'bomb.xlsx')

    assert result['filename'] == 'bomb.xlsx'
    assert set(result) == {'filename', 'total_rows', 'valid_rows', 'errors', 'preview', 'cases'}
    assert result['total_rows'] == 0
    assert result['valid_rows'] == 0
    assert result['preview'] == []
    assert result['cases'] == []
    assert 'XLSX 解析失败' in result['errors'][0]
    assert '单个文件解压后超过上限 1024 字节' in result['errors'][0]


def test_parse_xlsx_rejects_cumulative_zip_expansion_at_boundary(monkeypatch):
    openpyxl = pytest.importorskip('openpyxl')
    content = _xlsx([['question', 'answer'], ['q', 'a']])
    original_total = sum(size for _, size, _ in _zip_sizes(content))
    content = _append_zip_members(content, [
        ('xl/custom/part1.xml', b'A' * 600),
        ('xl/custom/part2.xml', b'B' * 600),
    ])
    monkeypatch.setattr(file_parser, 'MAX_XLSX_ZIP_MEMBER_BYTES', max(
        size for _, size, _ in _zip_sizes(content)
    ))
    monkeypatch.setattr(file_parser, 'MAX_XLSX_ZIP_TOTAL_BYTES', original_total + 1199)
    monkeypatch.setattr(openpyxl, 'load_workbook', lambda *args, **kwargs: pytest.fail('openpyxl must not load'))

    result = file_parser.parse_xlsx(content, 'total-bomb.xlsx')

    assert result['filename'] == 'total-bomb.xlsx'
    assert set(result) == {'filename', 'total_rows', 'valid_rows', 'errors', 'preview', 'cases'}
    assert result['total_rows'] == 0
    assert result['valid_rows'] == 0
    assert result['preview'] == []
    assert result['cases'] == []
    assert '总解压大小超过上限' in result['errors'][0]
