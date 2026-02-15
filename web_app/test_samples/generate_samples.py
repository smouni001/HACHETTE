from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web_app.backend.main import _build_contract, _get_flow_profiles


OUT_DIR = Path(__file__).resolve().parent


def _tokenize(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper()) or "X"


def _field_value(field: Any, row_index: int) -> str:
    length = int(field.length)
    if length <= 0:
        return ""

    field_type = str(getattr(field.type, "value", field.type))
    if field_type in {"integer", "decimal"}:
        digit = str((row_index % 9) + 1)
        return (digit * length)[:length]

    token = _tokenize(str(field.name))
    raw = f"{token}{row_index}"
    if len(raw) >= length:
        return raw[:length]
    return raw.ljust(length)


def _render_line(record: Any, line_length: int, row_index: int) -> str:
    buffer = [" "] * line_length
    selector_start = int(record.selector.start) - 1
    selector_len = int(record.selector.length)
    selector_end = selector_start + selector_len
    selector_value = str(record.selector.value)
    for pos, char in enumerate(selector_value[:selector_len]):
        target = selector_start + pos
        if 0 <= target < line_length:
            buffer[target] = char

    for field in sorted(record.fields, key=lambda item: int(item.start)):
        start = int(field.start) - 1
        end = start + int(field.length)
        if start < selector_end and end > selector_start:
            continue
        if start < 0 or start >= line_length:
            continue
        value = _field_value(field, row_index)
        for offset, char in enumerate(value):
            target = start + offset
            if target >= line_length:
                break
            buffer[target] = char
    return "".join(buffer)


def _invoice_sequence(record_by_name: dict[str, Any]) -> list[str]:
    preferred = ["FIC", "ENT", "ECH", "COM", "REF", "ADR", "AD2", "LIG", "LEC", "PIE"]
    selected = [name for name in preferred if name in record_by_name]
    if not selected:
        selected = list(record_by_name.keys())
    # Keep a small but representative file.
    if "COM" in selected:
        selected.remove("COM")
    if "LEC" in selected:
        selected.remove("LEC")
    return selected


def _generic_sequence(records: list[Any]) -> list[Any]:
    # Deduplicate by selector signature for ambiguous contracts.
    seen: set[tuple[int, int, str]] = set()
    output: list[Any] = []
    for record in records:
        key = (int(record.selector.start), int(record.selector.length), str(record.selector.value))
        if key in seen:
            continue
        seen.add(key)
        output.append(record)
    return output


def generate() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profiles = _get_flow_profiles()
    manifest: dict[str, Any] = {"samples": []}

    for (_, _), profile in sorted(profiles.items()):
        if not profile.supports_processing:
            continue

        contract = _build_contract(profile)
        lines: list[str] = []

        if profile.view_mode == "invoice":
            record_by_name = {record.name: record for record in contract.record_types}
            sequence = _invoice_sequence(record_by_name)
            for index, record_name in enumerate(sequence, start=1):
                lines.append(_render_line(record_by_name[record_name], contract.line_length, index))
        else:
            sequence = _generic_sequence(contract.record_types)
            for index, record in enumerate(sequence, start=1):
                lines.append(_render_line(record, contract.line_length, index))

        if not lines:
            continue

        filename = f"sample_{profile.flow_type}_{profile.file_name}.txt"
        output_path = OUT_DIR / filename
        output_path.write_text("\n".join(lines) + "\n", encoding="latin-1")

        manifest["samples"].append(
            {
                "filename": filename,
                "flow_type": profile.flow_type,
                "file_name": profile.file_name,
                "view_mode": profile.view_mode,
                "role_label": profile.role_label,
                "line_count": len(lines),
                "line_length": contract.line_length,
                "record_types": [record.name for record in contract.record_types],
                "raw_structures": list(profile.raw_structures),
            }
        )

    (OUT_DIR / "samples_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    generate()
