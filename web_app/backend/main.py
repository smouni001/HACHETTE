from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from idp470_pipeline.deterministic_extractor import extract_contract_deterministic
from idp470_pipeline.exporters import export_accounting_summary_pdf, export_first_invoice_pdf, export_to_excel
from idp470_pipeline.parsing_engine import FixedWidthParser, save_jsonl

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = Path(os.getenv("IDP470_WEB_SOURCE", PROJECT_ROOT / "IDP470RA.pli")).expanduser()
SPEC_PDF_PATH = Path(
    os.getenv("IDP470_WEB_SPEC_PDF", PROJECT_ROOT / "2785 - DOCTECHN - Dilifac - Format IDIL.pdf")
).expanduser()
LOGO_PATH = Path(os.getenv("IDP470_WEB_LOGO", PROJECT_ROOT / "assets" / "logo_hachette_livre.png")).expanduser()
JOBS_ROOT = Path(os.getenv("IDP470_WEB_JOBS_DIR", PROJECT_ROOT / "web_app" / "jobs")).expanduser()
INPUT_ENCODING = os.getenv("IDP470_WEB_INPUT_ENCODING", "latin-1")
CONTINUE_ON_ERROR = os.getenv("IDP470_WEB_CONTINUE_ON_ERROR", "false").strip().lower() == "true"
REUSE_CONTRACT = os.getenv("IDP470_WEB_REUSE_CONTRACT", "true").strip().lower() == "true"

JOBS_ROOT.mkdir(parents=True, exist_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobState:
    job_id: str
    input_filename: str
    flow_type: str = "output"
    file_name: str = "FICDEMA"
    view_mode: str = "invoice"
    role_label: str = "facturation"
    status: str = "queued"
    progress: int = 2
    message: str = "En attente"
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, int] = field(
        default_factory=lambda: {
            "client_count": 0,
            "invoice_count": 0,
            "line_count": 0,
            "issues_count": 0,
            "records_count": 0,
        }
    )
    kpis: list[dict[str, Any]] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FlowProfile:
    flow_type: str
    file_name: str
    display_name: str
    description: str
    role_label: str = "fichier metier"
    view_mode: str = "generic"
    structure_prefixes: tuple[str, ...] = ()
    structure_names: tuple[str, ...] = ()
    preserve_structure_names: bool = False
    apply_idil_rules: bool = False
    supports_pdf: bool = False
    supports_processing: bool = True
    strict_length_validation: bool = True
    raw_structures: tuple[str, ...] = ()

    @property
    def cache_key(self) -> str:
        return f"{self.flow_type}:{self.file_name}"


