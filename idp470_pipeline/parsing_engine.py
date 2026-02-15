from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .models import ContractSpec, FieldSpec, FieldType, RecordSpec

LOGGER = logging.getLogger(__name__)


class ParsingError(RuntimeError):
    pass


class ContractValidationError(RuntimeError):
    pass


@dataclass
class ParseIssue:
    line_number: int
    message: str
    raw_line: str


def _normalize_numeric(value: str) -> str:
    return value.replace(" ", "").replace(",", ".")


def _coerce_value(raw_value: str, field: FieldSpec) -> Any:
    value = raw_value.rstrip()

    if field.type in {FieldType.STRING, FieldType.DATE, FieldType.SIGN}:
        return value

    normalized = _normalize_numeric(value)
    if normalized == "":
        return None

    try:
        if field.type == FieldType.INTEGER:
            return int(normalized)
        if field.type == FieldType.DECIMAL:
            decimals = field.decimals or 0
            numeric = Decimal(normalized)
            if "." not in normalized:
                numeric = numeric / (Decimal(10) ** decimals)
            return numeric
    except (ValueError, InvalidOperation):
        return value

    return value


class FixedWidthParser:
    def __init__(self, contract: ContractSpec) -> None:
        self.contract = contract
        self._validate_contract()

    def _validate_contract(self) -> None:
        for record in self.contract.record_types:
            if record.max_end > self.contract.line_length:
                raise ContractValidationError(
                    f"Enregistrement {record.name}: max_end={record.max_end} > line_length={self.contract.line_length}"
                )
            if self.contract.strict_length_validation:
                if record.sum_of_lengths != self.contract.line_length:
                    raise ContractValidationError(
                        f"Enregistrement {record.name}: somme(longueurs)={record.sum_of_lengths}, "
                        f"attendu={self.contract.line_length}"
                    )

    def _record_for_line(self, line: str) -> RecordSpec | None:
        for record in self.contract.record_types:
            start = record.selector.start - 1
            end = start + record.selector.length
            if line[start:end] == record.selector.value:
                return record
        if len(self.contract.record_types) == 1:
            # Fallback for single-layout files where the selector token is not present in data.
            return self.contract.record_types[0]
        return None

    def parse_line(self, line: str, line_number: int) -> dict[str, Any]:
        effective_line = line
        if len(line) != self.contract.line_length:
            if self.contract.strict_length_validation:
                raise ParsingError(
                    f"Ligne {line_number}: longueur={len(line)} attendue={self.contract.line_length}"
                )
            if len(line) < self.contract.line_length:
                effective_line = line.ljust(self.contract.line_length)
            else:
                effective_line = line[: self.contract.line_length]

        record = self._record_for_line(effective_line)
        if record is None:
            raise ParsingError(
                f"Ligne {line_number}: type d'enregistrement inconnu aux positions configurees."
            )

        output: dict[str, Any] = {"record_type": record.name, "line_number": line_number}
        for field in record.fields:
            start = field.start - 1
            end = start + field.length
            raw_value = effective_line[start:end]
            output[field.name] = _coerce_value(raw_value=raw_value, field=field)

        return output

    def _structure_rule(self, label: str):
        for rule in self.contract.structure_rules:
            if rule.label == label:
                return rule
        return None

    def _check_occurrence(
        self,
        *,
        issues: list[ParseIssue],
        label: str,
        count: int,
        line_number: int,
    ) -> None:
        rule = self._structure_rule(label)
        if rule is None:
            return
        if count < rule.min_occurs:
            issues.append(
                ParseIssue(
                    line_number=line_number,
                    message=(
                        f"Regle de structure non respectee [{label}]: minimum "
                        f"{rule.min_occurs}, trouve {count}."
                    ),
                    raw_line="",
                )
            )
        if rule.max_occurs is not None and count > rule.max_occurs:
            issues.append(
                ParseIssue(
                    line_number=line_number,
                    message=(
                        f"Regle de structure non respectee [{label}]: maximum "
                        f"{rule.max_occurs}, trouve {count}."
                    ),
                    raw_line="",
                )
            )

    def _validate_structure(self, records: list[dict[str, Any]]) -> list[ParseIssue]:
        if not self.contract.structure_rules:
            return []

        issues: list[ParseIssue] = []
        if not records:
            issues.append(ParseIssue(line_number=0, message="Aucun enregistrement parse.", raw_line=""))
            return issues

        fic_positions = [record.get("line_number", 0) for record in records if record.get("record_type") == "FIC"]
        self._check_occurrence(
            issues=issues,
            label="FIC",
            count=len(fic_positions),
            line_number=fic_positions[0] if fic_positions else 0,
        )
        if fic_positions and records[0].get("record_type") != "FIC":
            issues.append(
                ParseIssue(
                    line_number=int(records[0].get("line_number", 0) or 0),
                    message="Ordre de structure non respecte: le premier enregistrement doit etre FIC.",
                    raw_line="",
                )
            )

        invoice_blocks: list[list[dict[str, Any]]] = []
        current_block: list[dict[str, Any]] | None = None

        for record in records:
            record_type = str(record.get("record_type", ""))
            if record_type == "FIC":
                continue
            if record_type == "ENT":
                if current_block:
                    invoice_blocks.append(current_block)
                current_block = [record]
                continue

            if current_block is None:
                issues.append(
                    ParseIssue(
                        line_number=int(record.get("line_number", 0) or 0),
                        message=(
                            "Ordre de structure non respecte: enregistrement avant le premier ENT "
                            f"({record_type})."
                        ),
                        raw_line="",
                    )
                )
                continue
            current_block.append(record)

        if current_block:
            invoice_blocks.append(current_block)

        if not invoice_blocks:
            issues.append(
                ParseIssue(
                    line_number=0,
                    message="Regle de structure non respectee [ENT]: aucun bloc facture trouve.",
                    raw_line="",
                )
            )
            return issues

        order_by_label = {
            "ENT": 2,
            "ECH": 3,
            "COM": 4,
            "REF(E)": 5,
            "ADR": 6,
            "AD2": 7,
            "LIG": 8,
            "REF(L)": 9,
            "LEC": 10,
            "PIE": 11,
        }

        for block in invoice_blocks:
            ent_line = int(block[0].get("line_number", 0) or 0)
            counts = Counter(str(record.get("record_type", "")) for record in block)
            header_ref_count = 0
            line_segments: list[dict[str, Any]] = []
            current_line: dict[str, Any] | None = None
            seen_lig = False
            last_order = order_by_label["ENT"]

            for record in block[1:]:
                record_type = str(record.get("record_type", ""))
                line_number = int(record.get("line_number", 0) or 0)

                if record_type == "LIG":
                    seen_lig = True
                    if current_line is not None:
                        line_segments.append(current_line)
                    current_line = {"line_number": line_number, "ref": 0, "lec": 0}
                    current_order = order_by_label["LIG"]
                    if last_order not in {order_by_label["LIG"], order_by_label["REF(L)"], order_by_label["LEC"]}:
                        if current_order < last_order:
                            issues.append(
                                ParseIssue(
                                    line_number=line_number,
                                    message="Ordre de structure non respecte dans le bloc facture autour de LIG.",
                                    raw_line="",
                                )
                            )
                    last_order = current_order
                    continue

                if record_type == "REF":
                    if seen_lig:
                        current_order = order_by_label["REF(L)"]
                        if current_line is None:
                            issues.append(
                                ParseIssue(
                                    line_number=line_number,
                                    message="Regle de structure non respectee [REF(L)]: REF sans LIG parent.",
                                    raw_line="",
                                )
                            )
                        else:
                            current_line["ref"] += 1
                    else:
                        current_order = order_by_label["REF(E)"]
                        header_ref_count += 1
                elif record_type == "LEC":
                    current_order = order_by_label["LEC"]
                    if current_line is None:
                        issues.append(
                            ParseIssue(
                                line_number=line_number,
                                message="Regle de structure non respectee [LEC]: LEC sans LIG parent.",
                                raw_line="",
                            )
                        )
                    else:
                        current_line["lec"] += 1
                elif record_type == "ECH":
                    current_order = order_by_label["ECH"]
                elif record_type == "COM":
                    current_order = order_by_label["COM"]
                elif record_type == "ADR":
                    current_order = order_by_label["ADR"]
                elif record_type == "AD2":
                    current_order = order_by_label["AD2"]
                elif record_type == "PIE":
                    current_order = order_by_label["PIE"]
                else:
                    continue

                if current_order < last_order:
                    issues.append(
                        ParseIssue(
                            line_number=line_number,
                            message=(
                                "Ordre de structure non respecte dans le bloc facture: "
                                f"{record_type} hors sequence attendue."
                            ),
                            raw_line="",
                        )
                    )
                else:
                    last_order = current_order

            if current_line is not None:
                line_segments.append(current_line)

            self._check_occurrence(issues=issues, label="ENT", count=1, line_number=ent_line)
            self._check_occurrence(
                issues=issues,
                label="ECH",
                count=counts.get("ECH", 0),
                line_number=ent_line,
            )
            self._check_occurrence(
                issues=issues,
                label="COM",
                count=counts.get("COM", 0),
                line_number=ent_line,
            )
            self._check_occurrence(
                issues=issues,
                label="REF(E)",
                count=header_ref_count,
                line_number=ent_line,
            )
            self._check_occurrence(
                issues=issues,
                label="ADR",
                count=counts.get("ADR", 0),
                line_number=ent_line,
            )
            self._check_occurrence(
                issues=issues,
                label="AD2",
                count=counts.get("AD2", 0),
                line_number=ent_line,
            )
            self._check_occurrence(
                issues=issues,
                label="LIG",
                count=counts.get("LIG", 0),
                line_number=ent_line,
            )
            self._check_occurrence(
                issues=issues,
                label="PIE",
                count=counts.get("PIE", 0),
                line_number=ent_line,
            )

            for segment in line_segments:
                self._check_occurrence(
                    issues=issues,
                    label="REF(L)",
                    count=int(segment["ref"]),
                    line_number=int(segment["line_number"]),
                )
                self._check_occurrence(
                    issues=issues,
                    label="LEC",
                    count=int(segment["lec"]),
                    line_number=int(segment["line_number"]),
                )

        return issues

    def parse_file(
        self,
        input_path: Path,
        encoding: str = "latin-1",
        continue_on_error: bool = False,
    ) -> tuple[list[dict[str, Any]], list[ParseIssue]]:
        records: list[dict[str, Any]] = []
        issues: list[ParseIssue] = []

        with input_path.open("r", encoding=encoding, newline="") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\r\n")
                try:
                    records.append(self.parse_line(line=line, line_number=line_number))
                except ParsingError as error:
                    issue = ParseIssue(line_number=line_number, message=str(error), raw_line=line)
                    issues.append(issue)
                    if not continue_on_error:
                        raise

        if issues:
            LOGGER.warning("Parsing termine avec %s anomalie(s).", len(issues))

        structure_issues = self._validate_structure(records)
        if structure_issues:
            issues.extend(structure_issues)
            LOGGER.warning("Validation de structure terminee avec %s anomalie(s).", len(structure_issues))
            if self.contract.strict_structure_validation and not continue_on_error:
                raise ParsingError(structure_issues[0].message)

        return records, issues


def save_jsonl(records: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, default=str))
            handle.write("\n")


def load_jsonl(input_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with input_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                records.append(json.loads(stripped))
    return records
