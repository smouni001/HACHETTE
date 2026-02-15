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

- `IDP470_WEB_PROGRAMS_DIR` dossier des configurations programmes JSON (defaut: `web_app/programs`)
- `IDP470_WEB_SOURCE` fallback programme source si aucun JSON n'est charge (defaut: `IDP470RA.pli`)
- `IDP470_WEB_SOURCE_PROGRAM` fallback nom programme (defaut: `IDP470RA`)
- `IDP470_WEB_SPEC_PDF` fallback PDF de regles (defaut: `2785 - DOCTECHN - Dilifac - Format IDIL.pdf`)
- `IDP470_WEB_LOGO` chemin du logo (defaut: `assets/logo_hachette_livre.png`)
- `IDP470_WEB_JOBS_DIR` dossier de travail des jobs (defaut: `web_app/jobs`)
- `IDP470_WEB_INPUT_ENCODING` fallback encodage (defaut: `latin-1`)
- `IDP470_WEB_CONTINUE_ON_ERROR` fallback `true/false` (defaut: `false`)
- `IDP470_WEB_REUSE_CONTRACT` fallback `true/false` reutilise le contrat en memoire entre jobs (defaut: `true`)

## 4) API principale

- `GET /api/health`
- `GET /api/programs` (liste des programmes disponibles)
- `GET /api/catalog?program_id=<id>&advanced=<true|false>`
  - selection hierarchique: Programme -> Flux -> Fichier
  - par defaut: seulement les fichiers Factures
  - mode avance: `GET /api/catalog?advanced=true` pour afficher tous les fichiers
- `POST /api/jobs` (form-data: `program_id`, `flow_type`, `file_name`, `data_file`, `advanced_mode`)
  - en mode standard (`advanced_mode=false`), seuls les fichiers Factures sont acceptes
  - le backend bloque le chargement si la signature structurelle du fichier ne correspond pas au fichier logique choisi (validation stricte)
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/download/excel`
- `GET /api/jobs/{job_id}/download/pdf-factures`
- `GET /api/jobs/{job_id}/download/pdf-synthese`

Le catalogue retourne pour chaque fichier:

- type de flux (`input`/`output`)
- nom de fichier logique
- role metier detecte
- mode d'affichage (`invoice` ou `generic`)
- etat du mapping (`supports_processing`)
- structures IDP470RA associees (`raw_structures`)

## 4.2) Architecture technique (multi-programmes)

- Frontend:
  - selection `Programme source` puis `Type de flux` puis `Fichier logique`
  - mode standard (factures) + switch `Mode avance` (vue globale)
  - rendu conditionnel facture/non-facture
- Backend:
  - registre de programmes JSON (`web_app/programs/*.json`)
  - moteur d'analyse abstrait via champ `analyzer.engine` (actuel: `idp470_pli`)
  - extraction structurelle dynamique depuis le programme choisi
  - validation anti-erreur avant parsing (bloque les donnees non conformes)
  - parsing/export generique pilote par contrat

## 4.3) Exemple de configuration programme (JSON)

Un exemple est fourni dans:

- `web_app/programs/program_config.example.json`

Exemple minimal:

```json
{
  "program_id": "my_program",
  "display_name": "Mon Programme Mainframe",
  "source": {
    "path": "sources/MYPROG.pli",
    "program_name": "MYPROG",
    "encoding": "latin-1"
  },
  "analyzer": {
    "engine": "idp470_pli",
    "spec_pdf_path": "docs/spec_metier.pdf"
  },
  "ui_defaults": {
    "invoice_only": true,
    "default_flow_type": "output",
    "default_file_name": "FICDEMA"
  }
}
```

## 4.4) Logique de validation fichier charge

Avant tout parsing, le backend compare le contenu charge avec la structure attendue:

1. verification de la signature des enregistrements (selectors)
2. verification de la longueur mediane des lignes vs contrat
3. verification des tolerances selon type de flux (facture/generic)
4. suggestion d'un fichier logique probable en cas de mismatch

Si la correspondance echoue, le job est bloque avec erreur explicite.

## 4.1) Logique de switch conditionnel

Le backend applique automatiquement un switch metier:

1. Si le fichier est de type facture (`DEMAT_*` ou `STO_D_*`):
   - mode `invoice`
   - regles de structure IDIL actives
   - KPI facture (Clients, Factures, Lignes fichier)
   - generation Excel + PDF
2. Sinon:
   - mode `generic`
   - mapping flexible base sur les structures detectees dans IDP470RA
   - KPI generiques (Enregistrements, Types detectes, Champs structures)
   - generation Excel (PDF masque)

Pour les fichiers non mappables automatiquement, le catalogue les expose avec `supports_processing=false`.

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
