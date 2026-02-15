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
- `IDP470_WEB_INPUT_ENCODING` encodage fichier source (defaut: `latin-1`)
- `IDP470_WEB_CONTINUE_ON_ERROR` `true/false` (defaut: `false`)
- `IDP470_WEB_FAST_EXCEL` `true/false` export Excel rapide (defaut: `true`)
- `IDP470_WEB_REUSE_CONTRACT` `true/false` reutilise le contrat en memoire entre jobs (defaut: `true`)

## 4) API principale

- `GET /api/health`
- `GET /api/catalog` (liste des flux/fichiers disponibles)
- `POST /api/jobs` (form-data: `flow_type`, `file_name`, `data_file`)
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/download/excel`
- `GET /api/jobs/{job_id}/download/pdf-factures`
- `GET /api/jobs/{job_id}/download/pdf-synthese`

Profils disponibles par defaut:

- `output / FICDEMA` (layout `DEMAT_*`)
- `output / FICSTOD` (layout `STO_D_*`)
- `input / FFAC3A` (layout `WTFAC`)

## 5) Hebergement AWS (ECS Fargate)

Guide complet:

- `web_app/deploy/aws-ecs-fargate.md`

## 6) Hebergement Render (gratuit)

Le repo contient deja un blueprint `render.yaml` a la racine.

### Deploiement en 1 clic (Blueprint)

1. Ouvrir Render Dashboard
2. `New` -> `Blueprint`
3. Connecter le repo GitHub `smouni001/HACHETTE`
4. Selectionner la branche `main`
5. Valider le service `idil-papyrus-web` (plan `free`)
6. Lancer le deploy

Render utilisera automatiquement:

- `Dockerfile` a la racine
- `render.yaml` (variables env + healthcheck `/api/health`)

### Apres deploiement

- Ouvrir l'URL Render
- Verifier `https://<votre-app>.onrender.com/api/health`
- Selectionner un flux puis charger le fichier correspondant depuis l'interface
- Verifier les telechargements Excel/PDF

Note:

- Le plan gratuit peut se mettre en veille (cold start au premier appel)
- Le stockage local n'est pas persistant en gratuit
