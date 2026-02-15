from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from idp470_pipeline.deterministic_extractor import extract_contract_deterministic
from idp470_pipeline.exporters import export_accounting_summary_pdf, export_first_invoice_pdf, export_to_excel
from idp470_pipeline.parsing_engine import FixedWidthParser, save_jsonl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = Path(os.getenv("IDP470_WEB_SOURCE", PROJECT_ROOT / "IDP470RA.pli")).expanduser()
SPEC_PDF_PATH = Path(
    os.getenv("IDP470_WEB_SPEC_PDF", PROJECT_ROOT / "2785 - DOCTECHN - Dilifac - Format IDIL.pdf")
).expanduser()
LOGO_PATH = Path(os.getenv("IDP470_WEB_LOGO", PROJECT_ROOT / "assets" / "logo_hachette_livre.png")).expanduser()
JOBS_ROOT = Path(os.getenv("IDP470_WEB_JOBS_DIR", PROJECT_ROOT / "web_app" / "jobs")).expanduser()
INPUT_ENCODING = os.getenv("IDP470_WEB_INPUT_ENCODING", "latin-1")
CONTINUE_ON_ERROR = os.getenv("IDP470_WEB_CONTINUE_ON_ERROR", "false").strip().lower() == "true"

JOBS_ROOT.mkdir(parents=True, exist_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobState:
    job_id: str
    input_filename: str
    status: str = "queued"
    progress: int = 2
    message: str = "En attente"
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, int] = field(
        default_factory=lambda: {
            "invoice_count": 0,
            "line_count": 0,
            "issues_count": 0,
            "records_count": 0,
        }
    )
    outputs: dict[str, str] = field(default_factory=dict)


_JOBS: dict[str, JobState] = {}
_JOBS_LOCK = threading.Lock()


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


def _process_job(job_id: str, input_path: Path) -> None:
    workdir = _job_dir(job_id)
    output_dir = workdir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    try:
        if not SOURCE_PATH.exists():
            raise FileNotFoundError(f"Source programme introuvable: {SOURCE_PATH}")

        _set_job(job_id, status="running", progress=10, message="Extraction du contrat en cours")
        contract = extract_contract_deterministic(
            source_path=SOURCE_PATH,
            source_program="IDP470RA",
            strict=True,
            spec_pdf_path=_safe_spec_pdf_path(),
        )

        contract_path = output_dir / "idp470ra_contract.json"
        contract_path.write_text(
            json.dumps(contract.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        _set_job(job_id, progress=35, message="Parsing FACDEMA en cours")
        parser = FixedWidthParser(contract)
        records, issues = parser.parse_file(
            input_path=input_path,
            encoding=INPUT_ENCODING,
            continue_on_error=CONTINUE_ON_ERROR,
        )

        parsed_path = output_dir / "parsed_records.jsonl"
        save_jsonl(records=records, output_path=parsed_path)

        _set_job(job_id, progress=55, message="Generation Excel en cours")
        excel_path = output_dir / "parsed_records.xlsx"
        export_to_excel(records=records, output_path=excel_path, contract=contract)

        _set_job(job_id, progress=75, message="Generation PDF factures en cours")
        pdf_factures_path = output_dir / "facture_exemple.pdf"
        try:
            export_first_invoice_pdf(records=records, output_path=pdf_factures_path, logo_path=_safe_logo_path())
        except Exception as error:  # noqa: BLE001
            warnings.append(f"PDF factures non genere: {error}")

        _set_job(job_id, progress=90, message="Generation PDF synthese en cours")
        pdf_synthese_path = output_dir / "synthese_comptable.pdf"
        try:
            export_accounting_summary_pdf(records=records, output_path=pdf_synthese_path, logo_path=_safe_logo_path())
        except Exception as error:  # noqa: BLE001
            warnings.append(f"PDF synthese non genere: {error}")

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


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
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
    description="API web pour extraction Mainframe FACDEMA vers Excel et PDF.",
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


@app.post("/api/jobs", response_model=JobCreateResponse)
async def create_job(
    background_tasks: BackgroundTasks,
    facdema_file: UploadFile = File(...),
) -> JobCreateResponse:
    if not facdema_file.filename:
        raise HTTPException(status_code=400, detail="Nom de fichier FACDEMA manquant.")

    suffix = Path(facdema_file.filename).suffix.lower()
    if suffix not in {".txt", ".dat"}:
        raise HTTPException(status_code=400, detail="Format autorise: .txt ou .dat")

    payload = await facdema_file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Le fichier FACDEMA est vide.")

    job_id = uuid.uuid4().hex[:12]
    safe_name = Path(facdema_file.filename).name
    input_dir = _job_dir(job_id) / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / safe_name
    input_path.write_bytes(payload)

    with _JOBS_LOCK:
        _JOBS[job_id] = JobState(job_id=job_id, input_filename=safe_name)

    background_tasks.add_task(_process_job, job_id, input_path)
    return JobCreateResponse(job_id=job_id, status="queued", message="Traitement lance")


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
