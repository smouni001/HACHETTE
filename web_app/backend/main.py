from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import pandas as pd

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
FAST_EXCEL = os.getenv("IDP470_WEB_FAST_EXCEL", "true").strip().lower() == "true"
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
    outputs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FlowProfile:
    flow_type: str
    file_name: str
    display_name: str
    description: str
    structure_prefixes: tuple[str, ...] = ()
    structure_names: tuple[str, ...] = ()
    preserve_structure_names: bool = False
    apply_idil_rules: bool = False
    supports_pdf: bool = False
    strict_length_validation: bool = True

    @property
    def cache_key(self) -> str:
        return f"{self.flow_type}:{self.file_name}"


_FLOW_PROFILES: dict[tuple[str, str], FlowProfile] = {
    ("output", "FICDEMA"): FlowProfile(
        flow_type="output",
        file_name="FICDEMA",
        display_name="FICDEMA",
        description="Flux output facture dematerialisee.",
        structure_prefixes=("DEMAT_",),
        apply_idil_rules=True,
        supports_pdf=True,
        strict_length_validation=True,
    ),
    ("output", "FICSTOD"): FlowProfile(
        flow_type="output",
        file_name="FICSTOD",
        display_name="FICSTOD",
        description="Flux output stock facture dematerialisee.",
        structure_prefixes=("STO_D_",),
        apply_idil_rules=True,
        supports_pdf=True,
        strict_length_validation=True,
    ),
    ("input", "FFAC3A"): FlowProfile(
        flow_type="input",
        file_name="FFAC3A",
        display_name="FFAC3A",
        description="Flux input source a facturer.",
        structure_names=("WTFAC",),
        preserve_structure_names=True,
        apply_idil_rules=False,
        supports_pdf=False,
        strict_length_validation=False,
    ),
}


_JOBS: dict[str, JobState] = {}
_JOBS_LOCK = threading.Lock()
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


def _normalize_flow_type(flow_type: str | None) -> str:
    return (flow_type or "").strip().lower()


def _normalize_file_name(file_name: str | None) -> str:
    return (file_name or "").strip().upper()


def _resolve_profile(flow_type: str, file_name: str) -> FlowProfile:
    normalized_flow = _normalize_flow_type(flow_type)
    normalized_file = _normalize_file_name(file_name)
    profile = _FLOW_PROFILES.get((normalized_flow, normalized_file))
    if profile is None:
        allowed = ", ".join(
            f"{profile.flow_type}/{profile.file_name}" for profile in _FLOW_PROFILES.values()
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


def _export_excel_fast(records: list[dict[str, Any]], output_path: Path) -> None:
    if not records:
        raise ValueError("Aucun enregistrement a exporter vers Excel.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(records)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="TOUS")


def _process_job(job_id: str, input_path: Path, profile: FlowProfile) -> None:
    workdir = _job_dir(job_id)
    output_dir = workdir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    try:
        if not SOURCE_PATH.exists():
            raise FileNotFoundError(f"Source programme introuvable: {SOURCE_PATH}")

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
            message="Generation Excel rapide en cours" if FAST_EXCEL else "Generation Excel en cours",
        )
        excel_path = output_dir / "parsed_records.xlsx"
        if FAST_EXCEL:
            _export_excel_fast(records=records, output_path=excel_path)
        else:
            export_to_excel(records=records, output_path=excel_path, contract=contract)

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

        _set_job(
            job_id,
            status="completed",
            progress=100,
            message="Extraction terminee avec succes",
            warnings=warnings,
            metrics=metrics,
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


class JobStatusResponse(BaseModel):
    job_id: str
    flow_type: str
    file_name: str
    status: str
    progress: int = Field(ge=0, le=100)
    message: str
    input_filename: str
    created_at: str
    updated_at: str
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, int] = Field(default_factory=dict)
    downloads: dict[str, str] = Field(default_factory=dict)


class CatalogProfileResponse(BaseModel):
    flow_type: str
    file_name: str
    display_name: str
    description: str


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
        status=job.status,
        progress=job.progress,
        message=job.message,
        input_filename=job.input_filename,
        created_at=job.created_at,
        updated_at=job.updated_at,
        error=job.error,
        warnings=job.warnings,
        metrics=job.metrics,
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
    profiles = [
        CatalogProfileResponse(
            flow_type=profile.flow_type,
            file_name=profile.file_name,
            display_name=profile.display_name,
            description=profile.description,
        )
        for profile in sorted(_FLOW_PROFILES.values(), key=lambda item: (item.flow_type, item.file_name))
    ]
    return CatalogResponse(
        source_program="IDP470RA",
        default_flow_type="output",
        default_file_name="FICDEMA",
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
    uploaded_file = data_file or facdema_file

    if uploaded_file is None or not uploaded_file.filename:
        raise HTTPException(status_code=400, detail=f"Nom de fichier {profile.file_name} manquant.")

    suffix = Path(uploaded_file.filename).suffix.lower()
    if suffix not in {".txt", ".dat"}:
        raise HTTPException(status_code=400, detail="Format autorise: .txt ou .dat")

    payload = await uploaded_file.read()
    if not payload:
        raise HTTPException(status_code=400, detail=f"Le fichier {profile.file_name} est vide.")

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
        )

    background_tasks.add_task(_process_job, job_id, input_path, profile)
    return JobCreateResponse(
        job_id=job_id,
        status="queued",
        message="Traitement lance",
        flow_type=profile.flow_type,
        file_name=profile.file_name,
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
