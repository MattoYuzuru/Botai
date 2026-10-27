"""The model proposes how to read a table of a student's document (FR-018, priority 2).

A document with a table (an exam list, a deadline sheet, a gradebook) is turned into records by the
`table` part of a `document` spec. The model sees a *redacted* sample (names and contacts of other
people are replaced before the sample leaves the server), proposes the mapping, and the student is
asked about every column that is not clear. The proposal is checked against the real headers: a
column the model invented is dropped, never guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from botai.contracts.specs import Kind
from botai.core.llm import LlmMessage, LlmProvider, LlmRequest
from botai.learn import prompting
from botai.learn.author import parse_json
from botai.learn.redact import Redactor
from botai.sources.documents import Extracted, Table
from botai.sources.extract import collapse
from botai.sources.vocab import KIND_LABELS, REQUIRED, TABLE_EXTRA_FIELDS, TABLE_KINDS, VOCAB

# Columns that hold people or contacts: their cells never go to the model, even redacted.
PERSON_HEADER = re.compile(
    r"(?i)фио|ф\.\s?и\.\s?о|фамил|имя|студент|преподават|name|student|teacher|login|"
    r"e-?mail|почт|телефон|phone|паспорт|снилс"
)
PROFILE_PARAMS = ("fullName", "email", "group", "studentId")
MAX_TABLES = 3
SAMPLE_ROWS = 8
CELL = 60
TRANSFORMS = ("iso", "date", "time", "trim", "number")


def norm(value: str) -> str:
    return collapse(value).casefold().replace("ё", "е")


@dataclass(slots=True)
class Column:
    column: str
    transform: str | None = None
    alternatives: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Proposal:
    kind: Kind | None
    sheet: str | None
    header_row: int
    columns: dict[str, Column]
    row_filter: dict[str, str] | None
    doubts: list[str]

    def spec(self, picked: dict[str, str] | None = None) -> dict[str, Any]:
        """The `table` object of a document spec; `picked` overrides columns the student chose."""
        columns: dict[str, Any] = {}
        for target, col in self.columns.items():
            entry: dict[str, Any] = {"column": (picked or {}).get(target, col.column)}
            if col.transform:
                entry["transform"] = col.transform
            columns[target] = entry
        table: dict[str, Any] = {
            "kind": self.kind,
            "headerRow": self.header_row,
            "columns": columns,
        }
        if self.sheet:
            table["sheet"] = self.sheet
        if self.row_filter:
            table["rowFilter"] = dict(self.row_filter)
        return table


# --- what the model is shown ----------------------------------------------------------------


def _masked_columns(rows: list[list[str]]) -> dict[int, int]:
    """column index -> the row from which its cells are people's: where a header names a person."""
    masked: dict[int, int] = {}
    for r, row in enumerate(rows[:4]):
        for c, cell in enumerate(row):
            if PERSON_HEADER.search(cell) and c not in masked:
                masked[c] = r
    return masked


def preview(extracted: Extracted, redactor: Redactor, note: str | None = None) -> str:
    """Tables as the model sees them. Raw cells go to the redactor's `observed` for the lint."""
    parts = ['<observation kind="document">']
    if note:
        parts.append(f"[student's note] {redactor.text(note, 300) or ''}")
    for table in extracted.tables[:MAX_TABLES]:
        rows = table.rows
        masked = _masked_columns(rows)
        parts.append(f'[table] name="{table.name}" rows={len(rows)}')
        for r, row in enumerate(rows[:SAMPLE_ROWS]):
            cells = []
            for c, cell in enumerate(row):
                if r > 0:
                    redactor.observed.add(cell)
                if c in masked and r > masked[c]:
                    cells.append("<person>")
                else:
                    cells.append(redactor.text(cell, CELL) or "<removed>")
            parts.append(f"  {r}: " + " | ".join(cells))
        for row in rows[SAMPLE_ROWS:]:
            for cell in row:
                redactor.observed.add(cell)
    parts.append("</observation>")
    return "\n".join(parts)


def all_data_cells(extracted: Extracted) -> list[str]:
    """Every cell below the first row of every table: the values the lint must keep out of a spec."""
    return [cell for t in extracted.tables for row in t.rows[1:] for cell in row if cell]


def schema() -> dict[str, Any]:
    column = {
        "type": "object",
        "additionalProperties": False,
        "required": ["column"],
        "properties": {
            "column": {"type": "string", "maxLength": 120},
            "transform": {"enum": list(TRANSFORMS)},
            "alternatives": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind"],
        "properties": {
            "kind": {"enum": [*TABLE_KINDS, None]},
            "sheet": {"type": ["string", "null"], "maxLength": 120},
            "headerRow": {"type": "integer", "minimum": 0, "maximum": 50},
            "columns": {"type": "object", "maxProperties": 16, "additionalProperties": column},
            "rowFilter": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {
                    "column": {"type": "string", "maxLength": 120},
                    "equalsParam": {"enum": list(PROFILE_PARAMS)},
                },
            },
            "doubts": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string", "maxLength": 200},
            },
        },
    }


