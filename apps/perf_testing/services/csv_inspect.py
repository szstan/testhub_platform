"""Bounded CSV inspection shared by upload validation and previews."""
from __future__ import annotations

import csv
import io


class CSVInspectionError(ValueError):
    pass


class _BudgetedLines:
    """Bound the input to csv.reader before it constructs a complete row.

    csv.reader controls logical record boundaries (including quoted newlines).
    The small lexical guard only counts cells and their source characters; it
    does not materialize rows or change the process-wide csv.field_size_limit.
    """

    def __init__(self, text, *, max_record_chars, max_cell_chars, max_columns,
                 max_physical_lines):
        self.text = text
        self.max_record_chars = max_record_chars
        self.max_cell_chars = max_cell_chars
        self.max_columns = max_columns
        self.max_physical_lines = max_physical_lines
        self.physical_lines = 0
        self.start_record()

    def start_record(self):
        self.record_chars = 0
        self.cell_chars = 0
        self.columns = 1
        self.quoted = False
        self.field_start = True

    def __iter__(self):
        return self

    def __next__(self):
        remaining = self.max_record_chars - self.record_chars
        line = self.text.readline(remaining + 1)
        if not line:
            raise StopIteration
        self.physical_lines += 1
        if self.physical_lines > self.max_physical_lines:
            raise CSVInspectionError(f'CSV 物理行数不能超过 {self.max_physical_lines}')
        self.record_chars += len(line)
        if self.record_chars > self.max_record_chars:
            raise CSVInspectionError(f'CSV 单条记录不能超过 {self.max_record_chars} 个字符')
        for char in line:
            if char == '"' and (self.quoted or self.field_start):
                # A doubled quote closes then reopens the same quoted field.
                self.quoted = not self.quoted
                self.field_start = True
            elif char == ',' and not self.quoted:
                self.columns += 1
                if self.columns > self.max_columns:
                    raise CSVInspectionError(f'CSV 列数不能超过 {self.max_columns}')
                self.cell_chars = 0
                self.field_start = True
                continue
            elif not self.quoted and char in '\r\n':
                continue
            else:
                self.field_start = False
            self.cell_chars += 1
            if self.cell_chars > self.max_cell_chars:
                raise CSVInspectionError(f'CSV 单元格不能超过 {self.max_cell_chars} 个字符')
        return line


def inspect_csv_stream(binary_stream, *, max_records, max_columns, max_cell_chars,
                       max_record_chars, max_physical_lines, preview_limit=0):
    """Return only header/count/bounded preview; rewind the caller-owned stream.

    Empty records still consume work budgets. UTF-8 with BOM and the existing
    GBK replacement fallback are supported, including quoted multiline cells.
    """
    def inspect_encoding(encoding, errors):
        binary_stream.seek(0)
        text = io.TextIOWrapper(binary_stream, encoding=encoding, errors=errors, newline='')
        try:
            lines = _BudgetedLines(
                text, max_record_chars=max_record_chars, max_cell_chars=max_cell_chars,
                max_columns=max_columns, max_physical_lines=max_physical_lines,
            )
            reader = csv.reader(lines, strict=True)
            columns = None
            row_count = 0
            total_rows = 0
            preview = []
            while True:
                lines.start_record()
                try:
                    row = next(reader)
                except StopIteration:
                    break
                if columns is None:
                    columns = [cell.strip() for cell in row]
                    if not any(columns):
                        raise CSVInspectionError('CSV 首行必须是列名')
                    continue
                total_rows += 1
                if total_rows > max_records:
                    raise CSVInspectionError(f'CSV 数据记录数（含空行）不能超过 {max_records}')
                if any(cell.strip() for cell in row):
                    row_count += 1
                if len(preview) < preview_limit:
                    preview.append(row)
            if columns is None:
                raise CSVInspectionError('CSV 文件为空')
            if not row_count:
                raise CSVInspectionError('CSV 除表头外没有数据行')
            return {
                'columns': columns, 'row_count': row_count,
                'total_rows': total_rows, 'rows': preview,
            }
        finally:
            text.detach()

    try:
        try:
            return inspect_encoding('utf-8-sig', 'strict')
        except UnicodeDecodeError:
            return inspect_encoding('gbk', 'replace')
    finally:
        binary_stream.seek(0)
