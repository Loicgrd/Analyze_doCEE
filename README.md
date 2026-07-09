# CEE Analyzer — Analyseur de dossiers CEE

Analyse automatique de dossiers CEE avec chargement **sélectif** des règles
métier pour minimiser le coût en tokens (~8–12k tokens vs 80–120k si tout est chargé).

## Architecture

```
cee_analyzer/
├── analyzer.py          ← Point d'entrée principal
├── setup_rules.py       ← Script de configuration des règles
├── requirements.txt
└── utils/
    ├── extractor.py     ← Extraction texte PDF (+ OCR pour scans)
    ├── classifier.py    ← Détection fiche BAR/BAT sans API
    ├── rule_loader.py   ← Chargement sélectif des règles
    └── claude_client.py ← Appel API Claude + assemblage prompt
```

## Installation

```bash
pip install -r requirements.txt

# Outils système nécessaires (poppler)
# Ubuntu/Debian :
apt-get install poppler-utils tesseract-ocr tesseract-ocr-fra
# macOS :
brew install poppler tesseract
```

## Configuration

1. **Copier les règles** depuis votre dossier de grimoires :

```bash
python setup_rules.py --source /chemin/vers/grimoires --dest ./rules_data
```

2. **Configurer la clé API** :

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Utilisation

### En ligne de commande

```bash
# Analyser un ZIP
python analyzer.py ./dossiers/T155418.zip --rules ./rules_data -v

# Analyser un dossier de PDFs
python analyzer.py ./dossiers/T155418/ --rules ./rules_data

# Sauvegarder le résultat en JSON
python analyzer.py ./dossiers/T155418.zip --rules ./rules_data --output result.json
```

### En Python

```python
from analyzer import process_dossier

result = process_dossier(
    input_path="./dossiers/T155418.zip",
    rules_dir="./rules_data",
    verbose=True,
)

print(result["statut"])       # VALIDE / NON VALIDE / INCOMPLET
print(result["analyse"])      # Analyse complète structurée
print(result["tokens_used"])  # {"input": 9800, "output": 1200, "total": 11000}
```

### Intégration dans une API web (FastAPI)

```python
from fastapi import FastAPI, UploadFile
from analyzer import process_dossier
import tempfile, shutil

app = FastAPI()

@app.post("/analyze")
async def analyze(file: UploadFile):
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    result = process_dossier(tmp_path, rules_dir="./rules_data")
    return result
```

## Logique de chargement sélectif

| Toujours chargé | Conditionnel |
|---|---|
| `Règles_validation.csv` | Grimoire fiche (ex: `GRIMOIRE__BARTH130.pdf`) |
| `Doc_Récapitulatif_*.csv` (x5) | `GRIMOIRE__Soustraitance.pdf` si sous-traitant |
| `GRIMOIRE__Preuves_*.pdf` (x2) | `GRIMOIRE__Coup_de_pouce_*.pdf` si CDP |
| `GRIMOIRE__RGE.pdf` | `GRIMOIRE__Ordre_de_service.pdf` selon type engagement |
| `GRIMOIRE__Attestation.pdf` | CSV Fiche filtré sur la fiche cible |
| Fiche BAR/BAT (lignes filtrées) | |

**Résultat : ~8–12k tokens envoyés** au lieu de 80–120k si tous les grimoires
étaient inclus, soit une **réduction de ~90% du coût**.

## Variables d'environnement

| Variable | Description | Défaut |
|---|---|---|
| `ANTHROPIC_API_KEY` | Clé API Anthropic | *(obligatoire)* |

## Exemple de sortie

```
[1/4] Extraction de : T155418.zip
[2/4] Lecture de 4 PDF(s)...
   • 155418 visa: DEMANDE D'UN VISA TRAVAUX OU NEUF...
   • rea 155418: Décompte Général Définitif...
   • rge 155418: CERTIFICAT QUALIBAT « RGE »...
   • eng 155418 [SCANNÉ]: Seine-Saint-Denis habitat...
[3/4] Classification du dossier...
   → Fiche détectée : BAR-EN-105
   → Type secteur   : BAR
   → Coup de pouce  : False
[4/4] Analyse par Claude...
   → ~10,400 tokens envoyés

============================================================
RÉSULTAT D'ANALYSE
============================================================
Fiche applicable : BAR-EN-105
Statut           : INCOMPLET
Tokens utilisés  : {'input': 10400, 'output': 1850, 'total': 12250}

--- Analyse détaillée ---
## FICHE BAR/BAT APPLICABLE
BAR-EN-105 A39.3 — Isolation en toiture terrasse
...
```
