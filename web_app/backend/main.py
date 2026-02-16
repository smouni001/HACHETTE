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
from idp470_pipeline.models import ContractSpec, FieldSpec, FieldType, RecordSpec, SelectorSpec
from idp470_pipeline.parsing_engine import FixedWidthParser, save_jsonl

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_DIR = Path(os.getenv("IDP470_WEB_PROGRAMS_DIR", PROJECT_ROOT / "web_app" / "programs")).expanduser()
DEFAULT_SOURCE_PATH = Path(os.getenv("IDP470_WEB_SOURCE", PROJECT_ROOT / "IDP470RA.pli")).expanduser()
DEFAULT_SOURCE_PROGRAM = os.getenv("IDP470_WEB_SOURCE_PROGRAM", "IDP470RA").strip() or "IDP470RA"
DEFAULT_SPEC_PDF_PATH = Path(
    os.getenv("IDP470_WEB_SPEC_PDF", PROJECT_ROOT / "2785 - DOCTECHN - Dilifac - Format IDIL.pdf")
).expanduser()
LOGO_PATH = Path(os.getenv("IDP470_WEB_LOGO", PROJECT_ROOT / "assets" / "logo_hachette_livre.png")).expanduser()
JOBS_ROOT = Path(os.getenv("IDP470_WEB_JOBS_DIR", PROJECT_ROOT / "web_app" / "jobs")).expanduser()
LOCAL_PROGRAMS_ROOT = JOBS_ROOT / "_program_sources"
DEFAULT_INPUT_ENCODING = os.getenv("IDP470_WEB_INPUT_ENCODING", "latin-1")
DEFAULT_CONTINUE_ON_ERROR = os.getenv("IDP470_WEB_CONTINUE_ON_ERROR", "false").strip().lower() == "true"
DEFAULT_REUSE_CONTRACT = os.getenv("IDP470_WEB_REUSE_CONTRACT", "true").strip().lower() == "true"
SUPPORTED_ANALYZERS = {"idp470_pli"}
ALLOWED_SOURCE_SUFFIXES = {".pli", ".cbl", ".jcl", ".txt"}

