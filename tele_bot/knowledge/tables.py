"""Format-neutral table contracts and deterministic, bounded field rendering.

Positions are one-based source coordinates; CSV rows are logical records. Only
explicit merge ranges propagate values. No type inference or model is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from urllib.parse import quote


@dataclass(frozen=True)
class TableLimits:
    max_unpacked_bytes: int = 100 * 1024 * 1024
    max_sheets: int = 32
    max_rows: int = 50000
    max_columns: int = 256
    max_cells: int = 500000
    max_cell_chars: int = 100000
    max_output_chars: int = 10000000

    def __post_init__(self):
        if any(getattr(self, field.name) <= 0 for field in fields(self)):
            raise ValueError("table limits must be positive")

    @classmethod
    def from_env(cls):
        from tele_bot.knowledge.config import kb_option
        return cls(**{f.name: int(kb_option("KB_TABLE_" + f.name.upper(), str(f.default)))
                      for f in fields(cls)})

    def check(self, value: int, maximum: int, label: str):
        if value > maximum:
            raise ValueError(f"表格 {label} 超限（{value} > {maximum}），请缩小工作表/区域或拆分文件后重新导入")


@dataclass(frozen=True)
class TableReadOptions:
    """Explicit deterministic choices, persisted by the confirmation snapshot."""
    header_rows: int = 1
    delimiter: str | None = None
    encoding: str | None = None
    sheets: tuple[str, ...] = ()
    cell_range: str | None = None

    def __post_init__(self):
        if self.header_rows < 0:
            raise ValueError("header_rows must be nonnegative")
        if self.delimiter is not None and (len(self.delimiter) != 1 or self.delimiter in '\r\n"'):
            raise ValueError("CSV delimiter must be one character")


@dataclass(frozen=True)
class TableCell:
    row: int
    column: int
    value: str | None
    formula: str | None = None
    number_format: str = ""
    unit: str = ""
    merged_from: tuple[int, int] | None = None

    def readable(self) -> str:
        value = self.value if self.value is not None else "[空值]"
        if self.formula is not None:
            value = (f"[公式 {self.formula}；缓存：{self.value}]" if self.value is not None
                     else f"[公式 {self.formula}；缺失缓存，未重算]")
        if self.number_format and self.number_format != "General":
            value += f" [源格式：{self.number_format}]"
        if self.unit:
            value += f" [{self.unit}]"
        return value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").replace("\n", " ↵ ")


@dataclass(frozen=True)
class TableColumn:
    position: int
    header_path: tuple[str, ...]
    name: str
    unit: str = ""


@dataclass(frozen=True)
class TableRow:
    position: int
    cells: tuple[TableCell, ...]
    record_id: str | None = None


@dataclass(frozen=True)
class TableMerge:
    first_row: int
    first_column: int
    last_row: int
    last_column: int


@dataclass(frozen=True)
class StructuredTable:
    source: str
    identity: str
    title: str
    columns: tuple[TableColumn, ...]
    rows: tuple[TableRow, ...]
    header_rows: tuple[int, ...] = ()
    merges: tuple[TableMerge, ...] = ()
    description: str = ""
    region: str = ""
    section_path: tuple[str, ...] = ()

    def locator(self, row: TableRow | None = None) -> str:
        base = f"{self.source}:table:{quote(self.identity, safe=':')}:range:{self.region}"
        if row is not None:
            label = "record" if self.source == "csv" else "row"
            base += f":{label}:{row.position}:cols:" + ",".join(str(c.position) for c in self.columns)
            if row.record_id:
                base += f":record_id:{quote(row.record_id, safe='')}"
        return base


@dataclass(frozen=True)
class TableTextPart:
    text: str
    locator: str
    path: tuple[str, ...]


def build_table(*, source: str, identity: str, title: str, rows: tuple[TableRow, ...],
                header_rows: int = 1, merges: tuple[TableMerge, ...] = (),
                region: str = "", section_path: tuple[str, ...] = ()) -> StructuredTable:
    if not 0 <= header_rows <= len(rows):
        raise ValueError("header_rows exceeds available table rows")
    if len({row.position for row in rows}) != len(rows):
        raise ValueError("duplicate table row positions")
    for row in rows:
        if row.position < 1 or len({c.column for c in row.cells}) != len(row.cells):
            raise ValueError("invalid or duplicate table coordinates")
        if any(c.row != row.position or c.column < 1 for c in row.cells):
            raise ValueError("invalid table cell position")
    positions = sorted({cell.column for row in rows for cell in row.cells})
    # Expand ONLY known merges, including real header spans. Never forward-fill blanks.
    values = {(cell.row, cell.column): cell for row in rows for cell in row.cells}
    merged_values = {}
    for merge in merges:
        if min(merge.first_row, merge.first_column) < 1 or merge.last_row < merge.first_row or merge.last_column < merge.first_column:
            raise ValueError("invalid table merge range")
        anchor = values.get((merge.first_row, merge.first_column))
        if anchor:
            for row_position in range(merge.first_row, merge.last_row + 1):
                for column_position in range(merge.first_column, merge.last_column + 1):
                    if (row_position, column_position) in merged_values:
                        raise ValueError("overlapping table merge ranges")
                    merged_values[row_position, column_position] = anchor
    expanded = []
    for row in rows:
        cells = []
        for position in positions:
            cell = values.get((row.position, position), TableCell(row.position, position, None))
            anchor = merged_values.get((row.position, position))
            if anchor and (row.position, position) != (anchor.row, anchor.column):
                cell = TableCell(row.position, position, anchor.value, anchor.formula,
                                 anchor.number_format, anchor.unit, (anchor.row, anchor.column))
            cells.append(cell)
        expanded.append(TableRow(row.position, tuple(cells), row.record_id))
    headers = expanded[:header_rows]
    names, columns = set(), []
    for index, position in enumerate(positions):
        path = []
        for row in headers:
            cell = row.cells[index]
            value = cell.readable() if cell.formula is not None else cell.value
            if value and (not path or path[-1] != value):
                path.append(value.replace("\n", " ↵ "))
        base = " / ".join(path) or f"列{position}"
        name = base
        if name in names or not path:
            name = f"{base} [列{position}]"
        while name in names:
            name += f" [列{position}]"
        names.add(name)
        columns.append(TableColumn(position, tuple(path), name))
    return StructuredTable(source, identity, title, tuple(columns), tuple(expanded[header_rows:]),
                           tuple(row.position for row in headers), merges, region=region,
                           section_path=section_path)


def render_table(table: StructuredTable, *, max_chars: int, limits: TableLimits) -> tuple[TableTextPart, ...]:
    """Every part is an atomic normalized text section; offsets need no invented spans.

    Whole rows are preferred. Oversized records split by field, then by value,
    repeating table/record/field context. No overlap or truncation is used.
    """
    context = f"表格：{table.title}\n"
    if table.description:
        context += f"说明：{table.description}\n"
    parts, total = [], 0
    pending_rows, pending_text = [], ""

    def append(text, locator):
        nonlocal total
        total += len(text)
        limits.check(total, limits.max_output_chars, "规范化输出字符数")
        parts.append(TableTextPart(text, locator, table.section_path or (table.title,)))

    def flush_rows():
        nonlocal pending_rows, pending_text
        if pending_rows:
            locator = table.locator(pending_rows[0])
            if len(pending_rows) > 1:
                locator += f":last_row:{pending_rows[-1].position}"
            append(context + pending_text, locator)
            pending_rows, pending_text = [], ""

    if not table.rows:
        body = context + "字段：" + "；".join(c.name for c in table.columns) + "\n[无数据记录]"
        if len(body) > max_chars:
            raise ValueError("表头上下文超过分块预算，请增加 KB_CHUNK_MAX_CHARS 或缩小列范围")
        append(body, table.locator())
    for row in table.rows:
        if row.record_id:
            # Base record identity must be present on EVERY fragment; do not pack
            # distinct stable records under one citation.
            flush_rows()
        fields_text = [(column, cell.readable()) for column, cell in zip(table.columns, row.cells, strict=True)]
        row_text = (f"记录：{row.record_id}；" if row.record_id else "") + "；".join(f"{c.name}：{value}" for c, value in fields_text)
        body = context + row_text
        if len(body) <= max_chars:
            combined = pending_text + ("\n" if pending_text else "") + row_text
            if len(context + combined) > max_chars:
                flush_rows()
                combined = row_text
            pending_rows.append(row)
            pending_text = combined
            if row.record_id:
                flush_rows()
            continue
        flush_rows()
        # First-column label contextualizes a matrix's numerical values. A long
        # label stays in its own field fragments; positional record identity remains.
        label = fields_text[0][1] if fields_text else ""
        record = f"记录：{row.record_id}\n" if row.record_id else f"记录：{label}\n" if len(label) <= max_chars // 4 else f"记录行：{row.position}\n"
        for column, value in fields_text:
            prefix = context + record + f"{column.name}："
            capacity = max_chars - len(prefix)
            if capacity <= 0:
                raise ValueError("表名/字段上下文超过分块预算，请增加 KB_CHUNK_MAX_CHARS 或缩小列范围")
            for start in range(0, len(value), capacity):
                append(prefix + value[start:start + capacity],
                       f"{table.locator(row)}:field:{column.position}:value_chars:{start}-{min(start + capacity, len(value))}")
    flush_rows()
    return tuple(parts)
