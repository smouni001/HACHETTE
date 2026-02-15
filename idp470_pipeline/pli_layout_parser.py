from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .models import ContractSpec, FieldSpec, FieldType, RecordSpec, SelectorSpec


_DCL_STRUCT_RE = re.compile(r"\bDCL\s+0?1\s+([A-Z0-9_]+)\b", re.IGNORECASE)
_FIELD_RE = re.compile(r"^\s*(\d+)\s+([A-Z0-9_]+)\s*(.*)$", re.IGNORECASE)
_CHAR_RE = re.compile(r"\bCHAR\s*\(\s*(\d+)\s*\)", re.IGNORECASE)
_PIC_RE = re.compile(r"\bPIC\s*'([^']+)'", re.IGNORECASE)
_DEC_RE = re.compile(
    r"\bDEC\s+FIXED\s*\(\s*(\d+)(?:\s*,\s*(\d+))?\s*\)", re.IGNORECASE
)
_TRAILING_SEQ_RE = re.compile(r"\s+\d{5,}\s*$")
_COMMENT_RE = re.compile(r"/\*(.*?)\*/")
_LIKE_RE = re.compile(r"\bLIKE\s+([A-Z0-9_]+)\.([A-Z0-9_]+)\b", re.IGNORECASE)


@dataclass
class _GroupState:
    level: int
    name: str
    description: str | None = None


@dataclass
class _ParsedType:
    length: int
    field_type: FieldType
    decimals: int | None


def _normalize_source_line(raw_line: str) -> str:
    line = raw_line.rstrip("\r\n")
    line = _TRAILING_SEQ_RE.sub("", line)
    if line and line[0].isdigit():
        line = line[1:]
    return line.rstrip()


def _parse_pic_pattern(pattern: str) -> tuple[int, int]:
    """Returns (physical_length, decimal_digits)."""
    expanded: list[str] = []
    source = pattern.replace(" ", "")
    index = 0

    while index < len(source):
        char = source[index]
        if char == "(":
            close = source.find(")", index)
            if close == -1 or close + 1 >= len(source):
                break
            repeat_count = int(source[index + 1 : close])
            repeated_token = source[close + 1]
            expanded.extend([repeated_token] * repeat_count)
            index = close + 2
            continue
        expanded.append(char)
        index += 1

    physical_length = 0
    decimal_digits = 0
    decimal_part = False

    for token in expanded:
        upper = token.upper()
        if upper == "V":
            decimal_part = True
            continue
        if upper in {"9", "Z", "A", "X", "B", ".", ",", "-", "+", "/"}:
            physical_length += 1
            if decimal_part and upper in {"9", "Z"}:
                decimal_digits += 1

    return physical_length, decimal_digits


def _parse_decl_type(remainder: str) -> _ParsedType | None:
    char_match = _CHAR_RE.search(remainder)
    if char_match:
        return _ParsedType(
            length=int(char_match.group(1)),
            field_type=FieldType.STRING,
            decimals=None,
        )

    pic_match = _PIC_RE.search(remainder)
    if pic_match:
        length, decimals = _parse_pic_pattern(pic_match.group(1))
        field_type = FieldType.DECIMAL if decimals > 0 else FieldType.INTEGER
        return _ParsedType(length=length, field_type=field_type, decimals=decimals or None)

    dec_match = _DEC_RE.search(remainder)
    if dec_match:
        precision = int(dec_match.group(1))
        scale = int(dec_match.group(2) or 0)
        field_type = FieldType.DECIMAL if scale > 0 else FieldType.INTEGER
        return _ParsedType(length=precision, field_type=field_type, decimals=scale or None)

    return None


def _extract_comment_fragments(text: str) -> list[str]:
    comments: list[str] = []
    for match in _COMMENT_RE.finditer(text):
        raw = match.group(1)
        if raw is None:
            continue
        cleaned = re.sub(r"\s+", " ", raw).strip()
        if not cleaned:
            continue
        comments.append(cleaned)
    return comments


def _structure_to_record_name(structure_name: str) -> str | None:
    if structure_name == "DEMAT_GEN" or structure_name == "STO_D_GEN":
        return None
    if structure_name.startswith("DEMAT_"):
        return structure_name.removeprefix("DEMAT_")
    if structure_name.startswith("STO_D_"):
        return structure_name.removeprefix("STO_D_")
    return None


