from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import ContractSpec, FieldSpec, FieldType, RecordSpec, SelectorSpec

LOGGER = logging.getLogger(__name__)

_RECORD_TYPES = ("FIC", "ENT", "ECH", "COM", "REF", "ADR", "AD2", "LIG", "LEC", "PIE")
_ROW_START_RE = re.compile(r"^\s*C(\d{2})\b", re.IGNORECASE)
_RECORD_HEADER_RE = re.compile(r"enregistrement\s+([A-Z0-9]{3})\b", re.IGNORECASE)
_PAGE_FOOTER_RE = re.compile(r"Page\s+\d+\s+sur\s+\d+.*", re.IGNORECASE)
_TABLE_HEADER_RE = re.compile(r"\bCol\s+Code\s+Po?s\b", re.IGNORECASE)


def _load_pdf_pages(pdf_path: Path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as error:
        raise RuntimeError("PDF parsing requires pypdf. Install with: pip install pypdf") from error

    reader = PdfReader(str(pdf_path))
    return [(page.extract_text() or "") for page in reader.pages]


def _sanitize_line(raw_line: str) -> str:
    line = raw_line.replace("\x00", " ").strip()
    line = _PAGE_FOOTER_RE.sub("", line).strip()
    return re.sub(r"\s+", " ", line).strip()


def _is_control_line(line: str) -> bool:
    lowered = line.lower()
    return lowered.startswith("controles") or lowered.startswith("controle") or lowered.startswith("contr")


def _extract_record_blocks(text: str) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {record: [] for record in _RECORD_TYPES}
    current_record: str | None = None
    current_row: list[str] = []

    def flush_row() -> None:
        nonlocal current_row
        if not current_row or current_record is None:
            current_row = []
            return
        joined = re.sub(r"\s+", " ", " ".join(current_row)).strip()
        if joined:
            blocks[current_record].append(joined)
        current_row = []

    for raw_line in text.splitlines():
        line = _sanitize_line(raw_line)
        if not line:
            continue
        if _TABLE_HEADER_RE.search(line):
            continue

        header_match = _RECORD_HEADER_RE.search(line)
        if header_match:
            candidate = header_match.group(1).upper()
            if candidate in blocks:
                flush_row()
                current_record = candidate

        if current_record is None:
            continue

        if _is_control_line(line):
            flush_row()
            continue

        if _ROW_START_RE.match(line):
            flush_row()
            current_row = [line]
            continue

        if current_row:
            current_row.append(line)

    flush_row()
    return blocks


def _normalize_field_name(source: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", source.upper()).strip("_")
    return normalized or "FIELD"


def _parse_format(fmt: str) -> tuple[FieldType, int, int | None]:
    token = fmt.upper().replace(" ", "")
    if token == "DATE":
        return FieldType.DATE, 8, None

    match = re.fullmatch(r"AN(\d+)", token)
    if match:
        return FieldType.STRING, int(match.group(1)), None

    match = re.fullmatch(r"N(\d+)", token)
    if match:
        return FieldType.INTEGER, int(match.group(1)), None

    match = re.fullmatch(r"N(\d+),(\d+)", token)
    if match:
        return FieldType.DECIMAL, int(match.group(1)), int(match.group(2))

    match = re.fullmatch(r"SN(\d+),(\d+)", token)
    if match:
        return FieldType.DECIMAL, int(match.group(1)), int(match.group(2))

    raise ValueError(f"Unsupported IDIL format token: {fmt}")


def _clean_description(text: str) -> str:
    description = _PAGE_FOOTER_RE.sub("", text)
    description = re.sub(r"\bCol\s+Code\s+Po?s.*", "", description, flags=re.IGNORECASE)
    description = re.sub(r"\s+", " ", description).strip()
    return description


def _parse_row(row_text: str) -> tuple[str, int, str, str] | None:
    compact = re.sub(r"\s+", " ", row_text).strip()
    match = re.match(
        r"^C\d{2}\s+(.+?)\s+(\d+)\s+([A-Z]+(?:\d+(?:,\d+)?)?)\s+([OFCN])\s+(.+)$",
        compact,
        re.IGNORECASE,
    )
    if not match:
        return None

    code = match.group(1).strip()
    position = int(match.group(2))
    fmt = match.group(3).strip().upper()
    description = _clean_description(match.group(5))
    return code, position, fmt, description


def _build_record_specs(blocks: dict[str, list[str]]) -> list[RecordSpec]:
    records: list[RecordSpec] = []

    for record_name in _RECORD_TYPES:
        rows = blocks.get(record_name, [])
        if not rows:
            continue

        fields: list[FieldSpec] = []
        used_names: set[str] = set()

        for row in rows:
            parsed = _parse_row(row)
            if parsed is None:
                LOGGER.debug("Skipping unparsed row for %s: %s", record_name, row)
                continue

            code, position, fmt, description = parsed
            try:
                field_type, length, decimals = _parse_format(fmt)
            except ValueError:
                LOGGER.debug("Skipping unsupported format for %s: %s", record_name, row)
                continue

            base_name = _normalize_field_name(code)
            field_name = base_name
            suffix = 2
            while field_name in used_names:
                field_name = f"{base_name}_{suffix}"
                suffix += 1
            used_names.add(field_name)

            fields.append(
                FieldSpec(
                    name=field_name,
                    start=position,
                    length=length,
                    type=field_type,
                    decimals=decimals,
                    description=description,
                )
            )

        if not fields:
            continue

        fields = sorted(fields, key=lambda item: item.start)
        records.append(
            RecordSpec(
                name=record_name,
                selector=SelectorSpec(start=1, length=3, value=record_name),
                fields=fields,
            )
        )

    return records


def extract_contract_from_idil_pdf(
    pdf_path: Path,
    source_program: str = "IDP470RA",
) -> ContractSpec:
    pages = _load_pdf_pages(pdf_path)
    if not pages:
        raise ValueError(f"Unable to read PDF: {pdf_path}")

    # Section 3.x of the document contains the record layouts.
    section_text = "\n".join(pages[6:28])
    blocks = _extract_record_blocks(section_text)
    records = _build_record_specs(blocks)
    if not records:
        raise ValueError("No IDIL record layout extracted from PDF section 3.x.")

    line_length = max(record.max_end for record in records)
    return ContractSpec(
        source_program=source_program,
        line_length=line_length,
        strict_length_validation=False,
        record_types=records,
    )
