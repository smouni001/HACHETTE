from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import streamlit as st

from idp470_pipeline.deterministic_extractor import extract_contract_deterministic
from idp470_pipeline.exporters import export_first_invoice_pdf, export_to_excel
from idp470_pipeline.genai_extractor import GenAIExtractionError, GenAISettings, extract_contract_with_genai
from idp470_pipeline.idil_structure_rules import attach_idil_structure_rules
from idp470_pipeline.models import ContractSpec
from idp470_pipeline.parsing_engine import (
    ContractValidationError,
    FixedWidthParser,
    ParsingError,
)


@dataclass
class UISettings:
    provider: str = "deterministic"
    model: str = ""
    temperature: float = 0.0
    source_encoding: str = "latin-1"
    input_encoding: str = "latin-1"
    fallback_deterministic: bool = True
    strict_length_validation: bool = True
    continue_on_error: bool = False
    default_source_path: str = "IDP470RA.pli"
    default_spec_pdf_path: str = "../2785 - DOCTECHN - Dilifac - Format IDIL.pdf"
    default_input_path: str = "facdemat_20251021_nufac29501954.txt"
    default_logo_path: str = "assets/logo_hachette_livre.png"
    app_title: str = "IDIL PAPYRUS FACTURE DEMAT"
    app_subtitle: str = (
        "Extraction Mainframe vers Excel/PDF"
    )


_ALLOWED_PROVIDERS = {"deterministic", "openai", "anthropic"}
_ALLOWED_ENCODINGS = {"latin-1", "utf-8"}


def _config_candidates() -> list[Path]:
    explicit = os.getenv("IDP470_UI_CONFIG_FILE")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend(
        [
            Path.cwd() / "config" / "ui_settings.toml",
            Path.cwd() / ".streamlit" / "ui_settings.toml",
        ]
    )
    return candidates


def _load_ui_settings() -> tuple[UISettings, str]:
    settings = UISettings()
    source_label = "internal defaults"

    for path in _config_candidates():
        if not path.exists():
            continue
        with path.open("rb") as handle:
            payload = tomllib.load(handle)

        section = payload.get("pipeline", payload)
        source_label = str(path)

        if isinstance(section.get("provider"), str):
            provider = section["provider"].strip().lower()
            if provider in _ALLOWED_PROVIDERS:
                settings.provider = provider

        if isinstance(section.get("model"), str):
            settings.model = section["model"].strip()
        if isinstance(section.get("temperature"), (float, int)):
            settings.temperature = max(0.0, min(1.0, float(section["temperature"])))

        if isinstance(section.get("source_encoding"), str):
            value = section["source_encoding"].strip().lower()
            if value in _ALLOWED_ENCODINGS:
                settings.source_encoding = value

        if isinstance(section.get("input_encoding"), str):
            value = section["input_encoding"].strip().lower()
            if value in _ALLOWED_ENCODINGS:
                settings.input_encoding = value

        if isinstance(section.get("fallback_deterministic"), bool):
            settings.fallback_deterministic = section["fallback_deterministic"]
        if isinstance(section.get("strict_length_validation"), bool):
            settings.strict_length_validation = section["strict_length_validation"]
        if isinstance(section.get("continue_on_error"), bool):
            settings.continue_on_error = section["continue_on_error"]

        if isinstance(section.get("default_source_path"), str):
            settings.default_source_path = section["default_source_path"].strip() or settings.default_source_path
        if isinstance(section.get("default_spec_pdf_path"), str):
            settings.default_spec_pdf_path = (
                section["default_spec_pdf_path"].strip() or settings.default_spec_pdf_path
            )
        if isinstance(section.get("default_input_path"), str):
            settings.default_input_path = section["default_input_path"].strip() or settings.default_input_path
        if isinstance(section.get("default_logo_path"), str):
            settings.default_logo_path = section["default_logo_path"].strip() or settings.default_logo_path

        if isinstance(section.get("app_title"), str):
            settings.app_title = section["app_title"].strip() or settings.app_title
        if isinstance(section.get("app_subtitle"), str):
            settings.app_subtitle = section["app_subtitle"].strip() or settings.app_subtitle
        break

    if settings.provider == "openai" and not settings.model:
        settings.model = "gpt-4.1-mini"
    if settings.provider == "anthropic" and not settings.model:
        settings.model = "claude-3-7-sonnet-20250219"
    return settings, source_label


