from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import ContractSpec, StructureRule, StructureScope

LOGGER = logging.getLogger(__name__)

_DEFAULT_SPEC_FILENAME = "2785 - DOCTECHN - Dilifac - Format IDIL.pdf"
_DEFAULT_STRUCTURE_SOURCE = "DOCTECHN IDIL section 3.2"


def _build_default_rules() -> list[StructureRule]:
    return [
        StructureRule(
            label="FIC",
            record_name="FIC",
            scope=StructureScope.FILE,
            min_occurs=1,
            max_occurs=1,
            order_index=1,
            description="En-tete de fichier",
        ),
        StructureRule(
            label="ENT",
            record_name="ENT",
            scope=StructureScope.INVOICE,
            min_occurs=1,
            max_occurs=1,
            order_index=2,
            description="En-tete de facture",
        ),
        StructureRule(
            label="ECH",
            record_name="ECH",
            scope=StructureScope.INVOICE,
            min_occurs=1,
            max_occurs=None,
            order_index=3,
            description="Dates d'echeance",
        ),
        StructureRule(
            label="COM",
            record_name="COM",
            scope=StructureScope.INVOICE,
            min_occurs=0,
            max_occurs=None,
            order_index=4,
            description="Commentaires libres",
        ),
        StructureRule(
            label="REF(E)",
            record_name="REF",
            scope=StructureScope.INVOICE,
            min_occurs=0,
            max_occurs=None,
            order_index=5,
            description="References de facture",
        ),
        StructureRule(
            label="ADR",
            record_name="ADR",
            scope=StructureScope.INVOICE,
            min_occurs=1,
            max_occurs=1,
            order_index=6,
            description="Ligne d'adresse",
        ),
        StructureRule(
            label="AD2",
            record_name="AD2",
            scope=StructureScope.INVOICE,
            min_occurs=1,
            max_occurs=1,
            order_index=7,
            description="Complement ligne d'adresse",
        ),
        StructureRule(
            label="LIG",
            record_name="LIG",
            scope=StructureScope.INVOICE,
            min_occurs=1,
            max_occurs=None,
            order_index=8,
            description="Ligne de facture",
        ),
        StructureRule(
            label="REF(L)",
            record_name="REF",
            scope=StructureScope.LINE,
            min_occurs=0,
            max_occurs=None,
            order_index=9,
            description="Reference ligne",
        ),
        StructureRule(
            label="LEC",
            record_name="LEC",
            scope=StructureScope.LINE,
            min_occurs=0,
            max_occurs=None,
            order_index=10,
            description="Echeance ligne",
        ),
        # PIE exists in IDP470RA output and is handled as optional in invoice scope.
        StructureRule(
            label="PIE",
            record_name="PIE",
            scope=StructureScope.INVOICE,
            min_occurs=0,
            max_occurs=None,
            order_index=11,
            description="Pied de facture / recapitulatif TVA",
        ),
    ]


def _resolve_default_pdf_path() -> Path | None:
    candidates = [
        Path.cwd() / _DEFAULT_SPEC_FILENAME,
        Path.cwd().parent / _DEFAULT_SPEC_FILENAME,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _read_pdf_text(pdf_path: Path) -> str | None:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:
        LOGGER.warning("pypdf not installed: skipping IDIL structure PDF verification.")
        return None

    try:
        reader = PdfReader(str(pdf_path))
        chunks: list[str] = []
        for page in reader.pages[:10]:
            chunks.append(page.extract_text() or "")
        return "\n".join(chunks)
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("Unable to read structure PDF %s: %s", pdf_path, error)
        return None


def _verify_table_labels(pdf_text: str) -> set[str]:
    expected_patterns = {
        "FIC": r"\bFIC\b",
        "ENT": r"\bENT\b",
        "ECH": r"\bECH\b",
        "COM": r"\bCOM\b",
        "REF(E)": r"REF\s*\(E\)",
        "ADR": r"\bADR\b",
        "AD2": r"\bAD2\b",
        "LIG": r"\bLIG\b",
        "REF(L)": r"REF\s*\(L\)",
        "LEC": r"\bLEC\b",
    }
    found: set[str] = set()
    for label, pattern in expected_patterns.items():
        if re.search(pattern, pdf_text, flags=re.IGNORECASE):
            found.add(label)
    return found


def attach_idil_structure_rules(
    contract: ContractSpec,
    spec_pdf_path: Path | None = None,
) -> ContractSpec:
    resolved_pdf = spec_pdf_path
    if resolved_pdf is None:
        resolved_pdf = _resolve_default_pdf_path()

    if resolved_pdf is not None and resolved_pdf.exists():
        pdf_text = _read_pdf_text(resolved_pdf)
        if pdf_text:
            found = _verify_table_labels(pdf_text)
            expected = {"FIC", "ENT", "ECH", "COM", "REF(E)", "ADR", "AD2", "LIG", "REF(L)", "LEC"}
            missing = sorted(expected - found)
            if missing:
                LOGGER.warning(
                    "Structure table labels missing in PDF verification: %s",
                    ", ".join(missing),
                )
        contract.structure_source = str(resolved_pdf)
    else:
        if spec_pdf_path is not None:
            LOGGER.warning("IDIL structure PDF not found: %s", spec_pdf_path)
        contract.structure_source = _DEFAULT_STRUCTURE_SOURCE

    contract.structure_rules = _build_default_rules()
    return contract