def _clone_template_fields(
    template_fields: list[FieldSpec],
    start_position: int,
) -> tuple[list[FieldSpec], int]:
    if not template_fields:
        return [], 0

    ordered = sorted(template_fields, key=lambda field: field.start)
    base_start = ordered[0].start
    consumed = 0
    cloned: list[FieldSpec] = []

    for field in ordered:
        relative_start = field.start - base_start + 1
        new_start = start_position + relative_start
        consumed = max(consumed, relative_start + field.length - 1)
        cloned.append(
            FieldSpec(
                name=field.name,
                start=new_start,
                length=field.length,
                type=field.type,
                decimals=field.decimals,
                description=field.description,
            )
        )

    return cloned, consumed


def _normalize_group_templates(group_fields: dict[str, list[FieldSpec]]) -> dict[str, list[FieldSpec]]:
    normalized: dict[str, list[FieldSpec]] = {}
    for group_name, fields in group_fields.items():
        ordered = sorted(fields, key=lambda field: field.start)
        if not ordered:
            continue
        base_start = ordered[0].start
        normalized[group_name] = [
            FieldSpec(
                name=field.name,
                start=field.start - base_start + 1,
                length=field.length,
                type=field.type,
                decimals=field.decimals,
                description=field.description,
            )
            for field in ordered
        ]
    return normalized


