from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .deterministic_extractor import extract_contract_deterministic
from .exporters import export_accounting_summary_pdf, export_first_invoice_pdf, export_to_excel
from .genai_extractor import GenAIExtractionError, GenAISettings, extract_contract_with_genai
from .idil_structure_rules import attach_idil_structure_rules
from .models import ContractSpec
from .parsing_engine import ContractValidationError, FixedWidthParser, ParsingError, load_jsonl, save_jsonl

LOGGER = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _save_contract(contract: ContractSpec, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(contract.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_contract(contract_path: Path) -> ContractSpec:
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    return ContractSpec.model_validate(payload)


def _extract_command(args: argparse.Namespace) -> int:
    source_path = Path(args.source)
    output_path = Path(args.output)
    spec_pdf_path = Path(args.spec_pdf) if args.spec_pdf else None

    if args.provider == "deterministic":
        contract = extract_contract_deterministic(
            source_path=source_path,
            source_program=args.program,
            strict=not args.disable_strict_length_validation,
            spec_pdf_path=spec_pdf_path,
        )
    else:
        source_text = source_path.read_text(encoding=args.source_encoding)
        settings = GenAISettings(
            provider=args.provider,
            model=args.model,
            temperature=args.temperature,
        )
        try:
            contract = extract_contract_with_genai(
                source_program=args.program,
                source_text=source_text,
                settings=settings,
            )
            contract = attach_idil_structure_rules(contract=contract, spec_pdf_path=spec_pdf_path)
        except GenAIExtractionError:
            if not args.fallback_deterministic:
                raise
            LOGGER.exception("GenAI extraction failed. Fallback to deterministic parser.")
            contract = extract_contract_deterministic(
                source_path=source_path,
                source_program=args.program,
                strict=not args.disable_strict_length_validation,
                spec_pdf_path=spec_pdf_path,
            )

    _save_contract(contract=contract, output_path=output_path)
    LOGGER.info("Contract saved to %s", output_path)
    return 0


def _parse_command(args: argparse.Namespace) -> int:
    contract = _load_contract(Path(args.contract))
    parser = FixedWidthParser(contract)
    records, issues = parser.parse_file(
        input_path=Path(args.input),
        encoding=args.input_encoding,
        continue_on_error=args.continue_on_error,
    )

    output_jsonl = Path(args.output_jsonl)
    save_jsonl(records=records, output_path=output_jsonl)
    LOGGER.info("Parsed %s records into %s", len(records), output_jsonl)
    if issues:
        LOGGER.warning("Parsing issues: %s", len(issues))
    return 0


def _excel_command(args: argparse.Namespace) -> int:
    records = load_jsonl(Path(args.input_jsonl))
    contract = _load_contract(Path(args.contract)) if args.contract else None
    export_to_excel(records=records, output_path=Path(args.output_xlsx), contract=contract)
    return 0


def _pdf_command(args: argparse.Namespace) -> int:
    records = load_jsonl(Path(args.input_jsonl))
    logo = Path(args.logo) if args.logo else None
    export_first_invoice_pdf(records=records, output_path=Path(args.output_pdf), logo_path=logo)
    return 0


def _pdf_summary_command(args: argparse.Namespace) -> int:
    records = load_jsonl(Path(args.input_jsonl))
    logo = Path(args.logo) if args.logo else None
    export_accounting_summary_pdf(records=records, output_path=Path(args.output_pdf), logo_path=logo)
    return 0


def _run_command(args: argparse.Namespace) -> int:
    source_path = Path(args.source)
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    contract_path = Path(args.contract) if args.contract else output_dir / "idp470ra_contract.json"
    parsed_path = output_dir / "parsed_records.jsonl"
    excel_path = output_dir / "parsed_records.xlsx"
    pdf_path = output_dir / "facture_exemple.pdf"
    accounting_pdf_path = output_dir / "synthese_comptable.pdf"

    if contract_path.exists() and not args.force_extract:
        contract = _load_contract(contract_path)
        LOGGER.info("Using existing contract: %s", contract_path)
    else:
        extract_args = argparse.Namespace(
            source=str(source_path),
            output=str(contract_path),
            provider=args.provider,
            model=args.model,
            program=args.program,
            source_encoding=args.source_encoding,
            temperature=args.temperature,
            fallback_deterministic=args.fallback_deterministic,
            disable_strict_length_validation=args.disable_strict_length_validation,
            spec_pdf=args.spec_pdf,
        )
        _extract_command(extract_args)
        contract = _load_contract(contract_path)

    def _parse_with_contract(active_contract: ContractSpec) -> tuple[list[dict], list]:
        parser = FixedWidthParser(active_contract)
        return parser.parse_file(
            input_path=input_path,
            encoding=args.input_encoding,
            continue_on_error=args.continue_on_error,
        )

    try:
        records, issues = _parse_with_contract(contract)
    except (ParsingError, ContractValidationError):
        if not args.fallback_deterministic or args.provider == "deterministic":
            raise
        LOGGER.exception(
            "Parsing failed with provider=%s contract. Fallback to deterministic contract.",
            args.provider,
        )
        contract = extract_contract_deterministic(
            source_path=source_path,
            source_program=args.program,
            strict=not args.disable_strict_length_validation,
            spec_pdf_path=Path(args.spec_pdf) if args.spec_pdf else None,
        )
        _save_contract(contract=contract, output_path=contract_path)
        records, issues = _parse_with_contract(contract)
    save_jsonl(records=records, output_path=parsed_path)
    LOGGER.info("JSONL exported: %s (%s records)", parsed_path, len(records))
    if issues:
        LOGGER.warning("Parsing issues encountered: %s", len(issues))

    export_to_excel(records=records, output_path=excel_path, contract=contract)

    try:
        logo = Path(args.logo) if args.logo else None
        export_first_invoice_pdf(records=records, output_path=pdf_path, logo_path=logo)
    except (RuntimeError, ValueError) as error:
        LOGGER.warning("PDF not generated: %s", error)

    try:
        logo = Path(args.logo) if args.logo else None
        export_accounting_summary_pdf(records=records, output_path=accounting_pdf_path, logo_path=logo)
    except (RuntimeError, ValueError) as error:
        LOGGER.warning("Accounting summary PDF not generated: %s", error)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="idp470-pipeline",
        description="IDP470RA fixed-width extraction, parsing, Excel and PDF pipeline.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logs.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="Extract contract from source.")
    extract.add_argument("--source", required=True, help="Path to source file (PL/I, COBOL, JCL).")
    extract.add_argument(
        "--spec-pdf",
        default=None,
        help="Optional DOCTECHN PDF path used only for order/occurrence rules (section 3.2).",
    )
    extract.add_argument("--output", required=True, help="Output JSON contract path.")
    extract.add_argument("--program", default="IDP470RA", help="Source program name.")
    extract.add_argument("--provider", default="deterministic", choices=["deterministic", "openai", "anthropic"])
    extract.add_argument("--model", default=None, help="GenAI model name.")
    extract.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature for GenAI.")
    extract.add_argument("--fallback-deterministic", action="store_true", help="Fallback to deterministic parser if GenAI fails.")
    extract.add_argument("--source-encoding", default="latin-1", help="Source file encoding.")
    extract.add_argument(
        "--disable-strict-length-validation",
        action="store_true",
        help="Disable strict sum(length)==line_length check in generated contract.",
    )
    extract.set_defaults(handler=_extract_command)

    parse = subparsers.add_parser("parse", help="Parse fixed-width output with a JSON contract.")
    parse.add_argument("--contract", required=True, help="Contract JSON path.")
    parse.add_argument("--input", required=True, help="Mainframe output file path.")
    parse.add_argument("--output-jsonl", required=True, help="Output JSONL path.")
    parse.add_argument("--input-encoding", default="latin-1", help="Input file encoding.")
    parse.add_argument("--continue-on-error", action="store_true", help="Continue parsing when a line fails.")
    parse.set_defaults(handler=_parse_command)

    excel = subparsers.add_parser("excel", help="Export parsed JSONL to Excel.")
    excel.add_argument("--input-jsonl", required=True, help="Parsed JSONL input.")
    excel.add_argument("--output-xlsx", required=True, help="Excel output path.")
    excel.add_argument("--contract", default=None, help="Optional contract JSON path to enrich labels by zone.")
    excel.set_defaults(handler=_excel_command)

    pdf = subparsers.add_parser("pdf", help="Generate first-invoice PDF from parsed JSONL.")
    pdf.add_argument("--input-jsonl", required=True, help="Parsed JSONL input.")
    pdf.add_argument("--output-pdf", required=True, help="PDF output path.")
    pdf.add_argument("--logo", default=None, help="Optional logo path.")
    pdf.set_defaults(handler=_pdf_command)

    pdf_summary = subparsers.add_parser("pdf-summary", help="Generate accounting summary PDF from parsed JSONL.")
    pdf_summary.add_argument("--input-jsonl", required=True, help="Parsed JSONL input.")
    pdf_summary.add_argument("--output-pdf", required=True, help="PDF output path.")
    pdf_summary.add_argument("--logo", default=None, help="Optional logo path.")
    pdf_summary.set_defaults(handler=_pdf_summary_command)

    run = subparsers.add_parser("run", help="Run extraction + parsing + Excel + PDF.")
    run.add_argument("--source", required=True, help="Source code path for structure extraction (main source).")
    run.add_argument(
        "--spec-pdf",
        default=None,
        help="Optional DOCTECHN PDF path used only for order/occurrence rules (section 3.2).",
    )
    run.add_argument("--input", required=True, help="Mainframe output file path.")
    run.add_argument("--output-dir", default="outputs", help="Output directory.")
    run.add_argument("--contract", default=None, help="Optional contract path.")
    run.add_argument("--program", default="IDP470RA", help="Source program name.")
    run.add_argument("--provider", default="deterministic", choices=["deterministic", "openai", "anthropic"])
    run.add_argument("--model", default=None, help="GenAI model name.")
    run.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature for GenAI.")
    run.add_argument("--force-extract", action="store_true", help="Rebuild contract even if it already exists.")
    run.add_argument("--fallback-deterministic", action="store_true", help="Fallback to deterministic parser if GenAI fails.")
    run.add_argument("--source-encoding", default="latin-1", help="Source file encoding.")
    run.add_argument("--input-encoding", default="latin-1", help="Input file encoding.")
    run.add_argument("--continue-on-error", action="store_true", help="Continue parsing when a line fails.")
    run.add_argument("--logo", default=None, help="Optional logo path for PDF.")
    run.add_argument(
        "--disable-strict-length-validation",
        action="store_true",
        help="Disable strict sum(length)==line_length check in generated contract.",
    )
    run.set_defaults(handler=_run_command)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _setup_logging(verbose=args.verbose)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