_DCL_FILE_RE = re.compile(r"\bDCL\s+([A-Z0-9_]+)\s+FILE\b([^;]*);", re.IGNORECASE)
_WRITE_FILE_RE = re.compile(
    r"\bWRITE\s+FILE\s*\(\s*([A-Z0-9_]+)\s*\)\s+FROM\s*\(?\s*([A-Z0-9_]+)\s*\)?",
    re.IGNORECASE,
)
_READ_FILE_RE = re.compile(
    r"\bREAD\s+FILE\s*\(\s*([A-Z0-9_]+)\s*\)\s+INTO\s*\(?\s*([A-Z0-9_]+)\s*\)?",
    re.IGNORECASE,
)
_BASED_ADDR_RE = re.compile(
    r"\bDCL\s+0?1\s+([A-Z0-9_]+)\b[^;]*\bBASED\s*\(\s*ADDR\s*\(\s*([A-Z0-9_]+)\s*\)\s*\)",
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(r"/\*(.*?)\*/")
_TRAILING_SEQ_RE = re.compile(r"\s+\d{5,}\s*$")


_JOBS: dict[str, JobState] = {}
_JOBS_LOCK = threading.Lock()
_FLOW_PROFILES_CACHE: dict[tuple[str, str], FlowProfile] | None = None
_FLOW_PROFILES_LOCK = threading.Lock()
_CONTRACT_CACHE: dict[str, Any] = {}
_CONTRACT_LOCK = threading.Lock()


def _job_dir(job_id: str) -> Path:
    return JOBS_ROOT / job_id


def _set_job(job_id: str, **updates: Any) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        for key, value in updates.items():
            setattr(job, key, value)
        job.updated_at = _utc_now()


def _get_job_or_404(job_id: str) -> JobState:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job introuvable: {job_id}")
    return job


def _invoice_count(records: list[dict[str, Any]]) -> int:
    ent_invoices = {
        str(record.get("NUFAC", "")).strip()
        for record in records
        if record.get("record_type") == "ENT" and str(record.get("NUFAC", "")).strip()
    }
    if ent_invoices:
        return len(ent_invoices)
    all_invoices = {
        str(record.get("NUFAC", "")).strip()
        for record in records
        if str(record.get("NUFAC", "")).strip()
    }
    return len(all_invoices)


def _client_count(records: list[dict[str, Any]]) -> int:
    ent_clients = {
        str(record.get("NUCLI", "")).strip()
        for record in records
        if record.get("record_type") == "ENT" and str(record.get("NUCLI", "")).strip()
    }
    if ent_clients:
        return len(ent_clients)

    adr_clients = {
        str(record.get("CLLIV_NOCLI", "")).strip()
        for record in records
        if record.get("record_type") == "ADR" and str(record.get("CLLIV_NOCLI", "")).strip()
    }
    if adr_clients:
        return len(adr_clients)

    all_clients = {
        str(record.get("NUCLI", "")).strip()
        for record in records
        if str(record.get("NUCLI", "")).strip()
    }
    all_clients.update(
        {
            str(record.get("CLLIV_NOCLI", "")).strip()
            for record in records
            if str(record.get("CLLIV_NOCLI", "")).strip()
        }
    )
    return len(all_clients)


def _build_kpis(
    *,
    profile: FlowProfile,
    records: list[dict[str, Any]],
    issues: list[Any],
    contract: Any,
) -> list[dict[str, Any]]:
    if profile.view_mode == "invoice":
        return [
            {"key": "clients", "label": "Clients", "value": _client_count(records)},
            {"key": "factures", "label": "Factures", "value": _invoice_count(records)},
            {"key": "lignes", "label": "Lignes fichier", "value": len(records) + len(issues)},
        ]

    record_type_count = len({str(record.get("record_type", "")).strip() for record in records if str(record.get("record_type", "")).strip()})
    field_count = sum(len(record.fields) for record in contract.record_types)
    return [
        {"key": "records", "label": "Enregistrements", "value": len(records)},
        {"key": "types", "label": "Types detectes", "value": record_type_count},
        {"key": "champs", "label": "Champs structures", "value": field_count},
    ]


def _safe_logo_path() -> Path | None:
    if LOGO_PATH.exists():
        return LOGO_PATH
    return None


def _safe_spec_pdf_path() -> Path | None:
    if SPEC_PDF_PATH.exists():
        return SPEC_PDF_PATH
    return None


def _safe_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".jsonl":
        return "application/json"
    if suffix == ".json":
        return "application/json"
    return "application/octet-stream"


def _sample_lines_from_payload(payload: bytes, *, max_lines: int = 250) -> list[str]:
    try:
        decoded = payload.decode(INPUT_ENCODING, errors="replace")
    except Exception:  # noqa: BLE001
        decoded = payload.decode("latin-1", errors="replace")

    lines: list[str] = []
    for raw_line in decoded.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines


def _median_length(lines: list[str]) -> int:
    lengths = sorted(len(line) for line in lines)
    if not lengths:
        return 0
    center = len(lengths) // 2
    if len(lengths) % 2 == 1:
        return lengths[center]
    return (lengths[center - 1] + lengths[center]) // 2


def _selector_match_ratio(lines: list[str], contract: Any) -> float:
    if not lines:
        return 0.0
    selectors: list[tuple[int, int, str]] = [
        (record.selector.start - 1, record.selector.length, record.selector.value)
        for record in contract.record_types
    ]
    matches = 0
    for line in lines:
        for start, length, expected in selectors:
            end = start + length
            if end <= len(line) and line[start:end] == expected:
                matches += 1
                break
    return matches / len(lines)


def _validate_uploaded_payload_for_profile(profile: FlowProfile, payload: bytes) -> None:
    contract = _get_contract(profile)
    lines = _sample_lines_from_payload(payload)
    if not lines:
        raise HTTPException(status_code=400, detail="Le fichier charge ne contient aucune ligne exploitable.")

    median_len = _median_length(lines)
    expected_lengths = sorted({record.max_end for record in contract.record_types})
    max_expected = max(expected_lengths) if expected_lengths else contract.line_length
    closest_gap = min(abs(median_len - expected) for expected in expected_lengths) if expected_lengths else abs(median_len - contract.line_length)
    selector_ratio = _selector_match_ratio(lines, contract)

    if profile.view_mode == "invoice":
        if selector_ratio < 0.5:
            selectors = ", ".join(sorted({record.selector.value for record in contract.record_types}))
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Le fichier charge ne correspond pas au flux {profile.file_name}: "
                    f"signature des enregistrements attendue ({selectors})."
                ),
            )
        if closest_gap > 12:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Le fichier charge ne correspond pas au flux {profile.file_name}: "
                    f"longueur mediane {median_len}, attendu proche de {max_expected}."
                ),
            )
        return

    if len(contract.record_types) == 1:
        tolerance = max(20, int(max_expected * 0.2))
        if selector_ratio < 0.1 and closest_gap > tolerance:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Le fichier charge semble incompatible avec {profile.file_name}: "
                    f"longueur mediane {median_len}, attendu proche de {max_expected}."
                ),
            )
        return

    tolerance = max(24, int(max_expected * 0.25))
    if selector_ratio < 0.1 and closest_gap > tolerance:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Le fichier charge semble incompatible avec {profile.file_name}: "
                "aucune signature d'enregistrement detectee sur l'echantillon."
            ),
        )