def _apply_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
        :root {
          --bg: #f3f6fa;
          --surface: #ffffff;
          --ink: #102235;
          --muted: #334155;
          --border: #c7d6e4;
          --primary: #0f766e;
          --primary-2: #0ea5a3;
          --accent: #d97706;
        }
        .stApp {
          background:
            radial-gradient(circle at 8% -4%, #d9f5ee 0, transparent 28%),
            radial-gradient(circle at 100% 0, #ffeccf 0, transparent 20%),
            linear-gradient(165deg, var(--bg) 0%, #e9f0f7 74%);
        }
        html, body, [data-testid="stAppViewContainer"] {
          font-family: "Sora", sans-serif;
          color: var(--ink);
          font-size: 16px;
        }
        .main .block-container {
          max-width: 1200px;
          padding-top: 1.1rem;
          padding-bottom: 2.1rem;
        }
        .hero {
          background: linear-gradient(130deg, #ffffff 0%, #f8fcff 100%);
          border: 1px solid var(--border);
          border-left: 7px solid var(--primary);
          border-radius: 18px;
          padding: 20px 22px;
          box-shadow: 0 14px 30px rgba(7, 23, 41, 0.06);
          margin-bottom: 0.9rem;
        }
        .hero h1 {
          margin: 0 0 0.35rem 0;
          font-size: 1.86rem;
          letter-spacing: 0.2px;
        }
        .hero p {
          margin: 0;
          color: var(--muted);
          font-size: 1rem;
          line-height: 1.55;
        }
        .section-card {
          background: var(--surface);
          border: 1px solid var(--border);
          border-radius: 16px;
          padding: 12px 14px 6px 14px;
          box-shadow: 0 10px 24px rgba(7, 23, 41, 0.04);
          margin-bottom: 0.8rem;
        }
        .section-title {
          font-size: 1.08rem;
          margin: 0 0 0.45rem 0;
          letter-spacing: 0.2px;
        }
        .small-note {
          color: var(--muted);
          font-size: 0.9rem;
        }
        .stButton > button {
          width: 100%;
          border-radius: 12px;
          border: 0;
          padding: 0.78rem 1rem;
          color: white;
          font-weight: 700;
          letter-spacing: 0.2px;
          background: linear-gradient(90deg, var(--primary), var(--primary-2));
        }
        .stButton > button:hover {
          filter: brightness(0.96);
        }
        [data-testid="stMetric"] {
          border: 1px solid var(--border);
          border-radius: 14px;
          background: #ffffff;
          padding: 0.35rem 0.55rem;
        }
        [data-testid="stDataFrame"] div {
          font-size: 0.92rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _read_local_file(path: str) -> bytes:
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"Fichier introuvable: {p}")
    return p.read_bytes()


def _resolve_input_file(mode: str, local_path: str, uploaded_file: Any | None) -> tuple[str, bytes]:
    if mode == "Local path":
        content = _read_local_file(local_path)
        return Path(local_path).name, content
    if uploaded_file is None:
        raise ValueError("Aucun fichier charge.")
    return uploaded_file.name, uploaded_file.getvalue()


def _run_pipeline(
    source_name: str,
    source_bytes: bytes,
    input_name: str,
    input_bytes: bytes,
    settings: UISettings,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="idp470_ui_") as tmp:
        workdir = Path(tmp)
        source_path = workdir / source_name
        input_path = workdir / input_name
        source_path.write_bytes(source_bytes)
        input_path.write_bytes(input_bytes)

        contract: ContractSpec
        spec_pdf_path = Path(settings.default_spec_pdf_path).expanduser()
        if not spec_pdf_path.exists():
            spec_pdf_path = None

        if settings.provider == "deterministic":
            contract = extract_contract_deterministic(
                source_path=source_path,
                source_program="IDP470RA",
                strict=settings.strict_length_validation,
                spec_pdf_path=spec_pdf_path,
            )
        else:
            source_text = source_path.read_text(encoding=settings.source_encoding, errors="replace")
            genai_settings = GenAISettings(
                provider=settings.provider,
                model=settings.model or None,
                temperature=settings.temperature,
            )
            try:
                contract = extract_contract_with_genai(
                    source_program="IDP470RA",
                    source_text=source_text,
                    settings=genai_settings,
                )
                contract = attach_idil_structure_rules(
                    contract=contract,
                    spec_pdf_path=spec_pdf_path,
                )
            except GenAIExtractionError:
                if not settings.fallback_deterministic:
                    raise
                contract = extract_contract_deterministic(
                    source_path=source_path,
                    source_program="IDP470RA",
                    strict=settings.strict_length_validation,
                    spec_pdf_path=spec_pdf_path,
                )

        parser = FixedWidthParser(contract)
        try:
            records, issues = parser.parse_file(
                input_path=input_path,
                encoding=settings.input_encoding,
                continue_on_error=settings.continue_on_error,
            )
        except (ParsingError, ContractValidationError):
            if not settings.fallback_deterministic or settings.provider == "deterministic":
                raise
            contract = extract_contract_deterministic(
                source_path=source_path,
                source_program="IDP470RA",
                strict=settings.strict_length_validation,
                spec_pdf_path=spec_pdf_path,
            )
            parser = FixedWidthParser(contract)
            records, issues = parser.parse_file(
                input_path=input_path,
                encoding=settings.input_encoding,
                continue_on_error=settings.continue_on_error,
            )

        excel_path = workdir / "parsed_records.xlsx"
        export_to_excel(records=records, output_path=excel_path, contract=contract)

        pdf_path = workdir / "facture_exemple.pdf"
        pdf_bytes: bytes | None = None
        pdf_error: str | None = None
        try:
            configured_logo = Path(settings.default_logo_path).expanduser()
            export_first_invoice_pdf(
                records=records,
                output_path=pdf_path,
                logo_path=configured_logo,
            )
            pdf_bytes = pdf_path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            pdf_error = str(exc)

        ent_invoices = {
            str(record.get("NUFAC", "")).strip()
            for record in records
            if record.get("record_type") == "ENT" and str(record.get("NUFAC", "")).strip()
        }
        if ent_invoices:
            invoice_count = len(ent_invoices)
        else:
            all_invoices = {
                str(record.get("NUFAC", "")).strip()
                for record in records
                if str(record.get("NUFAC", "")).strip()
            }
            invoice_count = len(all_invoices)
        line_count = len(records) + len(issues)

        return {
            "excel": excel_path.read_bytes(),
            "pdf": pdf_bytes,
            "pdf_error": pdf_error,
            "invoice_count": invoice_count,
            "line_count": line_count,
        }


def _metrics_panel(result: dict[str, Any]) -> None:
    c1, c2 = st.columns(2)
    c1.metric("Nombre de factures", f"{result['invoice_count']:,}")
    c2.metric("Lignes du fichier", f"{result['line_count']:,}")


def _resolve_ui_logo_path(settings: UISettings) -> Path | None:
    configured = Path(settings.default_logo_path).expanduser()
    if configured.exists():
        return configured

    candidates = [
        Path.cwd() / "assets" / "logo_hachette_livre.png",
        Path(__file__).resolve().parent / "assets" / "logo_hachette_livre.png",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _render_top_logo(settings: UISettings) -> None:
    logo_path = _resolve_ui_logo_path(settings)
    if logo_path is None:
        return

    st.image(str(logo_path), width=220)


def _hero(settings: UISettings) -> None:
    st.markdown(
        f"""
        <div class="hero">
          <h1>{settings.app_title}</h1>
          <p>{settings.app_subtitle}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(
        page_title="IDIL PAPYRUS FACTURE DEMAT",
        page_icon="ID",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _apply_theme()

    settings, _ = _load_ui_settings()
    _render_top_logo(settings=settings)
    _hero(settings=settings)

    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    st.markdown('<p class="section-title"><b>Source programme</b></p>', unsafe_allow_html=True)
    st.caption("IDP470RA")
    st.caption(f"Fichier source interne: `{settings.default_source_path}`")
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    st.markdown('<p class="section-title"><b>Fichier source FACDEMA</b></p>', unsafe_allow_html=True)
    input_upload = st.file_uploader(
        "Charger le fichier FACDEMA (obligatoire)",
        type=["txt", "dat"],
        key="input_upload_required",
    )
    st.markdown("</div>", unsafe_allow_html=True)

    run_clicked = st.button("Extraction Excel/PDF")

    if run_clicked:
        try:
            source_file = settings.default_source_path or "IDP470RA.pli"
            source_name = Path(source_file).name
            source_bytes = _read_local_file(source_file)

            if input_upload is None:
                raise ValueError("Veuillez charger le fichier FACDEMA avant de lancer l'extraction.")
            input_name = input_upload.name
            input_bytes = input_upload.getvalue()

            with st.status("Extraction en cours", expanded=True) as status:
                st.write("Validation des fichiers")
                st.write("Extraction de contrat")
                result = _run_pipeline(
                    source_name=source_name,
                    source_bytes=source_bytes,
                    input_name=input_name,
                    input_bytes=input_bytes,
                    settings=settings,
                )
                st.write("Traitement et generation des exports")
                status.update(label="Extraction terminee", state="complete")
            st.session_state["pipeline_result"] = result
            st.success("Extraction terminee avec succes")
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))

    result = st.session_state.get("pipeline_result")
    if not result:
        return

    _metrics_panel(result)
    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    st.markdown('<p class="section-title"><b>Telechargements</b></p>', unsafe_allow_html=True)
    col_excel, col_pdf = st.columns(2)
    with col_excel:
        st.download_button(
            "Telecharger Excel",
            data=result["excel"],
            file_name="parsed_records.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    with col_pdf:
        if result["pdf"] is not None:
            st.download_button(
                "Telecharger PDF",
                data=result["pdf"],
                file_name="facture_exemple.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        else:
            st.warning(f"PDF non genere: {result['pdf_error']}")
    st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
