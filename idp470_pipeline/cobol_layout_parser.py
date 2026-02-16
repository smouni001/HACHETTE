from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import ContractSpec, FieldSpec, FieldType, RecordSpec, SelectorSpec


_COBOL_LINE_SEQ_RE = re.compile(r"^\s*\d{6}")
_COBOL_INLINE_COMMENT_RE = re.compile(r"\*>.*$")
_LEVEL_RE = re.compile(r"^\s*(\d{1,2})\s+([A-Z0-9-]+)(.*)$", re.IGNORECASE)
_PIC_RE = re.compile(r"\bPIC(?:TURE)?\s+([A-Z0-9\(\)V\+\-\.,/SBZAXP]+)", re.IGNORECASE)
_USAGE_RE = re.compile(
    r"\b(?:USAGE\s+IS\s+|USAGE\s+)?(COMP-3|COMP-1|COMP-2|COMP-4|COMP|BINARY|DISPLAY)\b",
    re.IGNORECASE,
)
_OCCURS_RE = re.compile(r"\bOCCURS\s+(\d+)(?:\s+TO\s+(\d+))?\b", re.IGNORECASE)
_REDEFINES_RE = re.compile(r"\bREDEFINES\b", re.IGNORECASE)


@dataclass
class _CobolNode:
    level: int
    name: str
    remainder: str
    children: list["_CobolNode"] = field(default_factory=list)
    occurs: int = 1
    pic: str | None = None
    usage: str | None = None
    redefines: bool = False


@dataclass(frozen=True)
class _ParsedType:
    length: int
    field_type: FieldType
    decimals: int | None


def _normalize_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9_]", "_", value.upper().replace("-", "_"))
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "FIELD"


def _normalize_cobol_line(raw_line: str) -> str:
    line = raw_line.rstrip("\r\n")
    if not line.strip():
        return ""

    working = line
    if len(line) >= 7:
        indicator = line[6]
        if indicator in {"*", "/"}:
            return ""
        working = line[7:]
    elif _COBOL_LINE_SEQ_RE.match(line):
        working = line[6:]

    working = _COBOL_INLINE_COMMENT_RE.sub("", working)
    return working.rstrip()


def _parse_occurs(remainder: str) -> int:
    match = _OCCURS_RE.search(remainder)
    if not match:
        return 1
    minimum = int(match.group(1))
    maximum = int(match.group(2)) if match.group(2) else minimum
    return max(minimum, maximum, 1)


def _expand_picture(pattern: str) -> list[str]:
    compact = pattern.replace(" ", "").rstrip(".")
    tokens: list[str] = []
    index = 0

    while index < len(compact):
        token = compact[index].upper()
        if token in {"'", '"'}:
            index += 1
            continue

        if index + 1 < len(compact) and compact[index + 1] == "(":
            close = compact.find(")", index + 2)
            if close != -1:
                repeat_raw = compact[index + 2 : close]
                if repeat_raw.isdigit():
                    tokens.extend([token] * int(repeat_raw))
                    index = close + 1
                    continue

        tokens.append(token)
        index += 1

    return tokens