def _normalize_source_line(raw_line: str) -> str:
    line = raw_line.rstrip("\r\n")
    line = _TRAILING_SEQ_RE.sub("", line)
    if line and line[0].isdigit():
        line = line[1:]
    return line.rstrip()


def _extract_inline_comment(line: str) -> str | None:
    match = _COMMENT_RE.search(line)
    if not match:
        return None
    cleaned = re.sub(r"\s+", " ", match.group(1) or "").strip()
    return cleaned or None


def _infer_role_label(*, file_name: str, description: str, structures: tuple[str, ...]) -> str:
    upper_desc = description.upper()
    if any(name.startswith("DEMAT_") or name.startswith("STO_D_") for name in structures):
        return "facturation"
    if "LOG" in upper_desc or "JOURNAL" in upper_desc:
        return "journalisation"
    if "INDEX" in upper_desc:
        return "indexation"
    if "HISTORIQUE" in upper_desc:
        return "historisation"
    if "EXPORT" in upper_desc:
        return "export"
    if "STOCK" in upper_desc:
        return "stockage"
    if "FACTURE" in upper_desc or file_name.startswith("FIC"):
        return "facturation"
    if file_name.startswith("ID"):
        return "interface"
    return "fichier metier"


def _contract_can_be_built(
    *,
    strict_length_validation: bool,
    structure_prefixes: tuple[str, ...],
    structure_names: tuple[str, ...],
    preserve_structure_names: bool,
    apply_idil_rules: bool,
) -> bool:
    try:
        extract_contract_deterministic(
            source_path=SOURCE_PATH,
            source_program="IDP470RA",
            strict=strict_length_validation,
            spec_pdf_path=_safe_spec_pdf_path(),
            structure_prefixes=structure_prefixes or None,
            structure_names=set(structure_names) if structure_names else None,
            preserve_structure_names=preserve_structure_names,
            apply_idil_rules=apply_idil_rules,
        )
        return True
    except Exception as error:  # noqa: BLE001
        LOGGER.debug("Profile mapping non exploitable (%s): %s", ",".join(structure_names) or ",".join(structure_prefixes), error)
        return False