def request(
    observation: str, force: Kind | None = None, feedback: list[str] | None = None
) -> LlmRequest:
    pool = VOCAB[force] if force else set().union(*(VOCAB[k] for k in TABLE_KINDS))
    fields = ", ".join(f"`{name}`" for name in sorted((pool - {"externalId"}) | TABLE_EXTRA_FIELDS))
    forced = (
        f"Студент сказал, что нужны данные вида `{force}` ({KIND_LABELS[force].lower()})."
        if force
        else ""
    )
    note = ("Замечания к прошлому варианту:\n- " + "\n- ".join(feedback[:8])) if feedback else ""
    task = (
        prompting.load("table.md")
        .replace("{{fields}}", fields)
        .replace("{{forced}}", forced)
        .replace("{{feedback}}", note)
        .strip()
    )
    return LlmRequest(
        messages=[
            LlmMessage(role="system", content=prompting.system_prompt()),
            LlmMessage(role="user", content=f"{observation}\n\n{task}"),
        ],
        json_schema=schema(),
        max_tokens=2500,
        temperature=0.1,
        purpose="extract",
    )


# --- what comes back ------------------------------------------------------------------------


def _pick_table(tables: list[Table], sheet: Any) -> Table | None:
    if isinstance(sheet, str) and sheet.strip():
        wanted = norm(sheet)
        for t in tables:
            if norm(t.name) == wanted:
                return t
        return None
    return tables[0] if tables else None


def _header(table: Table, header_row: int) -> dict[str, str]:
    """normalised header -> header as written."""
    if header_row >= len(table.rows):
        return {}
    return {norm(h): h for h in table.rows[header_row] if h.strip()}


def parse(data: Any, extracted: Extracted) -> Proposal | None:
    """Check the answer against the real headers. Invented columns are dropped; a proposal that
    cannot give the kind its required fields is no proposal."""
    if not isinstance(data, dict) or data.get("kind") not in TABLE_KINDS:
        return None
    kind: Kind = data["kind"]
    table = _pick_table(extracted.tables, data.get("sheet"))
    if table is None:
        return None
    raw_header_row = data.get("headerRow")
    header_row = (
        raw_header_row
        if isinstance(raw_header_row, int) and not isinstance(raw_header_row, bool)
        else 0
    )
    headers = _header(table, header_row)
    allowed = (VOCAB[kind] | TABLE_EXTRA_FIELDS) - {"externalId"}
    columns: dict[str, Column] = {}
    raw_columns = data.get("columns")
    for target, raw in (raw_columns if isinstance(raw_columns, dict) else {}).items():
        if target not in allowed or not isinstance(raw, dict):
            continue
        real = headers.get(norm(str(raw.get("column", ""))))
        if real is None:
            continue
        transform = raw.get("transform")
        alternatives = [
            headers[norm(a)]
            for a in raw.get("alternatives", [])
            if isinstance(a, str) and norm(a) in headers and norm(a) != norm(real)
        ]
        columns[target] = Column(
            real, transform if transform in TRANSFORMS else None, alternatives[:3]
        )
    aliases = {"startsAt", "dueAt"} if "date" in columns else set()
    if not REQUIRED[kind] <= set(columns) | aliases:
        return None
    row_filter = None
    raw_filter = data.get("rowFilter")
    if isinstance(raw_filter, dict):
        col = headers.get(norm(str(raw_filter.get("column", ""))))
        param = raw_filter.get("equalsParam")
        if col is not None and param in PROFILE_PARAMS:
            row_filter = {"column": col, "equalsParam": param}
    doubts = [d for d in data.get("doubts", []) if isinstance(d, str)][:3]
    sheet = table.name if len(extracted.tables) > 1 else None
    return Proposal(kind, sheet, header_row, columns, row_filter, doubts)


async def propose(
    llm: LlmProvider,
    extracted: Extracted,
    redactor: Redactor,
    *,
    note: str | None = None,
    force: Kind | None = None,
) -> tuple[Proposal | None, int, bool]:
    """(proposal, tokens spent, whether the model said "this is not a data table")."""
    observation = preview(extracted, redactor, note)
    spent = 0
    for attempt in range(2):
        response = await llm.complete(request(observation, force))
        u = response.usage
        spent += u.input_cache_miss + u.input_cache_hit + u.output
        try:
            data = parse_json(response.content)
        except ValueError:
            continue
        proposal = parse(data, extracted)
        if proposal is not None:
            return proposal, spent, False
        if isinstance(data, dict) and data.get("kind") is None:
            return None, spent, True
        if attempt == 1:
            break
    return None, spent, False
