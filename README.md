# CEE Analyzer — Analyseur de dossiers CEE

Analyse automatique de dossiers CEE avec chargement **déterministe et sélectif**
des règles métier (socle Markdown mis en cache + fiche BAR/BAT filtrée).

## Installation

```bash
pip install -r requirements.txt
apt-get install poppler-utils tesseract-ocr tesseract-ocr-fra
```

## Configuration de la clé API — sécurité

**Sur Streamlit Community Cloud (recommandé pour le partage)** :
Paramètres de l'app → onglet **Secrets** :
```toml
ANTHROPIC_API_KEY = "sk-ant-..."
```
La clé n'apparaît jamais dans le code ni dans le repo Git.

**En local / serveur interne** :
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

**Jamais** en dur dans le code — même sur un repo privé.

## Tester sans payer — mode dry-run

L'app (et le CLI) proposent un **mode test** qui exécute tout le pipeline
(extraction, OCR, classification, chargement des règles, assemblage du prompt)
SANS appeler l'API Claude. Utile pour vérifier que l'extraction et le routage
des règles fonctionnent avant de payer un vrai appel.

```bash
python analyzer.py ./dossier.zip --rules ./rules_data --dry-run -v
```

Dans l'app Streamlit : activer le toggle "🧪 Mode test" dans la barre latérale.

> Anthropic offre généralement un crédit gratuit initial à la création d'un
> compte API — vérifiez le montant sur console.anthropic.com.
> Pensez aussi à définir une limite de dépense mensuelle sur la console pour
> vous protéger d'un usage excessif si l'app est partagée.

## Utilisation normale

```bash
python setup_rules.py --source /chemin/vers/grimoires --dest ./rules_data
export ANTHROPIC_API_KEY="sk-ant-..."
python analyzer.py ./dossier.zip --rules ./rules_data -v
```

```bash
streamlit run app.py
```

## Ajouter un grimoire (une fois réécrit en Markdown)

Déposer le fichier dans `rules_data/` — aucune modification de code requise :

| Nom du fichier | Comportement |
|---|---|
| `GRIMOIRE__NomGenerique.md` (pas de code fiche) | Rejoint le socle, chargé à chaque analyse (caché) |
| `GRIMOIRE__BARTH130.md` (code fiche détecté) | Chargé seulement quand cette fiche est identifiée |

## Évaluation de fiabilité

```bash
# 1. Déposer des ZIPs de test dans eval/dossiers/
# 2. Remplir eval/expected_results.json avec les verdicts attendus
python eval/run_eval.py --rules ./rules_data
```

## Architecture des tokens

| Bloc | Contenu | Taille | Cache |
|---|---|---|---|
| Socle | 6 règles Doc en Markdown | ~6 500 tk | ✅ Caché (10% du tarif dès le 2e appel) |
| Variable | Fiche(s) filtrée(s) + documents INTÉGRAUX du dossier | ~2 000 à 15 000 tk selon dossier | ❌ Plein tarif |

**Coût mesuré sur des dossiers réels : ~0,03 à 0,07 € par dossier.**

## OCR multi-pages

Les documents scannés volumineux (ex: DGD de 20+ pages) sont OCRisés avec
`ocr_pdf_smart()` : par défaut les 3 premières + 3 dernières pages (6 pages
max), car les informations utiles (identification, objet, totaux, signatures)
s'y trouvent le plus souvent. Ajustable via `max_pages_ocr` dans
`utils/extractor.py` si vos documents ont une structure différente.

**Limite connue** : si l'information clé (ex: code fiche BAR/BAT) se trouve
sur une page intermédiaire non couverte, ou si aucun document du dossier ne
mentionne explicitement la fiche, la classification retourne "INCONNUE" —
c'est un comportement volontaire (mieux vaut un statut explicite qu'une
fiche devinée à tort). L'app et le CLI affichent une alerte visible dans ce
cas, invitant à vérifier manuellement.

## Couverture documentaire (nouveau)

Chaque document est marqué dans le prompt **[DOCUMENT COMPLET]** ou
**[EXTRAIT PARTIEL : ...]** (pages non OCRisées, texte tronqué). Claude a
l'instruction explicite de ne JAMAIS conclure NON VALIDE sur la seule
absence d'un élément dans un extrait partiel : il renseigne
`hors_extrait_possible=true` sur l'élément, ajoute une anomalie, et tire le
verdict vers INCOMPLET (vérification humaine du document original).
L'app affiche une alerte dédiée listant ces éléments incertains.

## Recoupement date d'engagement / version de fiche (nouveau)

La date détectée par le classifier sélectionne la version de fiche chargée —
si elle était fausse, les mauvais seuils seraient vérifiés. Trois garde-fous :
1. Le prompt inclut désormais les **périodes d'application de TOUTES les
   versions** de chaque fiche (quelques dizaines de tokens).
2. Le schéma d'audit impose un champ `date_engagement_confirmee` : Claude
   ré-identifie lui-même la date et vérifie qu'elle tombe dans la période de
   la version chargée ; sinon, anomalie 'VERSION DE FICHE À REVÉRIFIER' et
   statut au mieux INCOMPLET.
3. L'app compare date classifier vs date audit et affiche une alerte rouge
   en cas de divergence.


## Classification de la fiche — 3 modes

Beaucoup de dossiers ne mentionnent **jamais explicitement** le code fiche
(BAR-EN-105...) dans leurs documents — c'est le cas le plus fréquent. Trois
stratégies, utilisables ensemble :

1. **Regex (gratuit, instantané)** : si le code est écrit tel quel dans un
   document, il est trouvé directement, sans appel API.
2. **IA sémantique (Sonnet, ~0,01-0,02€)** : si aucun code n'est trouvé, le
   classifier passe la nature des travaux décrits (matériaux, épaisseurs,
   équipements facturés) à Sonnet, avec la **nomenclature officielle**
   extraite de vos CSV Fiche Récapitulatif (`get_fiche_correspondance_table()`)
   pour éviter qu'il invente un code non existant.
3. **Manuel** : `--fiche BAR-EN-105` en CLI, ou le sélecteur "Manuelle" dans
   l'app Streamlit — contourne totalement la détection, recommandé si vous
   connaissez déjà le dossier ou si les deux modes précédents hésitent
   (`confiance: "faible"`).

## Note sur l'encodage des CSV

Les fichiers `Fiche_Récapitulatif_*.csv` sont en encodage **Mac Roman**
(export Excel Mac), pas Latin-1 comme on pourrait le supposer par défaut.
`RuleLoader._read_file()` détecte automatiquement le bon encodage parmi
plusieurs candidats (mac_roman, cp850, latin-1, utf-8) en choisissant celui
qui produit le moins d'erreurs de décodage — aucune configuration requise,
mais utile à savoir si vous ouvrez ces CSV vous-même dans un éditeur de code.