def _discover_flow_profiles() -> dict[tuple[str, str], FlowProfile]:
    text = SOURCE_PATH.read_text(encoding="latin-1")
    declarations: dict[str, tuple[str, str]] = {}
    file_to_structures: dict[str, set[str]] = defaultdict(set)
    aliases_by_base: dict[str, set[str]] = defaultdict(set)

    for raw_line in text.splitlines():
        line = _normalize_source_line(raw_line)
        if not line:
            continue

        decl_match = _DCL_FILE_RE.search(line)
        if decl_match:
            file_name = decl_match.group(1).upper()
            tail = decl_match.group(2).upper()
            flow_type = "output" if "OUTPUT" in tail else "input"
            description = _extract_inline_comment(line) or f"Flux {flow_type} {file_name}"
            declarations[file_name] = (flow_type, description)

        for write_match in _WRITE_FILE_RE.finditer(line):
            file_name = write_match.group(1).upper()
            structure_name = write_match.group(2).upper()
            file_to_structures[file_name].add(structure_name)

        for read_match in _READ_FILE_RE.finditer(line):
            file_name = read_match.group(1).upper()
            structure_name = read_match.group(2).upper()
            file_to_structures[file_name].add(structure_name)

        alias_match = _BASED_ADDR_RE.search(line)
        if alias_match:
            alias_name = alias_match.group(1).upper()
            base_name = alias_match.group(2).upper()
            aliases_by_base[base_name].add(alias_name)

    # FFAC3A uses BASED area WTFAC and is not read through INTO syntax.
    file_to_structures.setdefault("FFAC3A", set()).add("WTFAC")

    profiles: dict[tuple[str, str], FlowProfile] = {}
    for file_name, (flow_type, description) in sorted(declarations.items()):
        mapped_structures = set(file_to_structures.get(file_name, set()))
        expanded_structures = set(mapped_structures)
        for structure_name in mapped_structures:
            expanded_structures.update(aliases_by_base.get(structure_name, set()))
        structures = tuple(sorted(expanded_structures))
        invoice_mode = any(name.startswith("DEMAT_") or name.startswith("STO_D_") for name in structures)
        if file_name in {"FICDEMA", "FICSTOD"}:
            invoice_mode = True

        structure_prefixes: tuple[str, ...] = ()
        structure_names: tuple[str, ...] = structures
        preserve_structure_names = True
        apply_idil_rules = False
        strict_length_validation = False
        supports_pdf = False
        role_label = _infer_role_label(file_name=file_name, description=description, structures=structures)
        view_mode = "generic"

        if invoice_mode:
            prefixes: list[str] = []
            if file_name == "FICDEMA" or any(name.startswith("DEMAT_") for name in structures):
                prefixes.append("DEMAT_")
            if file_name == "FICSTOD" or any(name.startswith("STO_D_") for name in structures):
                prefixes.append("STO_D_")
            structure_prefixes = tuple(prefixes)
            structure_names = ()
            preserve_structure_names = False
            apply_idil_rules = True
            strict_length_validation = True
            supports_pdf = True
            role_label = "facturation"
            view_mode = "invoice"

        supports_processing = bool(structure_prefixes or structure_names)
        if supports_processing:
            supports_processing = _contract_can_be_built(
                strict_length_validation=strict_length_validation,
                structure_prefixes=structure_prefixes,
                structure_names=structure_names,
                preserve_structure_names=preserve_structure_names,
                apply_idil_rules=apply_idil_rules,
            )

        profiles[(flow_type, file_name)] = FlowProfile(
            flow_type=flow_type,
            file_name=file_name,
            display_name=file_name,
            description=description,
            role_label=role_label,
            view_mode=view_mode,
            structure_prefixes=structure_prefixes,
            structure_names=structure_names,
            preserve_structure_names=preserve_structure_names,
            apply_idil_rules=apply_idil_rules,
            supports_pdf=supports_pdf,
            supports_processing=supports_processing,
            strict_length_validation=strict_length_validation,
            raw_structures=structures,
        )

    return profiles


def _get_flow_profiles() -> dict[tuple[str, str], FlowProfile]:
    global _FLOW_PROFILES_CACHE
    with _FLOW_PROFILES_LOCK:
        if _FLOW_PROFILES_CACHE is None:
            _FLOW_PROFILES_CACHE = _discover_flow_profiles()
        return _FLOW_PROFILES_CACHE