def _build_record_specs_from_text(
    source_text: str,
    *,
    structure_prefixes: tuple[str, ...] | None = None,
    structure_names: set[str] | None = None,
    preserve_structure_names: bool = False,
) -> list[RecordSpec]:
    records_by_name: dict[str, RecordSpec] = {}
    templates_by_structure: dict[str, dict[str, list[FieldSpec]]] = {}
    current_structure: str | None = None
    stack: list[_GroupState] = []
    current_fields: list[FieldSpec] = []
    current_group_fields: dict[str, list[FieldSpec]] = {}
    current_position = 0

    normalized_prefixes = tuple((prefix or "").upper() for prefix in (structure_prefixes or ()))
    normalized_names = {name.upper() for name in (structure_names or set())}
    if not normalized_prefixes and not normalized_names:
        normalized_prefixes = ("DEMAT_", "STO_D_")

    def include_structure(name: str) -> bool:
        upper = name.upper()
        if upper in normalized_names:
            return True
        return any(upper.startswith(prefix) for prefix in normalized_prefixes)

    def resolve_record_name(structure_name: str) -> str | None:
        mapped = _structure_to_record_name(structure_name)
        if mapped:
            return mapped
        if preserve_structure_names:
            return structure_name
        return None

    def flush_current() -> None:
        nonlocal current_structure, stack, current_fields, current_group_fields, current_position
        if not current_structure:
            return

        if current_fields:
            templates_by_structure[current_structure] = _normalize_group_templates(current_group_fields)

            if include_structure(current_structure):
                record_name = resolve_record_name(current_structure)
                if record_name and record_name not in records_by_name:
                    selector_value = record_name[:3]
                    records_by_name[record_name] = RecordSpec(
                        name=record_name,
                        selector=SelectorSpec(start=1, length=3, value=selector_value),
                        fields=current_fields,
                    )

        current_structure = None
        stack = []
        current_fields = []
        current_group_fields = {}
        current_position = 0

    for raw_line in source_text.splitlines():
        line = _normalize_source_line(raw_line)
        if not line:
            continue

        start_match = _DCL_STRUCT_RE.search(line)
        if start_match:
            next_structure = start_match.group(1).upper()
            flush_current()
            current_structure = next_structure
            stack = []
            current_fields = []
            current_group_fields = {}
            current_position = 0
            inline_remainder = line[start_match.end() :]
            inline_type = _parse_decl_type(inline_remainder)
            if inline_type is not None and inline_type.length > 0:
                header_field = FieldSpec(
                    name="VALUE",
                    start=1,
                    length=inline_type.length,
                    type=inline_type.field_type,
                    decimals=inline_type.decimals,
                    description=None,
                )
                current_fields.append(header_field)
                current_group_fields.setdefault("__ROOT__", []).append(header_field)
                current_position = inline_type.length
            continue

        if current_structure is None:
            continue

        # Structure boundary can be reached by another DCL level 01.
        boundary_match = _DCL_STRUCT_RE.search(line)
        if boundary_match:
            flush_current()
            next_structure = boundary_match.group(1).upper()
            current_structure = next_structure
            stack = []
            current_fields = []
            current_group_fields = {}
            current_position = 0
            inline_remainder = line[boundary_match.end() :]
            inline_type = _parse_decl_type(inline_remainder)
            if inline_type is not None and inline_type.length > 0:
                header_field = FieldSpec(
                    name="VALUE",
                    start=1,
                    length=inline_type.length,
                    type=inline_type.field_type,
                    decimals=inline_type.decimals,
                    description=None,
                )
                current_fields.append(header_field)
                current_group_fields.setdefault("__ROOT__", []).append(header_field)
                current_position = inline_type.length
            continue

        field_match = _FIELD_RE.match(line)
        if not field_match:
            continue

        level = int(field_match.group(1))
        name = field_match.group(2).upper()
        remainder = field_match.group(3)
        comment_fragments = _extract_comment_fragments(remainder)
        inline_description = comment_fragments[0] if comment_fragments else None

        while stack and stack[-1].level >= level:
            stack.pop()

        parsed_type = _parse_decl_type(remainder)
        if parsed_type is None:
            like_match = _LIKE_RE.search(remainder)
            if like_match:
                reference_structure = like_match.group(1).upper()
                reference_group = like_match.group(2).upper()
                target_group = name.upper()

                if target_group != "ID":
                    group_templates = templates_by_structure.get(reference_structure, {})
                    template_fields = group_templates.get(reference_group)
                    if template_fields:
                        cloned_fields, consumed_length = _clone_template_fields(
                            template_fields=template_fields,
                            start_position=current_position,
                        )
                        current_fields.extend(cloned_fields)
                        current_group_fields.setdefault(target_group, []).extend(cloned_fields)
                        current_position += consumed_length
                        continue

            stack.append(_GroupState(level=level, name=name, description=inline_description))
            continue

        top_level_group = next((group for group in stack if group.level == 10), None)
        top_level_group_name: str | None = None
        if top_level_group is not None:
            if top_level_group.name == "ID":
                continue
            top_level_group_name = top_level_group.name
        elif stack:
            first_group = stack[0]
            if first_group.name == "ID":
                continue
            top_level_group_name = first_group.name

        if parsed_type.length == 0:
            continue

        parent_groups: list[str] = []
        parent_descriptions: list[str] = []
        for group in stack:
            if group.level == 10:
                if group.name != "GS":
                    parent_groups.append(group.name)
                    if group.description:
                        parent_descriptions.append(group.description)
                continue
            parent_groups.append(group.name)
            if group.description:
                parent_descriptions.append(group.description)

        field_name = "_".join([*parent_groups, name]) if parent_groups else name
        description_parts = list(parent_descriptions)
        if inline_description:
            description_parts.append(inline_description)
        field_description = " | ".join(description_parts) if description_parts else None
        start_position = current_position + 1
        current_position += parsed_type.length

        new_field = FieldSpec(
            name=field_name,
            start=start_position,
            length=parsed_type.length,
            type=parsed_type.field_type,
            decimals=parsed_type.decimals,
            description=field_description,
        )
        current_fields.append(new_field)
        group_bucket = top_level_group_name or "__ROOT__"
        current_group_fields.setdefault(group_bucket, []).append(new_field)

    flush_current()
    return list(records_by_name.values())


def extract_contract_from_pli_source(
    source_path: Path,
    source_program: str = "IDP470RA",
    strict: bool = True,
    *,
    structure_prefixes: tuple[str, ...] | None = None,
    structure_names: set[str] | None = None,
    preserve_structure_names: bool = False,
) -> ContractSpec:
    text = source_path.read_text(encoding="latin-1")
    records = _build_record_specs_from_text(
        text,
        structure_prefixes=structure_prefixes,
        structure_names=structure_names,
        preserve_structure_names=preserve_structure_names,
    )
    if not records:
        raise ValueError("No structure found for selected filters in source file.")

    line_lengths = {record.sum_of_lengths for record in records}
    if len(line_lengths) == 1:
        line_length = next(iter(line_lengths))
    else:
        line_length = max(line_lengths)

    return ContractSpec(
        source_program=source_program,
        line_length=line_length,
        strict_length_validation=strict,
        record_types=records,
    )
