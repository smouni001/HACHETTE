# Pipeline IDP470RA - Parsing Mainframe vers Excel/PDF

Ce module fournit un pipeline en 3 couches:

1. Extraction de structure depuis le programme source (principalement `IDP470RA.pli`)
2. Parsing generique fixed-width pilote par contrat JSON
3. Exports Excel et PDF

Le PDF DOCTECHN (`2785 - DOCTECHN - Dilifac - Format IDIL.pdf`) est utilise pour enrichir le contrat avec:

- ordre des enregistrements
- occurrences par niveau (fichier/facture/ligne)

Reference: section 3.2 (`FIC, ENT, ECH, COM, REF(E), ADR, AD2, LIG, REF(L), LEC`).

## Installation

```bash
python -m pip install -r requirements.txt
```

## 1) Extraction du contrat JSON

### Option A - Deterministe (recommande)

Source principale: programme PL/I.
PDF: uniquement pour les regles d'ordre/occurrence.

```bash
python -m idp470_pipeline extract ^
  --provider deterministic ^
  --source IDP470RA.pli ^
  --spec-pdf "2785 - DOCTECHN - Dilifac - Format IDIL.pdf" ^
  --output contracts/idp470ra_contract.json
```

### Option B - GenAI OpenAI

Variable requise: `OPENAI_API_KEY`

```bash
python -m idp470_pipeline extract ^
  --provider openai ^
  --model gpt-4.1-mini ^
  --source IDP470RA.pli ^
  --spec-pdf "2785 - DOCTECHN - Dilifac - Format IDIL.pdf" ^
  --output contracts/idp470ra_contract.json ^
  --fallback-deterministic
```

### Option C - GenAI Claude (Anthropic)

Variable requise: `ANTHROPIC_API_KEY`

```bash
python -m idp470_pipeline extract ^
  --provider anthropic ^
  --model claude-3-7-sonnet-20250219 ^
  --source IDP470RA.pli ^
  --spec-pdf "2785 - DOCTECHN - Dilifac - Format IDIL.pdf" ^
  --output contracts/idp470ra_contract.json ^
  --fallback-deterministic
```

## 2) Parsing FACDEMA

```bash
python -m idp470_pipeline parse ^
  --contract contracts/idp470ra_contract.json ^
  --input facdemat_20251021_nufac29501954.txt ^
  --output-jsonl outputs/parsed_records.jsonl
```

Validations:

- longueur de ligne
- selecteur de type d'enregistrement
- coherence du contrat
- validation structurelle (ordre/occurrences) selon les regles section 3.2

## 3) Export Excel

```bash
python -m idp470_pipeline excel ^
  --input-jsonl outputs/parsed_records.jsonl ^
  --output-xlsx outputs/parsed_records.xlsx ^
  --contract contracts/idp470ra_contract.json
```

## 4) Generation PDF

```bash
python -m idp470_pipeline pdf ^
  --input-jsonl outputs/parsed_records.jsonl ^
  --output-pdf outputs/facture_exemple.pdf
```

## Pipeline complet

```bash
python -m idp470_pipeline run ^
  --provider deterministic ^
  --source IDP470RA.pli ^
  --spec-pdf "2785 - DOCTECHN - Dilifac - Format IDIL.pdf" ^
  --input facdemat_20251021_nufac29501954.txt ^
  --output-dir outputs
```

Sorties:

- `outputs/idp470ra_contract.json`
- `outputs/parsed_records.jsonl`
- `outputs/parsed_records.xlsx`
- `outputs/facture_exemple.pdf`

## Streamlit

```bash
streamlit run streamlit_app.py
```

Configuration interne:

- `config/ui_settings.toml`
- source principale: `default_source_path = "IDP470RA.pli"`
- PDF de structure: `default_spec_pdf_path = "2785 - DOCTECHN - Dilifac - Format IDIL.pdf"`

## Secrets API

Le code lit les cles depuis:

- `IDP470_SECRETS_FILE`
- `.streamlit/secrets.toml`
- `secrets.local.toml`
- `%USERPROFILE%\.idp470\secrets.toml`