def _normalize_flow_type(flow_type: str | None) -> str:
    return (flow_type or "").strip().lower()


def _normalize_file_name(file_name: str | None) -> str:
    return (file_name or "").strip().upper()


def _resolve_profile(flow_type: str, file_name: str) -> FlowProfile:
    normalized_flow = _normalize_flow_type(flow_type)
    normalized_file = _normalize_file_name(file_name)
    profiles = _get_flow_profiles()
    profile = profiles.get((normalized_flow, normalized_file))
    if profile is None:
        allowed = ", ".join(
            f"{profile.flow_type}/{profile.file_name}" for profile in profiles.values()
        )
        raise HTTPException(
            status_code=400,
            detail=f"Flux/fichier non supporte. Choix autorises: {allowed}",
        )
    return profile


def _build_contract(profile: FlowProfile):
    return extract_contract_deterministic(
        source_path=SOURCE_PATH,
        source_program="IDP470RA",
        strict=profile.strict_length_validation,
        spec_pdf_path=_safe_spec_pdf_path(),
        structure_prefixes=profile.structure_prefixes or None,
        structure_names=set(profile.structure_names) if profile.structure_names else None,
        preserve_structure_names=profile.preserve_structure_names,
        apply_idil_rules=profile.apply_idil_rules,
    )


def _get_contract(profile: FlowProfile):
    if not REUSE_CONTRACT:
        return _build_contract(profile)

    with _CONTRACT_LOCK:
        cached = _CONTRACT_CACHE.get(profile.cache_key)
        if cached is None:
            cached = _build_contract(profile)
            _CONTRACT_CACHE[profile.cache_key] = cached
        return cached


