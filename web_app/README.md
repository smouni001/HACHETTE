# Version Web - FastAPI

Ce dossier fournit une version web (API + interface) en reutilisant le moteur `idp470_pipeline`.

## 1) Installation

Depuis la racine du projet:

```bash
python -m pip install -r requirements.txt
python -m pip install -r web_app/requirements.txt
```

## 2) Lancement

Toujours depuis la racine du projet:

```bash
python -m uvicorn web_app.backend.main:app --host 0.0.0.0 --port 8000 --reload
```

Ensuite ouvrir:

- `http://localhost:8000`

## 2.b) Lancement Docker local

Depuis la racine du projet:

```bash
docker compose up --build
```

Puis ouvrir:

- `http://localhost:8000`

## 3) Variables optionnelles

- `IDP470_WEB_SOURCE` chemin du programme source (defaut: `IDP470RA.pli`)
- `IDP470_WEB_SPEC_PDF` chemin du PDF de regles (defaut: `2785 - DOCTECHN - Dilifac - Format IDIL.pdf`)
- `IDP470_WEB_LOGO` chemin du logo (defaut: `assets/logo_hachette_livre.png`)
- `IDP470_WEB_JOBS_DIR` dossier de travail des jobs (defaut: `web_app/jobs`)
- `IDP470_WEB_INPUT_ENCODING` encodage FACDEMA (defaut: `latin-1`)
- `IDP470_WEB_CONTINUE_ON_ERROR` `true/false` (defaut: `false`)

## 4) API principale

- `GET /api/health`
- `POST /api/jobs` (form-data: `facdema_file`)
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/download/excel`
- `GET /api/jobs/{job_id}/download/pdf-factures`
- `GET /api/jobs/{job_id}/download/pdf-synthese`

## 5) Hebergement AWS (ECS Fargate)

Guide complet:

- `web_app/deploy/aws-ecs-fargate.md`