JOBS_ROOT.mkdir(parents=True, exist_ok=True)
LOCAL_PROGRAMS_ROOT.mkdir(parents=True, exist_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobState:
    job_id: str
    input_filename: str
    program_id: str = "idp470ra"
    program_display_name: str = "IDIL470 PROJET PAPYRUS"
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
    program_id: str
    source_program: str
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
        return f"{self.program_id}:{self.flow_type}:{self.file_name}"


class ProgramSourceConfig(BaseModel):
    path: str
    program_name: str = "IDP470RA"
    encoding: str = "latin-1"


class ProgramAnalyzerConfig(BaseModel):
    engine: str = "idp470_pli"
    spec_pdf_path: str | None = None


class ProgramUiDefaultsConfig(BaseModel):
    invoice_only: bool = True
    default_flow_type: str = "output"
    default_file_name: str = "FICDEMA"


class ProgramDefinitionConfig(BaseModel):
    program_id: str
    display_name: str
    description: str = ""
    source: ProgramSourceConfig
    analyzer: ProgramAnalyzerConfig = Field(default_factory=ProgramAnalyzerConfig)
    ui_defaults: ProgramUiDefaultsConfig = Field(default_factory=ProgramUiDefaultsConfig)
    continue_on_error: bool = DEFAULT_CONTINUE_ON_ERROR
    reuse_contract: bool = DEFAULT_REUSE_CONTRACT


@dataclass(frozen=True)
class ProgramRuntime:
    program_id: str
    display_name: str
    description: str
    source_program: str
    source_path: Path
    source_encoding: str
    analyzer_engine: str
    spec_pdf_path: Path | None
    invoice_only_default: bool
    default_flow_type: str
    default_file_name: str
    continue_on_error: bool
    reuse_contract: bool


_DCL_FILE_RE = re.compile(r"\bDCL\s+([A-Z0-9_]+)\s+FILE\b([^;]*);", re.IGNORECASE)
_FILE_RECSIZE_RE = re.compile(r"\b(?:RECSIZE|BLKSIZE)\s*\(\s*(\d+)\s*\)", re.IGNORECASE)
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
_PROGRAMS_CACHE: dict[str, ProgramRuntime] | None = None
_PROGRAMS_LOCK = threading.Lock()
_FLOW_PROFILES_CACHE: dict[str, dict[tuple[str, str], FlowProfile]] = {}
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


def _resolve_project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def _default_program_runtime() -> ProgramRuntime:
    spec_pdf = DEFAULT_SPEC_PDF_PATH if DEFAULT_SPEC_PDF_PATH.exists() else None
    return ProgramRuntime(
        program_id="idp470ra",
        display_name="IDIL470 PROJET PAPYRUS",
        description="Programme principal de traitement facturation.",
        source_program=DEFAULT_SOURCE_PROGRAM,
        source_path=DEFAULT_SOURCE_PATH,
        source_encoding=DEFAULT_INPUT_ENCODING,
        analyzer_engine="idp470_pli",
        spec_pdf_path=spec_pdf,
        invoice_only_default=True,
        default_flow_type="output",
        default_file_name="FICDEMA",
        continue_on_error=DEFAULT_CONTINUE_ON_ERROR,
        reuse_contract=DEFAULT_REUSE_CONTRACT,
    )


def _load_program_registry() -> dict[str, ProgramRuntime]:
    runtimes: dict[str, ProgramRuntime] = {}
    if PROGRAMS_DIR.exists():
        for config_path in sorted(PROGRAMS_DIR.glob("*.json")):
            if config_path.name.endswith(".example.json"):
                continue
            try:
                payload = json.loads(config_path.read_text(encoding="utf-8"))
                config = ProgramDefinitionConfig.model_validate(payload)
                engine = config.analyzer.engine.strip().lower()
                if engine not in SUPPORTED_ANALYZERS:
                    LOGGER.warning(
                        "Programme ignore (%s): moteur non supporte '%s'.",
                        config_path.name,
                        engine,
                    )
                    continue
                program_id = config.program_id.strip().lower()
                runtime = ProgramRuntime(
                    program_id=program_id,
                    display_name=config.display_name.strip() or config.program_id.strip(),
                    description=config.description.strip(),
                    source_program=config.source.program_name.strip() or config.program_id.strip(),
                    source_path=_resolve_project_path(config.source.path),
                    source_encoding=config.source.encoding.strip() or DEFAULT_INPUT_ENCODING,
                    analyzer_engine=engine,
                    spec_pdf_path=_resolve_project_path(config.analyzer.spec_pdf_path)
                    if config.analyzer.spec_pdf_path
                    else None,
                    invoice_only_default=config.ui_defaults.invoice_only,
                    default_flow_type=config.ui_defaults.default_flow_type.strip().lower() or "output",
                    default_file_name=config.ui_defaults.default_file_name.strip().upper() or "FICDEMA",
                    continue_on_error=config.continue_on_error,
                    reuse_contract=config.reuse_contract,
                )
                runtimes[program_id] = runtime
            except Exception as error:  # noqa: BLE001
                LOGGER.warning("Programme ignore (%s): %s", config_path.name, error)

    if not runtimes:
        fallback = _default_program_runtime()
        runtimes[fallback.program_id] = fallback
    return runtimes


def _get_programs() -> dict[str, ProgramRuntime]:
    global _PROGRAMS_CACHE
    with _PROGRAMS_LOCK:
        if _PROGRAMS_CACHE is None:
            _PROGRAMS_CACHE = _load_program_registry()
        return _PROGRAMS_CACHE


def _get_default_program() -> ProgramRuntime:
    programs = _get_programs()
    preferred = programs.get("idp470ra")
    if preferred is not None:
        return preferred
    return next(iter(programs.values()))


def _resolve_program(program_id: str | None) -> ProgramRuntime:
    if not program_id:
        return _get_default_program()
    normalized = program_id.strip().lower()
    programs = _get_programs()
    runtime = programs.get(normalized)
    if runtime is None:
        allowed = ", ".join(sorted(programs.keys()))
        raise HTTPException(status_code=400, detail=f"Programme inconnu '{program_id}'. Programmes autorises: {allowed}")
    return runtime


def _register_runtime(runtime: ProgramRuntime) -> None:
    global _PROGRAMS_CACHE
    with _PROGRAMS_LOCK:
        if _PROGRAMS_CACHE is None:
            _PROGRAMS_CACHE = _load_program_registry()
        _PROGRAMS_CACHE[runtime.program_id] = runtime
    with _FLOW_PROFILES_LOCK:
        _FLOW_PROFILES_CACHE.pop(runtime.program_id, None)
    with _CONTRACT_LOCK:
        stale_prefix = f"{runtime.program_id}:"
        stale_keys = [key for key in _CONTRACT_CACHE.keys() if key.startswith(stale_prefix)]
        for key in stale_keys:
            _CONTRACT_CACHE.pop(key, None)


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


def _safe_spec_pdf_path(program: ProgramRuntime) -> Path | None:
    if program.spec_pdf_path and program.spec_pdf_path.exists():
        return program.spec_pdf_path
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


def _sample_lines_from_payload(payload: bytes, *, input_encoding: str, max_lines: int = 250) -> list[str]:
    try:
        decoded = payload.decode(input_encoding, errors="replace")
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


def _closest_length_gap(contract: Any, median_len: int) -> tuple[int, int]:
    expected_lengths = sorted({record.max_end for record in contract.record_types})
    max_expected = max(expected_lengths) if expected_lengths else contract.line_length
    closest_gap = (
        min(abs(median_len - expected) for expected in expected_lengths)
        if expected_lengths
        else abs(median_len - contract.line_length)
    )
    return closest_gap, max_expected


def _compatibility_score(program: ProgramRuntime, profile: FlowProfile, lines: list[str]) -> float:
    if not lines:
        return 0.0
    try:
        contract = _get_contract(program, profile)
    except Exception:  # noqa: BLE001
        return 0.0

    median_len = _median_length(lines)
    selector_ratio = _selector_match_ratio(lines, contract)
    closest_gap, max_expected = _closest_length_gap(contract, median_len)
    length_tolerance = max(24, int(max_expected * 0.2))
    length_score = max(0.0, 1.0 - min(closest_gap, length_tolerance * 2) / (length_tolerance * 2))

    if profile.view_mode == "invoice":
        return round(selector_ratio * 0.8 + length_score * 0.2, 4)
    return round(max(selector_ratio, 0.15) * 0.6 + length_score * 0.4, 4)


def _suggest_better_profile(program: ProgramRuntime, profile: FlowProfile, lines: list[str]) -> FlowProfile | None:
    if not lines:
        return None

    selected_score = _compatibility_score(program, profile, lines)
    best_profile: FlowProfile | None = None
    best_score = 0.0
    for candidate in _get_flow_profiles(program.program_id).values():
        if candidate.flow_type != profile.flow_type:
            continue
        if candidate.file_name == profile.file_name:
            continue
        if not candidate.supports_processing:
            continue
        candidate_score = _compatibility_score(program, candidate, lines)
        if candidate_score > best_score:
            best_score = candidate_score
            best_profile = candidate

    if best_profile is None:
        return None
    if best_score < 0.5:
        return None
    if best_score < selected_score + 0.18:
        return None
    return best_profile


def _blocked_structure_message(program: ProgramRuntime, profile: FlowProfile, base_reason: str, lines: list[str]) -> str:
    suggestion = _suggest_better_profile(program, profile, lines)
    if suggestion is None:
        return (
            f"Chargement bloque pour {profile.flow_type}/{profile.file_name}: {base_reason} "
            f"Le contenu du fichier ne respecte pas la structure {program.source_program} attendue."
        )
    return (
        f"Chargement bloque pour {profile.flow_type}/{profile.file_name}: {base_reason} "
        f"Le contenu ressemble plutot a {suggestion.flow_type}/{suggestion.file_name} "
        f"d'apres la signature structurelle {program.source_program}."
    )


def _validate_uploaded_payload_for_profile(program: ProgramRuntime, profile: FlowProfile, payload: bytes) -> None:
    contract = _get_contract(program, profile)
    lines = _sample_lines_from_payload(payload, input_encoding=program.source_encoding)
    if not lines:
        raise HTTPException(status_code=400, detail="Le fichier charge ne contient aucune ligne exploitable.")

    median_len = _median_length(lines)
    closest_gap, max_expected = _closest_length_gap(contract, median_len)
    selector_ratio = _selector_match_ratio(lines, contract)

    if profile.view_mode == "invoice":
        if selector_ratio < 0.5:
            selectors = ", ".join(sorted({record.selector.value for record in contract.record_types}))
            raise HTTPException(
                status_code=400,
                detail=_blocked_structure_message(
                    program,
                    profile,
                    f"signature des enregistrements attendue ({selectors}) non detectee.",
                    lines,
                ),
            )
        if closest_gap > 12:
            raise HTTPException(
                status_code=400,
                detail=_blocked_structure_message(
                    program,
                    profile,
                    f"longueur mediane {median_len}, attendu proche de {max_expected}.",
                    lines,
                ),
            )
        return

    if len(contract.record_types) == 1:
        tolerance = max(20, int(max_expected * 0.2))
        if selector_ratio < 0.1 and closest_gap > tolerance:
            raise HTTPException(
                status_code=400,
                detail=_blocked_structure_message(
                    program,
                    profile,
                    f"longueur mediane {median_len}, attendu proche de {max_expected}.",
                    lines,
                ),
            )
        return

    tolerance = max(24, int(max_expected * 0.25))
    if selector_ratio < 0.1 and closest_gap > tolerance:
        raise HTTPException(
            status_code=400,
            detail=_blocked_structure_message(
                program,
                profile,
                "aucune signature d'enregistrement detectee sur l'echantillon.",
                lines,
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


def _discover_flow_profiles(program: ProgramRuntime) -> dict[tuple[str, str], FlowProfile]:
    text = program.source_path.read_text(encoding=program.source_encoding)
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

        # Fast startup: do not build deterministic contracts for every profile here.
        # Real contract build/validation is done lazily during job creation.
        supports_processing = bool(structure_prefixes or structure_names)

        profiles[(flow_type, file_name)] = FlowProfile(
            program_id=program.program_id,
            source_program=program.source_program,
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


def _get_flow_profiles(program_id: str) -> dict[tuple[str, str], FlowProfile]:
    program = _resolve_program(program_id)
    with _FLOW_PROFILES_LOCK:
        cached = _FLOW_PROFILES_CACHE.get(program.program_id)
        if cached is None:
            try:
                cached = _discover_flow_profiles(program)
            except FileNotFoundError as error:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Source programme introuvable pour {program.program_id}: {program.source_path}"
                    ),
                ) from error
            _FLOW_PROFILES_CACHE[program.program_id] = cached
        return cached


def _normalize_flow_type(flow_type: str | None) -> str:
    return (flow_type or "").strip().lower()


def _normalize_file_name(file_name: str | None) -> str:
    return (file_name or "").strip().upper()


def _resolve_profile(program_id: str, flow_type: str, file_name: str) -> FlowProfile:
    normalized_flow = _normalize_flow_type(flow_type)
    normalized_file = _normalize_file_name(file_name)
    profiles = _get_flow_profiles(program_id)
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


def _declared_line_length(program: ProgramRuntime, file_name: str) -> int | None:
    target = _normalize_file_name(file_name)
    if not target:
        return None
    try:
        text = program.source_path.read_text(encoding=program.source_encoding)
    except Exception:  # noqa: BLE001
        return None

    for raw_line in text.splitlines():
        line = _normalize_source_line(raw_line)
        if not line:
            continue
        match = _DCL_FILE_RE.search(line)
        if not match:
            continue
        declared_name = match.group(1).upper()
        if declared_name != target:
            continue
        tail = match.group(2) or ""
        sizes = [int(value) for value in _FILE_RECSIZE_RE.findall(tail)]
        if sizes:
            return max(sizes)
    return None


def _build_raw_fallback_contract(program: ProgramRuntime, profile: FlowProfile) -> ContractSpec:
    line_length = _declared_line_length(program, profile.file_name) or 1200
    record_name = _normalize_file_name(profile.file_name) or "RAW"
    return ContractSpec(
        source_program=program.source_program,
        line_length=line_length,
        strict_length_validation=False,
        strict_structure_validation=False,
        structure_source="fallback_raw",
        record_types=[
            RecordSpec(
                name=record_name,
                selector=SelectorSpec(start=1, length=1, value="*"),
                fields=[
                    FieldSpec(
                        name="RAW_LINE",
                        start=1,
                        length=line_length,
                        type=FieldType.STRING,
                        description=(
                            "Fallback technique: structure DETAILLEE indisponible, "
                            "enregistrement charge en ligne brute."
                        ),
                    )
                ],
            )
        ],
    )


def _build_contract(program: ProgramRuntime, profile: FlowProfile):
    if program.analyzer_engine not in SUPPORTED_ANALYZERS:
        raise ValueError(
            f"Moteur d'analyse non supporte '{program.analyzer_engine}' pour {program.program_id}."
        )
    try:
        return extract_contract_deterministic(
            source_path=program.source_path,
            source_program=program.source_program,
            strict=profile.strict_length_validation,
            spec_pdf_path=_safe_spec_pdf_path(program),
            structure_prefixes=profile.structure_prefixes or None,
            structure_names=set(profile.structure_names) if profile.structure_names else None,
            preserve_structure_names=profile.preserve_structure_names,
            apply_idil_rules=profile.apply_idil_rules,
        )
    except ValueError as error:
        message = str(error)
        if "No structure found for selected filters in source file." not in message:
            raise
        if profile.view_mode == "invoice":
            raise

        LOGGER.warning(
            "Fallback contrat brut active pour %s/%s (%s): %s",
            profile.flow_type,
            profile.file_name,
            program.source_program,
            message,
        )
        return _build_raw_fallback_contract(program, profile)


def _program_contract_cache_key(program: ProgramRuntime, profile: FlowProfile) -> str:
    source_mtime = "na"
    try:
        source_mtime = str(program.source_path.stat().st_mtime_ns)
    except Exception:  # noqa: BLE001
        pass
    return f"{profile.cache_key}:{source_mtime}"


def _get_contract(program: ProgramRuntime, profile: FlowProfile):
    if not program.reuse_contract:
        return _build_contract(program, profile)

    with _CONTRACT_LOCK:
        key = _program_contract_cache_key(program, profile)
        cached = _CONTRACT_CACHE.get(key)
        if cached is None:
            cached = _build_contract(program, profile)
            _CONTRACT_CACHE[key] = cached
        return cached


def _process_job(job_id: str, input_path: Path, program: ProgramRuntime, profile: FlowProfile) -> None:
    workdir = _job_dir(job_id)
    output_dir = workdir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    try:
        if not program.source_path.exists():
            raise FileNotFoundError(f"Source programme introuvable: {program.source_path}")
        if not profile.supports_processing:
            raise ValueError(
                f"Aucune structure exploitable detectee pour {profile.file_name} dans {program.source_program}."
            )

        _set_job(job_id, status="running", progress=10, message="Extraction du contrat en cours")
        contract = _get_contract(program, profile)

        contract_path = output_dir / f"{program.program_id}_contract.json"
        contract_path.write_text(
            json.dumps(contract.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        _set_job(job_id, progress=35, message=f"Parsing {profile.file_name} en cours")
        parser = FixedWidthParser(contract)
        records, issues = parser.parse_file(
            input_path=input_path,
            encoding=program.source_encoding,
            continue_on_error=program.continue_on_error,
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
                "program_id": program.program_id,
                "source_program": program.source_program,
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
    program_id: str
    program_display_name: str
    status: str
    message: str
    flow_type: str
    file_name: str
    view_mode: str
    role_label: str


class JobStatusResponse(BaseModel):
    job_id: str
    program_id: str
    program_display_name: str
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


class ProgramSummaryResponse(BaseModel):
    program_id: str
    display_name: str
    source_program: str
    analyzer_engine: str
    source_path: str
    invoice_only_default: bool


class ProgramsResponse(BaseModel):
    default_program_id: str
    programs: list[ProgramSummaryResponse]


class CatalogResponse(BaseModel):
    program_id: str
    program_display_name: str
    source_program: str
    default_flow_type: str
    default_file_name: str
    invoice_only_default: bool
    advanced_mode: bool
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


def _artifact_download_filename(job: JobState, output_key: str, output_path: Path) -> str:
    prefix = _normalize_file_name(job.file_name) or "EXPORT"
    suffix_map = {
        "excel": "parsed_records.xlsx",
        "pdf_factures": "facture_exemple.pdf",
        "pdf_synthese": "synthese_comptable.pdf",
        "jsonl": "parsed_records.jsonl",
        "contract": "contract.json",
    }
    suffix = suffix_map.get(output_key, output_path.name)
    upper_prefix = f"{prefix}_"
    if suffix.upper().startswith(upper_prefix):
        return suffix
    return f"{prefix}_{suffix}"


def _to_status_response(job_id: str, job: JobState) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=job_id,
        program_id=job.program_id,
        program_display_name=job.program_display_name,
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
    title="IDIL PAPYRUS MAINFRAME API",
    version="1.0.0",
    description="API web multi-programmes pour extraction Mainframe dynamique vers Excel et PDF.",
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
    default_program = _get_default_program()
    return {
        "status": "ok",
        "default_program_id": default_program.program_id,
        "source_program": default_program.source_program,
    }


@app.get("/api/programs", response_model=ProgramsResponse)
def programs() -> ProgramsResponse:
    program_items = sorted(_get_programs().values(), key=lambda item: item.program_id)
    default_program = _get_default_program()
    return ProgramsResponse(
        default_program_id=default_program.program_id,
        programs=[
            ProgramSummaryResponse(
                program_id=item.program_id,
                display_name=item.display_name,
                source_program=item.source_program,
                analyzer_engine=item.analyzer_engine,
                source_path=str(item.source_path),
                invoice_only_default=item.invoice_only_default,
            )
            for item in program_items
        ],
    )


@app.post("/api/programs/local", response_model=ProgramSummaryResponse)
async def register_local_program(
    source_file: UploadFile = File(...),
    program_name: str | None = Form(default=None),
    display_name: str | None = Form(default=None),
    source_encoding: str = Form(default=DEFAULT_INPUT_ENCODING),
    invoice_only_default: bool = Form(default=False),
    spec_pdf_path: str | None = Form(default=None),
) -> ProgramSummaryResponse:
    if not source_file.filename:
        raise HTTPException(status_code=400, detail="Aucun fichier programme transmis.")

    safe_name = Path(source_file.filename).name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_SOURCE_SUFFIXES:
        allowed = ", ".join(sorted(ALLOWED_SOURCE_SUFFIXES))
        raise HTTPException(status_code=400, detail=f"Format non supporte ({suffix}). Formats autorises: {allowed}")

    payload = await source_file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Le programme local charge est vide.")

    effective_encoding = (source_encoding or DEFAULT_INPUT_ENCODING).strip() or DEFAULT_INPUT_ENCODING
    source_program_name = (program_name or Path(safe_name).stem or "LOCAL_PROGRAM").strip().upper()
    local_program_id = f"local_{uuid.uuid4().hex[:10]}"
    target_path = LOCAL_PROGRAMS_ROOT / f"{local_program_id}_{safe_name}"
    target_path.write_bytes(payload)

    resolved_spec_pdf_path: Path | None = None
    if spec_pdf_path:
        candidate = _resolve_project_path(spec_pdf_path)
        if candidate.exists():
            resolved_spec_pdf_path = candidate

    runtime = ProgramRuntime(
        program_id=local_program_id,
        display_name=(display_name or f"Programme Local {source_program_name}").strip(),
        description=f"Programme local charge depuis {safe_name}",
        source_program=source_program_name,
        source_path=target_path,
        source_encoding=effective_encoding,
        analyzer_engine="idp470_pli",
        spec_pdf_path=resolved_spec_pdf_path,
        invoice_only_default=invoice_only_default,
        default_flow_type="output",
        default_file_name="FICDEMA",
        continue_on_error=DEFAULT_CONTINUE_ON_ERROR,
        reuse_contract=DEFAULT_REUSE_CONTRACT,
    )
    _register_runtime(runtime)

    return ProgramSummaryResponse(
        program_id=runtime.program_id,
        display_name=runtime.display_name,
        source_program=runtime.source_program,
        analyzer_engine=runtime.analyzer_engine,
        source_path=str(runtime.source_path),
        invoice_only_default=runtime.invoice_only_default,
    )


@app.get("/api/catalog", response_model=CatalogResponse)
def catalog(program_id: str | None = None, advanced: bool | None = None) -> CatalogResponse:
    program = _resolve_program(program_id)
    profiles_map = _get_flow_profiles(program.program_id)
    advanced_mode = bool(advanced) if advanced is not None else (not program.invoice_only_default)
    filtered_profiles = [
        profile for profile in profiles_map.values() if advanced_mode or profile.view_mode == "invoice"
    ]
    if not filtered_profiles:
        filtered_profiles = list(profiles_map.values())
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
        for profile in sorted(filtered_profiles, key=lambda item: (item.flow_type, item.file_name))
    ]
    filtered_map = {(profile.flow_type, profile.file_name): profile for profile in filtered_profiles}
    default_profile = filtered_map.get((program.default_flow_type, program.default_file_name))
    if default_profile is None and profiles:
        first = profiles[0]
        default_flow_type = first.flow_type
        default_file_name = first.file_name
    else:
        default_flow_type = default_profile.flow_type if default_profile else program.default_flow_type
        default_file_name = default_profile.file_name if default_profile else program.default_file_name
    return CatalogResponse(
        program_id=program.program_id,
        program_display_name=program.display_name,
        source_program=program.source_program,
        default_flow_type=default_flow_type,
        default_file_name=default_file_name,
        invoice_only_default=program.invoice_only_default,
        advanced_mode=advanced_mode,
        profiles=profiles,
    )


@app.post("/api/jobs", response_model=JobCreateResponse)
async def create_job(
    background_tasks: BackgroundTasks,
    data_file: UploadFile | None = File(default=None),
    facdema_file: UploadFile | None = File(default=None),
    program_id: str | None = Form(default=None),
    flow_type: str = Form(default="output"),
    file_name: str = Form(default="FICDEMA"),
    advanced_mode: bool = Form(default=False),
) -> JobCreateResponse:
    program = _resolve_program(program_id)
    profile = _resolve_profile(program_id=program.program_id, flow_type=flow_type, file_name=file_name)
    if program.invoice_only_default and not advanced_mode and profile.view_mode != "invoice":
        raise HTTPException(
            status_code=400,
            detail=(
                "Mode standard actif: seuls les fichiers Factures sont autorises. "
                "Activez le mode avance pour charger ce fichier."
            ),
        )
    if not profile.supports_processing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Le fichier {profile.file_name} est detecte dans {program.source_program} mais aucun mapping "
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

    try:
        _get_contract(program, profile)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=(
                f"Mapping {program.source_program} indisponible pour {profile.file_name}. "
                f"Details: {error}"
            ),
        ) from error

    _validate_uploaded_payload_for_profile(program, profile, payload)

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
            program_id=program.program_id,
            program_display_name=program.display_name,
            flow_type=profile.flow_type,
            file_name=profile.file_name,
            view_mode=profile.view_mode,
            role_label=profile.role_label,
        )

    background_tasks.add_task(_process_job, job_id, input_path, program, profile)
    return JobCreateResponse(
        job_id=job_id,
        program_id=program.program_id,
        program_display_name=program.display_name,
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

    download_name = _artifact_download_filename(job, output_key, output_path)
    return FileResponse(
        output_path,
        filename=download_name,
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