def _process_job(job_id: str, input_path: Path, profile: FlowProfile) -> None:
    workdir = _job_dir(job_id)
    output_dir = workdir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    try:
        if not SOURCE_PATH.exists():
            raise FileNotFoundError(f"Source programme introuvable: {SOURCE_PATH}")
        if not profile.supports_processing:
            raise ValueError(
                f"Aucune structure exploitable detectee pour {profile.file_name} dans IDP470RA."
            )

        _set_job(job_id, status="running", progress=10, message="Extraction du contrat en cours")
        contract = _get_contract(profile)

        contract_path = output_dir / "idp470ra_contract.json"
        contract_path.write_text(
            json.dumps(contract.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        _set_job(job_id, progress=35, message=f"Parsing {profile.file_name} en cours")
        parser = FixedWidthParser(contract)
        records, issues = parser.parse_file(
            input_path=input_path,
            encoding=INPUT_ENCODING,
            continue_on_error=CONTINUE_ON_ERROR,
        )

        parsed_path = output_dir / "parsed_records.jsonl"
        save_jsonl(records=records, output_path=parsed_path)

        _set_job(
            job_id,
            progress=55,
            message="Generation Excel en cours",
        )
        excel_path = output_dir / "parsed_records.xlsx"
        export_to_excel(
            records=records,
            output_path=excel_path,
            contract=contract,
            metadata={
                "title": "IDIL PAPYRUS - Synthese de traitement",
                "flow_type": profile.flow_type.upper(),
                "file_name": profile.file_name,
                "view_mode": profile.view_mode,
                "role_label": profile.role_label,
            },
        )

        pdf_factures_path = output_dir / "facture_exemple.pdf"
        pdf_synthese_path = output_dir / "synthese_comptable.pdf"
        if profile.supports_pdf:
            _set_job(job_id, progress=75, message="Generation PDF factures en cours")
            try:
                export_first_invoice_pdf(records=records, output_path=pdf_factures_path, logo_path=_safe_logo_path())
            except Exception as error:  # noqa: BLE001
                warnings.append(f"PDF factures non genere: {error}")

            _set_job(job_id, progress=90, message="Generation PDF synthese en cours")
            try:
                export_accounting_summary_pdf(
                    records=records,
                    output_path=pdf_synthese_path,
                    logo_path=_safe_logo_path(),
                )
            except Exception as error:  # noqa: BLE001
                warnings.append(f"PDF synthese non genere: {error}")
        else:
            _set_job(job_id, progress=90, message="Generation terminee pour ce flux")

        outputs: dict[str, str] = {
            "contract": str(contract_path),
            "jsonl": str(parsed_path),
            "excel": str(excel_path),
        }
        if pdf_factures_path.exists():
            outputs["pdf_factures"] = str(pdf_factures_path)
        if pdf_synthese_path.exists():
            outputs["pdf_synthese"] = str(pdf_synthese_path)

        metrics = {
            "client_count": _client_count(records),
            "invoice_count": _invoice_count(records),
            "line_count": len(records) + len(issues),
            "issues_count": len(issues),
            "records_count": len(records),
        }
        kpis = _build_kpis(profile=profile, records=records, issues=issues, contract=contract)

        _set_job(
            job_id,
            status="completed",
            progress=100,
            message="Extraction terminee avec succes",
            warnings=warnings,
            metrics=metrics,
            kpis=kpis,
            outputs=outputs,
        )
    except Exception as error:  # noqa: BLE001
        _set_job(
            job_id,
            status="failed",
            progress=100,
            message="Echec du traitement",
            error=str(error),
            warnings=warnings,
        )
        LOGGER.exception("Job %s failed", job_id)


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    message: str
    flow_type: str
    file_name: str
    view_mode: str
    role_label: str


class JobStatusResponse(BaseModel):
    job_id: str
    flow_type: str
    file_name: str
    view_mode: str
    role_label: str
    status: str
    progress: int = Field(ge=0, le=100)
    message: str
    input_filename: str
    created_at: str
    updated_at: str
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, int] = Field(default_factory=dict)
    kpis: list[dict[str, Any]] = Field(default_factory=list)
    downloads: dict[str, str] = Field(default_factory=dict)


class CatalogProfileResponse(BaseModel):
    flow_type: str
    file_name: str
    display_name: str
    description: str
    role_label: str
    view_mode: str
    supports_processing: bool
    supports_pdf: bool
    raw_structures: list[str] = Field(default_factory=list)


class CatalogResponse(BaseModel):
    source_program: str
    default_flow_type: str
    default_file_name: str
    profiles: list[CatalogProfileResponse]


def _download_links(job_id: str, job: JobState) -> dict[str, str]:
    links: dict[str, str] = {}
    if "excel" in job.outputs:
        links["excel"] = f"/api/jobs/{job_id}/download/excel"
    if "pdf_factures" in job.outputs:
        links["pdf_factures"] = f"/api/jobs/{job_id}/download/pdf-factures"
    if "pdf_synthese" in job.outputs:
        links["pdf_synthese"] = f"/api/jobs/{job_id}/download/pdf-synthese"
    if "jsonl" in job.outputs:
        links["jsonl"] = f"/api/jobs/{job_id}/download/jsonl"
    if "contract" in job.outputs:
        links["contract"] = f"/api/jobs/{job_id}/download/contract"
    return links


def _to_status_response(job_id: str, job: JobState) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=job_id,
        flow_type=job.flow_type,
        file_name=job.file_name,
        view_mode=job.view_mode,
        role_label=job.role_label,
        status=job.status,
        progress=job.progress,
        message=job.message,
        input_filename=job.input_filename,
        created_at=job.created_at,
        updated_at=job.updated_at,
        error=job.error,
        warnings=job.warnings,
        metrics=job.metrics,
        kpis=job.kpis,
        downloads=_download_links(job_id, job),
    )


app = FastAPI(
    title="IDIL PAPYRUS FACTURE DEMAT API",
    version="1.0.0",
    description="API web pour extraction Mainframe dynamique vers Excel et PDF.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "source_program": str(SOURCE_PATH),
    }


