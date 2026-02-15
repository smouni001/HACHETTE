from __future__ import annotations

import logging
from pathlib import Path

from .idil_structure_rules import attach_idil_structure_rules
from .models import ContractSpec
from .pli_layout_parser import extract_contract_from_pli_source

LOGGER = logging.getLogger(__name__)


def extract_contract_deterministic(
    source_path: Path,
    source_program: str = "IDP470RA",
    strict: bool = True,
    spec_pdf_path: Path | None = None,
    *,
    structure_prefixes: tuple[str, ...] | None = None,
    structure_names: set[str] | None = None,
    preserve_structure_names: bool = False,
    apply_idil_rules: bool = True,
) -> ContractSpec:
    if source_path.suffix.lower() == ".pdf":
        raise ValueError(
            "The main source must be IDP470RA.pli. Use the PDF only as structure reference."
        )

    LOGGER.info("Deterministic extraction from source code (main): %s", source_path)
    contract = extract_contract_from_pli_source(
        source_path=source_path,
        source_program=source_program,
        strict=strict,
        structure_prefixes=structure_prefixes,
        structure_names=structure_names,
        preserve_structure_names=preserve_structure_names,
    )
    if apply_idil_rules:
        return attach_idil_structure_rules(contract=contract, spec_pdf_path=spec_pdf_path)
    return contract
