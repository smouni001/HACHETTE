from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .models import ContractSpec

LOGGER = logging.getLogger(__name__)


class GenAIExtractionError(RuntimeError):
    pass


@dataclass
class GenAISettings:
    provider: str = "openai"
    model: str | None = None
    temperature: float = 0.0


_MAX_SOURCE_CHARS_FOR_PROMPT = 120_000


def _candidate_secret_files() -> list[Path]:
    candidates: list[Path] = []

    explicit_file = os.getenv("IDP470_SECRETS_FILE")
    if explicit_file:
        candidates.append(Path(explicit_file).expanduser())

    candidates.extend(
        [
            Path.cwd() / ".streamlit" / "secrets.toml",
            Path.cwd() / "secrets.local.toml",
            Path.home() / ".idp470" / "secrets.toml",
        ]
    )

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        marker = str(path.resolve()) if path.exists() else str(path)
        if marker not in seen:
            seen.add(marker)
            unique.append(path)
    return unique


@lru_cache(maxsize=1)
def _load_secure_file_values() -> dict[str, str]:
    values: dict[str, str] = {}

    def register(key: str, val: object) -> None:
        if not isinstance(val, str):
            return
        stripped = val.strip()
        if not stripped:
            return
        values[key] = stripped
        values[key.upper()] = stripped
        values[key.replace(".", "_").upper()] = stripped

    for file_path in _candidate_secret_files():
        if not file_path.exists():
            continue
        try:
            with file_path.open("rb") as handle:
                payload = tomllib.load(handle)
        except (tomllib.TOMLDecodeError, OSError) as error:
            LOGGER.warning("Unable to read secrets file %s: %s", file_path, error)
            continue

        for key, val in payload.items():
            register(key, val)
            if isinstance(val, dict):
                for nested_key, nested_val in val.items():
                    register(nested_key, nested_val)
                    register(f"{key}.{nested_key}", nested_val)

    return values


def _get_secure_file_value(name: str) -> str | None:
    values = _load_secure_file_values()
    if name in values:
        return values[name]
    normalized = name.upper()
    if normalized in values:
        return values[normalized]
    return None


def _get_env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value

    # Windows fallback: read persisted user environment variables from registry.
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                raw, _ = winreg.QueryValueEx(key, name)
                if isinstance(raw, str) and raw:
                    return raw
        except OSError:
            pass

    secure_value = _get_secure_file_value(name)
    if secure_value:
        return secure_value
    return None


def _extract_first_json_block(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise GenAIExtractionError("No JSON object found in GenAI response.")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise GenAIExtractionError(f"GenAI response JSON parsing failed: {error}") from error


def _reduce_source_for_prompt(source_text: str) -> str:
    if len(source_text) <= _MAX_SOURCE_CHARS_FOR_PROMPT:
        return source_text

    keep_head = 90_000
    keep_tail = 30_000
    head = source_text[:keep_head]
    tail = source_text[-keep_tail:]
    notice = (
        "\n\n/* --- SOURCE TRUNCATED FOR TOKEN LIMITS --- */\n"
        "/* Keep first and last sections only */\n\n"
    )
    return f"{head}{notice}{tail}"


def _build_prompt(source_program: str, source_text: str) -> str:
    reduced_source = _reduce_source_for_prompt(source_text)
    return f"""
Tu es un expert Mainframe (PL/I, COBOL, JCL) et parsing fixed-width.
Analyse le code source ci-dessous et extrais la structure des enregistrements de sortie.

Tu dois renvoyer UNIQUEMENT un JSON valide avec ce schéma logique:
{{
  "schema_version": "1.0",
  "source_program": "{source_program}",
  "line_length": 1200,
  "strict_length_validation": true,
  "record_types": [
    {{
      "name": "ENT",
      "selector": {{"start": 1, "length": 3, "value": "ENT"}},
      "fields": [
        {{"name": "TYPEN", "start": 1, "length": 3, "type": "string", "decimals": null, "description": "Type enregistrement"}}
      ]
    }}
  ]
}}

Contraintes:
- Positions 1-based.
- Aucun chevauchement de champs.
- Inclure les fillers pour que sum(length) == line_length par type d'enregistrement.
- Types autorisés: string, integer, decimal, date, sign.
- Pour les décimaux implicites, renseigner "decimals" (ex: PIC 9V99 => decimals=2).
- Les sélecteurs de type d'enregistrement doivent être cohérents avec le champ type (ENT/COM/LIG/etc.).
- Renseigner "description" pour chaque champ avec le libellé métier lu dans les commentaires source.

Source à analyser:
```text
{reduced_source}
```
""".strip()


def _extract_with_openai(prompt: str, model: str, temperature: float) -> dict:
    try:
        from openai import OpenAI
    except ModuleNotFoundError as error:
        raise GenAIExtractionError(
            "openai package not installed. Install with: pip install openai"
        ) from error

    api_key = _get_env_value("OPENAI_API_KEY")
    if not api_key:
        raise GenAIExtractionError("Missing OPENAI_API_KEY environment variable.")
    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=temperature,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "Return valid JSON only. No markdown, no explanations.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content or ""
        return _extract_first_json_block(content)
    except Exception as error:
        raise GenAIExtractionError(f"OpenAI extraction failed: {error}") from error


def _extract_with_anthropic(prompt: str, model: str, temperature: float) -> dict:
    try:
        from anthropic import Anthropic
    except ModuleNotFoundError as error:
        raise GenAIExtractionError(
            "anthropic package not installed. Install with: pip install anthropic"
        ) from error

    api_key = _get_env_value("ANTHROPIC_API_KEY")
    if not api_key:
        raise GenAIExtractionError("Missing ANTHROPIC_API_KEY environment variable.")

    client = Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            temperature=temperature,
            system="Return valid JSON only. No markdown, no explanations.",
            messages=[{"role": "user", "content": prompt}],
        )
        blocks = getattr(response, "content", []) or []
        parts: list[str] = []
        for block in blocks:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
        content = "\n".join(parts)
        return _extract_first_json_block(content)
    except Exception as error:
        raise GenAIExtractionError(f"Anthropic extraction failed: {error}") from error


def extract_contract_with_genai(
    source_program: str,
    source_text: str,
    settings: GenAISettings,
) -> ContractSpec:
    provider = settings.provider.lower()
    model = settings.model

    if provider == "openai":
        model = model or "gpt-4.1-mini"
    elif provider == "anthropic":
        model = model or "claude-3-7-sonnet-20250219"
    else:
        raise GenAIExtractionError(f"Unsupported provider: {settings.provider}")

    prompt = _build_prompt(source_program=source_program, source_text=source_text)
    LOGGER.info("Extracting structure with provider=%s model=%s", provider, model)

    if provider == "openai":
        payload = _extract_with_openai(prompt=prompt, model=model, temperature=settings.temperature)
    else:
        payload = _extract_with_anthropic(prompt=prompt, model=model, temperature=settings.temperature)

    try:
        return ContractSpec.model_validate(payload)
    except Exception as error:
        raise GenAIExtractionError(f"Invalid contract returned by GenAI: {error}") from error
