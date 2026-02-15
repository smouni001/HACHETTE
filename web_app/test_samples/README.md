# Exemples de fichiers de test (web)

Ce dossier contient des fichiers d'exemple prets a charger dans l'interface web.

## Utilisation rapide

1. Ouvrir l'application web.
2. Choisir `Type de flux` + `Fichier logique`.
3. Charger le fichier `sample_<type>_<fichier>.txt` correspondant.

Exemples:

- `output + FICDEMA` -> `sample_output_FICDEMA.txt`
- `output + FICLCOM` -> `sample_output_FICLCOM.txt`
- `input + FFAC3A` -> `sample_input_FFAC3A.txt`
- `input + FTRDA (IDP470F2)` -> `sample_input_FTRDA_IDP470F2.txt`
- `output + FTRSQ (IDP470F2)` -> `sample_output_FTRSQ_IDP470F2.txt`

## Manifest

Le fichier `samples_manifest.json` contient:

- le mapping `flow_type` / `file_name`
- le mode (`invoice` ou `generic`)
- la longueur des lignes
- les types d'enregistrement et structures detectees

## Regeneration

Pour regenerer tous les exemples depuis `IDP470RA`:

```bash
python web_app/test_samples/generate_samples.py
```