def _parse_picture(pic: str, usage: str | None) -> _ParsedType | None:
    tokens = _expand_picture(pic)
    if not tokens:
        return None

    numeric_positions = 0
    decimal_positions = 0
    physical_length = 0
    decimal_part = False
    has_alpha = False

    for token in tokens:
        if token == "V":
            decimal_part = True
            continue
        if token in {"S", "P"}:
            # Sign and assumed decimal scaling do not consume storage in fixed display.
            continue
        if token in {"X", "A"}:
            has_alpha = True
        if token in {"9", "Z"}:
            numeric_positions += 1
            if decimal_part:
                decimal_positions += 1
        physical_length += 1

    normalized_usage = (usage or "").upper()
    if numeric_positions > 0:
        if normalized_usage == "COMP-3":
            physical_length = max(1, (numeric_positions + 1) // 2)
        elif normalized_usage in {"COMP", "COMP-4", "BINARY"}:
            if numeric_positions <= 4:
                physical_length = 2
            elif numeric_positions <= 9:
                physical_length = 4
            else:
                physical_length = 8
        elif normalized_usage == "COMP-1":
            physical_length = 4
        elif normalized_usage == "COMP-2":
            physical_length = 8

    if has_alpha or numeric_positions == 0:
        return _ParsedType(length=max(1, physical_length), field_type=FieldType.STRING, decimals=None)

    if decimal_positions > 0:
        return _ParsedType(
            length=max(1, physical_length),
            field_type=FieldType.DECIMAL,
            decimals=decimal_positions,
        )

    return _ParsedType(length=max(1, physical_length), field_type=FieldType.INTEGER, decimals=None)


def _build_tree(source_text: str) -> list[_CobolNode]:
    root = _CobolNode(level=0, name="ROOT", remainder="")
    stack: list[_CobolNode] = [root]

    for raw_line in source_text.splitlines():
        line = _normalize_cobol_line(raw_line)
        if not line:
            continue

        match = _LEVEL_RE.match(line)
        if not match:
            continue

        level = int(match.group(1))
        if level in {66, 77, 88}:
            continue

        name = _normalize_identifier(match.group(2))
        remainder = (match.group(3) or "").upper()
        pic_match = _PIC_RE.search(remainder)
        usage_match = _USAGE_RE.search(remainder)
        node = _CobolNode(
            level=level,
            name=name,
            remainder=remainder,
            occurs=_parse_occurs(remainder),
            pic=pic_match.group(1) if pic_match else None,
            usage=usage_match.group(1).upper() if usage_match else None,
            redefines=bool(_REDEFINES_RE.search(remainder)),
        )

        while stack and level <= stack[-1].level:
            stack.pop()
        stack[-1].children.append(node)
        stack.append(node)

    return [node for node in root.children if node.level == 1]


def _append_field(
    *,
    fields: list[FieldSpec],
    name: str,
    start: int,
    length: int,
    field_type: FieldType,
    decimals: int | None,
    used_names: set[str],
) -> None:
    base = _normalize_identifier(name)
    candidate = base
    suffix = 2
    while candidate in used_names:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_names.add(candidate)

    fields.append(
        FieldSpec(
            name=candidate,
            start=start,
            length=length,
            type=field_type,
            decimals=decimals,
        )
    )


def _emit_fields(
    node: _CobolNode,
    *,
    prefix: str,
    position: int,
    fields: list[FieldSpec],
    used_names: set[str],
) -> int:
    if node.redefines:
        return position

    qualified_name = f"{prefix}_{node.name}" if prefix else node.name
    occurs = max(1, node.occurs)

    if node.pic:
        parsed = _parse_picture(node.pic, node.usage)
        if parsed is None:
            return position
        for index in range(occurs):
            suffix = f"_{index + 1}" if occurs > 1 else ""
            _append_field(
                fields=fields,
                name=f"{qualified_name}{suffix}",
                start=position,
                length=parsed.length,
                field_type=parsed.field_type,
                decimals=parsed.decimals,
                used_names=used_names,
            )
            position += parsed.length
        return position

    for index in range(occurs):
        group_suffix = f"_{index + 1}" if occurs > 1 else ""
        next_prefix = f"{qualified_name}{group_suffix}" if qualified_name else prefix
        for child in node.children:
            position = _emit_fields(
                child,
                prefix=next_prefix,
                position=position,
                fields=fields,
                used_names=used_names,
            )
    return position


def _normalize_filter_names(structure_names: set[str] | None) -> set[str]:
    return {_normalize_identifier(name) for name in (structure_names or set()) if name}


def _normalize_filter_prefixes(structure_prefixes: tuple[str, ...] | None) -> tuple[str, ...]:
    prefixes = tuple(_normalize_identifier(prefix) for prefix in (structure_prefixes or ()) if prefix)
    return tuple(prefix for prefix in prefixes if prefix)


def _should_include_record(
    record_name: str,
    *,
    normalized_names: set[str],
    normalized_prefixes: tuple[str, ...],
) -> bool:
    if not normalized_names and not normalized_prefixes:
        return True
    normalized = _normalize_identifier(record_name)
    if normalized in normalized_names:
        return True
    return any(normalized.startswith(prefix) for prefix in normalized_prefixes)


def extract_contract_from_cobol_source(
    source_path: Path,
    source_program: str = "COBOL_SOURCE",
    strict: bool = True,
    *,
    structure_prefixes: tuple[str, ...] | None = None,
    structure_names: set[str] | None = None,
    preserve_structure_names: bool = False,  # kept for API compatibility
) -> ContractSpec:
    _ = preserve_structure_names
    text = source_path.read_text(encoding="latin-1")
    record_nodes = _build_tree(text)
    normalized_names = _normalize_filter_names(structure_names)
    normalized_prefixes = _normalize_filter_prefixes(structure_prefixes)

    selected_specs: list[RecordSpec] = []
    for node in record_nodes:
        if not _should_include_record(
            node.name,
            normalized_names=normalized_names,
            normalized_prefixes=normalized_prefixes,
        ):
            continue

        fields: list[FieldSpec] = []
        used_names: set[str] = set()
        end_position = _emit_fields(
            node,
            prefix="",
            position=1,
            fields=fields,
            used_names=used_names,
        )
        if not fields:
            continue

        selected_specs.append(
            RecordSpec(
                name=node.name,
                selector=SelectorSpec(start=1, length=1, value=node.name[0]),
                fields=fields,
            )
        )

    if not selected_specs:
        raise ValueError("No structure found for selected filters in source file.")

    line_length = max(spec.max_end for spec in selected_specs)
    strict_length_validation = strict and len(selected_specs) == 1

    return ContractSpec(
        source_program=source_program,
        line_length=line_length,
        strict_length_validation=strict_length_validation,
        record_types=selected_specs,
    )