@app.get("/api/catalog", response_model=CatalogResponse)
def catalog() -> CatalogResponse:
    profiles_map = _get_flow_profiles()
    profiles = [
        CatalogProfileResponse(
            flow_type=profile.flow_type,
            file_name=profile.file_name,
            display_name=profile.display_name,
            description=profile.description,
            role_label=profile.role_label,
            view_mode=profile.view_mode,
            supports_processing=profile.supports_processing,
            supports_pdf=profile.supports_pdf,
            raw_structures=list(profile.raw_structures),
        )
        for profile in sorted(profiles_map.values(), key=lambda item: (item.flow_type, item.file_name))
    ]
    default_profile = profiles_map.get(("output", "FICDEMA"))
    if default_profile is None and profiles:
        first = profiles[0]
        default_flow_type = first.flow_type
        default_file_name = first.file_name
    else:
        default_flow_type = default_profile.flow_type if default_profile else "output"
        default_file_name = default_profile.file_name if default_profile else "FICDEMA"
    return CatalogResponse(
        source_program="IDP470RA",
        default_flow_type=default_flow_type,
        default_file_name=default_file_name,
        profiles=profiles,
    )


@app.post("/api/jobs", response_model=JobCreateResponse)
async def create_job(
    background_tasks: BackgroundTasks,
    data_file: UploadFile | None = File(default=None),
    facdema_file: UploadFile | None = File(default=None),
    flow_type: str = Form(default="output"),
    file_name: str = Form(default="FICDEMA"),
) -> JobCreateResponse:
    profile = _resolve_profile(flow_type=flow_type, file_name=file_name)
    if not profile.supports_processing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Le fichier {profile.file_name} est detecte dans IDP470RA mais aucun mapping "
                "structurel exploitable n'a ete trouve."
            ),
        )
    uploaded_file = data_file or facdema_file

    if uploaded_file is None or not uploaded_file.filename:
        raise HTTPException(status_code=400, detail=f"Nom de fichier {profile.file_name} manquant.")

    suffix = Path(uploaded_file.filename).suffix.lower()
    if suffix not in {".txt", ".dat"}:
        raise HTTPException(status_code=400, detail="Format autorise: .txt ou .dat")

    payload = await uploaded_file.read()
    if not payload:
        raise HTTPException(status_code=400, detail=f"Le fichier {profile.file_name} est vide.")

    _validate_uploaded_payload_for_profile(profile, payload)

    job_id = uuid.uuid4().hex[:12]
    safe_name = Path(uploaded_file.filename).name
    input_dir = _job_dir(job_id) / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / safe_name
    input_path.write_bytes(payload)

    with _JOBS_LOCK:
        _JOBS[job_id] = JobState(
            job_id=job_id,
            input_filename=safe_name,
            flow_type=profile.flow_type,
            file_name=profile.file_name,
            view_mode=profile.view_mode,
            role_label=profile.role_label,
        )

    background_tasks.add_task(_process_job, job_id, input_path, profile)
    return JobCreateResponse(
        job_id=job_id,
        status="queued",
        message="Traitement lance",
        flow_type=profile.flow_type,
        file_name=profile.file_name,
        view_mode=profile.view_mode,
        role_label=profile.role_label,
    )


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str) -> JobStatusResponse:
    job = _get_job_or_404(job_id)
    return _to_status_response(job_id, job)


@app.get("/api/jobs/{job_id}/download/{artifact}")
def download_artifact(job_id: str, artifact: str) -> FileResponse:
    artifact_map = {
        "excel": "excel",
        "pdf-factures": "pdf_factures",
        "pdf-synthese": "pdf_synthese",
        "jsonl": "jsonl",
        "contract": "contract",
    }
    output_key = artifact_map.get(artifact)
    if output_key is None:
        raise HTTPException(status_code=404, detail=f"Artifact inconnu: {artifact}")

    job = _get_job_or_404(job_id)
    output_path_str = job.outputs.get(output_key)
    if not output_path_str:
        raise HTTPException(status_code=404, detail=f"Artifact indisponible pour ce job: {artifact}")

    output_path = Path(output_path_str)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail=f"Fichier introuvable: {output_path.name}")

    return FileResponse(
        output_path,
        filename=output_path.name,
        media_type=_safe_media_type(output_path),
    )


assets_dir = PROJECT_ROOT / "assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

frontend_dir = PROJECT_ROOT / "web_app" / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_app.backend.main:app", host="0.0.0.0", port=8000, reload=True)
