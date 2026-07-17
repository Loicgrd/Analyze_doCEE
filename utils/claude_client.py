"""
Client API Claude pour l'analyse CEE — sortie structurée (tool use) + prompt caching + mode dry-run.

Architecture du prompt :
  [system + socle de règles MD]  ← bloc STABLE, marqué cache_control
  [fiche filtrée + docs dossier] ← bloc VARIABLE

Architecture de la réponse :
  Le schéma JSON de l'outil "produire_audit_cee" est FIXE et universel (ne
  change jamais d'un appel à l'autre). Ce qui varie par fiche, c'est le
  contenu du prompt (checklist des champs atomiques attendus, générée par
  utils/technical_schema.py et injectée via rule_loader) — pas la forme du
  schéma JSON lui-même. Claude est contraint via tool_choice à produire
  cette structure, éliminant le parsing fragile de Markdown par regex/split.

Mode dry-run : assemble le prompt complet SANS appeler l'API.
Permet de vérifier gratuitement que l'extraction, la classification et le
chargement des règles fonctionnent avant de payer un vrai appel.
"""

import os
import time
from typing import Dict, Any, List

import anthropic

from utils.extractor import smart_truncate


# ---------------------------------------------------------------------------
# SCHÉMA FIXE de l'outil d'audit — ne varie jamais selon la fiche ou le
# dossier. Le détail de CE QUI doit être vérifié (quels champs atomiques,
# quelles conditions techniques) vient du PROMPT, pas de ce schéma.
# ---------------------------------------------------------------------------

_VERDICT_ENUM = ["VALIDE", "NON VALIDE", "INCOMPLET"]

# ---------------------------------------------------------------------------
# Tarifs utilisés pour les ESTIMATIONS (dry-run + affichage app), en $/MTok.
# Valeurs = tarif STANDARD de claude-sonnet-5 (applicable à partir du
# 01/09/2026, identique au tarif Sonnet 4.6). Jusqu'au 31/08/2026, le tarif
# de lancement de Sonnet 5 est de 2$/10$ : les estimations ci-dessous
# SURESTIMENT donc le coût réel d'environ 33% pendant cette période — choix
# volontairement conservateur pour ne pas sous-budgéter après le 31/08.
# ---------------------------------------------------------------------------
PRICE_INPUT_USD_MTOK = 3.0
PRICE_OUTPUT_USD_MTOK = 15.0
PRICE_CACHE_READ_USD_MTOK = 0.3  # 10% du tarif input

_ELEMENT_TECHNIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "champ": {
            "type": "string",
            "description": ("Nom du champ atomique (utiliser exactement le nom donné dans la "
                             "checklist de la fiche, ex: 'marque_reference', 'etas_pourcent'), "
                             "ou le libellé exact de l'exigence si elle ne correspond à aucun "
                             "champ standard (section 'elements_specifiques' de la checklist)."),
        },
        "present": {
            "type": "boolean",
            "description": "L'élément est-il présent sur la preuve de réalisation ?",
        },
        "valeur_trouvee": {
            "type": ["string", "null"],
            "description": "Valeur exacte (marque, référence, R, etc.) telle qu'elle apparaît dans le document, null si absent.",
        },
        "citation_verbatim": {
            "type": ["string", "null"],
            "description": (
                "OBLIGATOIRE si present=true : la ligne ou phrase COMPLÈTE du document "
                "d'où provient valeur_trouvee, copiée mot pour mot (pas reformulée, pas "
                "résumée). Cette citation doit porter en elle-même de quoi vérifier que "
                "la valeur concerne bien LE BON composant/équipement -- pas juste que la "
                "valeur existe quelque part dans le document. Ex: si tu extrais une marque "
                "pour un isolant, cite la ligne qui mentionne explicitement l'isolant "
                "('Marque : URSA' seul ne suffit pas si la ligne ne précise pas de quoi il "
                "s'agit -- inclus le titre de section ou la mention du composant juste "
                "avant/après si nécessaire pour que la citation soit auto-suffisante)."
            ),
        },
        "conforme": {
            "type": ["boolean", "null"],
            "description": ("Conforme au seuil minimum de la fiche (ex: R trouvé >= R minimum "
                             "requis) ? null si non applicable ou non évaluable (ex: simple "
                             "présence d'une marque, pas de seuil à comparer)."),
        },
        "source": {
            "type": ["string", "null"],
            "description": "Document et emplacement (ex: 'Facture p.4, annexe technique, section isolation combles').",
        },
        "hors_extrait_possible": {
            "type": ["boolean", "null"],
            "description": ("À remplir UNIQUEMENT si present=false : true si le document où cet "
                             "élément était attendu est marqué [EXTRAIT PARTIEL] (pages non OCRisées "
                             "ou texte tronqué) — l'élément pourrait se trouver dans une partie non "
                             "fournie et son absence n'est PAS certaine. false si le document est "
                             "marqué [DOCUMENT COMPLET] (absence définitive). null si present=true."),
        },
    },
    "required": ["champ", "present"],
}

_CONTROLE_SCHEMA = {
    "type": "object",
    "properties": {
        "item": {"type": "string", "description": "Intitulé du point de contrôle vérifié."},
        "verdict": {"type": "boolean", "description": "Ce point de contrôle est-il satisfait ?"},
        "details": {"type": ["string", "null"], "description": "Précision courte si utile (valeur trouvée, écart constaté...)."},
        "source": {"type": ["string", "null"], "description": "Document source justifiant le verdict."},
    },
    "required": ["item", "verdict"],
}

_AXE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": _VERDICT_ENUM},
        "controles": {
            "type": "array",
            "items": _CONTROLE_SCHEMA,
            "description": "Liste des points de contrôle vérifiés pour cet axe.",
        },
    },
    "required": ["verdict", "controles"],
}

AUDIT_TOOL_SCHEMA = {
    "name": "produire_audit_cee",
    "description": ("Produit le résultat structuré de l'audit de conformité d'un dossier CEE. "
                     "Doit être appelé une seule fois, à la fin de l'analyse complète."),
    "input_schema": {
        "type": "object",
        "properties": {
            "fiches": {
                "type": "array",
                "description": "Une entrée par fiche BAR/BAT applicable au dossier (plusieurs si dossier multi-fiches).",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Code fiche, ex: BAR-EN-105"},
                        "version_applicable": {"type": "string", "description": "Version retenue, ex: A54.5, avec la date d'engagement qui justifie ce choix."},
                        "elements_techniques": {
                            "type": "array",
                            "items": _ELEMENT_TECHNIQUE_SCHEMA,
                            "description": "Un objet par champ de la checklist fournie dans le prompt pour cette fiche, plus les éléments spécifiques.",
                        },
                        "verdict_technique": {"type": "string", "enum": _VERDICT_ENUM},
                    },
                    "required": ["code", "elements_techniques", "verdict_technique"],
                },
            },
            "axes": {
                "type": "object",
                "description": "Les axes de validation globale du dossier (Temps 2 du processus d'audit).",
                "properties": {
                    "logique_globale": _AXE_SCHEMA,
                    "engagement": _AXE_SCHEMA,
                    "realisation_documentaire": {
                        **_AXE_SCHEMA,
                        "description": "Validité structurelle/métier du document de réalisation (hors éligibilité technique, déjà couverte dans fiches[].elements_techniques).",
                    },
                    "rge": _AXE_SCHEMA,
                    "ah": _AXE_SCHEMA,
                    "coherence": {
                        **_AXE_SCHEMA,
                        "description": "Cohérence engagement <-> réalisation (lien fort : numéro marché+adresse OU prix HT).",
                    },
                    "documents_annexes": _AXE_SCHEMA,
                },
                "required": ["logique_globale", "engagement", "realisation_documentaire", "rge", "ah", "coherence"],
            },
            "date_engagement_confirmee": {
                "type": ["string", "null"],
                "description": ("Date d'engagement (JJ/MM/AAAA) que TU as identifiée toi-même dans les "
                                 "documents, indépendamment du 'CONTEXTE PRÉ-ANALYSÉ'. null si réellement "
                                 "introuvable. IMPÉRATIF : si cette date diffère de celle du contexte "
                                 "pré-analysé, OU si elle sort de la période d'application de la version "
                                 "de fiche chargée dans les règles (périodes listées dans le bloc de "
                                 "règles), les seuils vérifiés proviennent potentiellement de la MAUVAISE "
                                 "version — ajoute une anomalie explicite 'VERSION DE FICHE À REVÉRIFIER' "
                                 "et le statut global ne peut alors pas être VALIDE (au mieux INCOMPLET)."),
            },
            "date_realisation": {
                "type": ["string", "null"],
                "description": ("Date de réalisation/achèvement des travaux (JJ/MM/AAAA) identifiée sur "
                                 "la PREUVE DE RÉALISATION : date d'achèvement des travaux si mentionnée, "
                                 "sinon date de la facture finale ou du DGD. null si introuvable. "
                                 "Rappel : elle doit être POSTÉRIEURE à la date d'engagement — sinon, "
                                 "anomalie majeure à signaler."),
            },
            "professionnel_realisation": {
                "type": ["string", "null"],
                "description": ("Identité du professionnel ayant RÉALISÉ les travaux, telle qu'elle "
                                 "figure sur la PREUVE DE RÉALISATION : raison sociale + SIRET si "
                                 "disponible (ex: 'SOPREMA ENTREPRISES — SIRET 485 197 552 00071'). "
                                 "null si non identifiable."),
            },
            "sous_traitant": {
                "type": ["string", "null"],
                "description": ("Raison sociale + SIRET du SOUS-TRAITANT si les travaux ont été "
                                 "sous-traités (mention explicite sur la facture, le BC ou l'AH). "
                                 "null si pas de sous-traitance. RAPPEL : en cas de sous-traitance, "
                                 "c'est le sous-traitant qui doit porter la qualification RGE quand "
                                 "la fiche l'exige."),
            },
            "adresse_travaux": {
                "type": ["string", "null"],
                "description": ("Adresse complète du LIEU DES TRAVAUX telle que mentionnée sur la "
                                 "preuve de réalisation — à recouper avec l'adresse du document "
                                 "d'engagement (une divergence est une anomalie de cohérence). "
                                 "null si introuvable."),
            },
            "montant_ht": {
                "type": ["string", "null"],
                "description": ("Montant total HT des travaux sur la preuve de réalisation (ex: "
                                 "'92 646,00 €'). Sert au LIEN FORT engagement ↔ réalisation : doit "
                                 "correspondre au montant du document d'engagement (ou s'expliquer : "
                                 "avenants, révision de prix...). null si introuvable."),
            },
            "anomalies": {
                "type": "array",
                "items": {"type": "string"},
                "description": ("Anomalies signalées mais non bloquantes (ex: écart de numéro de "
                                 "dossier ODICEE, mention VISA à corriger). NE PAS inclure la "
                                 "mention BAR-TH-130 liée à la case 'bâtiment neuf' non cochée "
                                 "(faux positif connu, à ignorer totalement, cf. règles fournies)."),
            },
            "statut_global": {
                "type": "string",
                "enum": _VERDICT_ENUM,
                "description": "Un seul axe ou une seule fiche NON VALIDE => dossier NON VALIDE. Un élément manquant sans non-conformité => INCOMPLET.",
            },
            "synthese_narrative": {
                "type": "string",
                "description": "Résumé libre de 4 à 8 phrases pour une lecture humaine rapide : fiche(s), verdict global, points bloquants principaux.",
            },
        },
        "required": ["fiches", "axes", "date_engagement_confirmee", "date_realisation",
                      "professionnel_realisation", "sous_traitant", "adresse_travaux",
                      "statut_global", "synthese_narrative"],
    },
}


_SYSTEM_INSTRUCTIONS = """
Tu es un Expert Auditeur Senior en Certificats d'Économies d'Énergie (CEE),
spécialisé dans l'analyse de conformité des dossiers réglementaires.

# PROCESSUS D'AUDIT EN DEUX TEMPS (OBLIGATOIRE)

## Temps 1 — Le cœur technique : fiche, version, éligibilité
1. Identifier la ou les fiches BAR/BAT applicables au(x) type(s) de travaux du dossier.
2. Identifier la DATE D'ENGAGEMENT du dossier (sur la preuve d'engagement, ou à défaut
   le VISA). Cette date détermine la VERSION de la fiche applicable — les fiches CEE
   changent de version dans le temps, avec des critères techniques différents.
   Le bloc "RÈGLES SPÉCIFIQUES À LA FICHE" ci-dessous a normalement déjà été filtré
   sur la version correspondant à cette date. S'il indique "TOUTES VERSIONS" (date non
   déterminée automatiquement) ou "AMBIGU" (plusieurs versions se chevauchent sur cette
   date), c'est à TOI de trancher explicitement quelle version s'applique à partir de la
   date d'engagement que tu identifies dans les documents, et de le justifier dans
   "version_applicable". Si aucune date d'engagement n'est identifiable nulle part dans
   les documents, retiens la version la PLUS RÉCENTE disponible par défaut, et
   signale-le explicitement dans les anomalies.
   RECOUPEMENT OBLIGATOIRE : remplis toujours "date_engagement_confirmee" avec la date
   que TU as toi-même identifiée dans les documents. Le bloc de règles liste les
   périodes d'application de TOUTES les versions de chaque fiche : vérifie que ta date
   confirmée tombe bien dans la période de la version dont les seuils t'ont été
   fournis. Si ce n'est pas le cas (date différente de celle du contexte pré-analysé,
   ou hors période), les seuils que tu vérifies proviennent peut-être de la MAUVAISE
   version : ajoute l'anomalie 'VERSION DE FICHE À REVÉRIFIER' et ne rends jamais un
   statut global VALIDE dans cette situation (au mieux INCOMPLET).
3. Pour chaque fiche, remplir "elements_techniques" en suivant EXACTEMENT la checklist
   de champs atomiques fournie dans le bloc de règles de cette fiche (noms de champs
   imposés). Vérifier que chaque élément est présent ET conforme au seuil minimum sur
   la PREUVE DE RÉALISATION elle-même (jamais uniquement sur l'AH — voir règle générale
   de `regles_ah.md`). Lire tout le texte fourni du document, y compris les annexes
   techniques multi-pages qui accompagnent souvent une facture ou un DGD, en tenant
   compte de sa COUVERTURE (voir section dédiée ci-dessous).

## Temps 2 — Les règles de validation globales
Une fois le cœur technique établi, vérifier la cohérence et la conformité de
l'ensemble du dossier selon TOUTES les règles de validation fournies, en
remplissant chaque axe de "axes" avec ses points de contrôle ("controles").
Un dossier peut avoir un cœur technique parfaitement valide et être NON VALIDE
ou INCOMPLET à cause d'un défaut sur ces règles de validation (document manquant,
signature absente, incohérence de prix...), et inversement.

# COUVERTURE DOCUMENTAIRE (CRITIQUE)
Chaque document du dossier est marqué [DOCUMENT COMPLET] ou [EXTRAIT PARTIEL : ...] :
- [DOCUMENT COMPLET] : tout le texte du document t'est fourni. Un élément absent
  est RÉELLEMENT absent du document — conclusion définitive autorisée.
- [EXTRAIT PARTIEL] : des pages n'ont pas été OCRisées ou le texte a été tronqué
  (coupures marquées [...] ou [... N page(s) non OCRisée(s) ...]). Un élément
  attendu mais introuvable dans un tel extrait POURRAIT se trouver dans une partie
  non fournie. Dans ce cas : mets present=false ET hors_extrait_possible=true,
  ajoute une anomalie précisant le document et les pages/sections manquantes, et
  tire le verdict vers INCOMPLET (vérification humaine du document original
  requise) — JAMAIS vers NON VALIDE sur la seule base de cette absence incertaine.
  Ne conclus NON VALIDE que sur une non-conformité POSITIVE (valeur présente mais
  sous le seuil, incohérence avérée...), pas sur une absence dans un extrait partiel.

# RÈGLES DE CONTRÔLE PAR POINT
Pour chaque point de contrôle :
1. RÈGLE BRUTE : quelle est l'exigence de base (règles fournies) ?
2. EXCEPTION / TOLÉRANCE : une alternative est-elle prévue par les règles (logique "OU") ?
3. CONDITION DE LIEN : quelle condition stricte rend l'alternative recevable ?

# DISTINCTION OBLIGATOIRE / NÉCESSAIRE (IMPORTANT)
Chaque fiche fournit deux listes d'éléments techniques distinctes :
- **Champs OBLIGATOIRES** : leur absence sur la preuve de réalisation rend
  cet élément non conforme (present=false), potentiellement bloquant pour
  le verdict technique de la fiche.
- **Champs NÉCESSAIRES (non obligatoires sur la preuve de réalisation)** :
  leur absence sur la preuve de réalisation NE rend PAS ce document non
  conforme en tant que tel — mais l'information reste OBLIGATOIRE pour
  juger le DOSSIER dans son ensemble pleinement conforme. Cherche ces
  éléments partout dans le dossier (facture, annexe technique, AH...) avant
  de les déclarer absents. S'ils sont réellement introuvables nulle part
  dans le dossier, ne les ignore JAMAIS silencieusement au prétexte que la
  colonne source n'est pas "obligatoire" : signale l'absence (present=false)
  et reflète-la dans le verdict technique de la fiche (typiquement INCOMPLET
  plutôt que NON VALIDE, sauf si les règles de la fiche indiquent le contraire).

# RÈGLES DE CITATION
- Chaque contrôle non satisfait DOIT préciser sa source dans le champ "source"
  ou "details" (document + emplacement).
- Ne jamais inventer une valeur : si une information est absente, mets "present": false
  et "valeur_trouvee": null plutôt que d'improviser une valeur plausible.
- Pour chaque "elements_techniques" avec present=true, remplis "citation_verbatim"
  avec la ligne exacte du document (copiée mot pour mot). Le risque principal
  n'est PAS d'inventer une valeur qui n'existe nulle part -- c'est de prendre
  une valeur RÉELLE mais qui concerne un AUTRE poste que celui demandé.
  Exemple concret : sur une facture d'isolation, la ligne "Marque : XYZ" peut
  concerner l'enduit, la colle, les fixations, le pare-vapeur ou le panneau
  isolant lui-même selon sa position dans la facture -- vérifie TOUJOURS le
  titre de section ou la ligne de désignation juste avant pour confirmer à
  quel composant la valeur se rapporte réellement, avant de l'attribuer au
  champ atomique concerné. Une facture multi-lignes (ITE, VMC, chauffage...)
  contient presque toujours plusieurs marques/références différentes pour
  des composants différents (isolant, treillis, colle, régulateur, caisson,
  bouches...) -- ne jamais prendre la première valeur du bon TYPE rencontrée
  sans vérifier qu'elle concerne le bon COMPOSANT.

# ATTENTION PARTICULIÈRE
- La fiche mentionnée sur le VISA est déclarative : vérifie qu'elle correspond
  à la nature réelle des travaux. Le libellé "BAR-TH-130" imprimé par défaut à
  côté de la case "Construction d'un bâtiment neuf" (non cochée) est un FAUX
  POSITIF CONNU — ne jamais le retenir comme fiche ni le lister en anomalie.
- Une preuve de réalisation doit être un document FINAL (solde, DGD, facture
  finale). Les situations/acomptes partiels ne sont PAS conformes, sauf
  situation de solde à 100% identifiable comme dernier document du marché.
- Distingue PRGE et RGE complet sur les certificats Qualifelec.
- Le délai engagement → réalisation ne doit pas dépasser 12 mois (alerte non
  bloquante dès 10 mois, à mettre dans "anomalies" si applicable).
- Les éléments techniques de l'engagement ne valident jamais l'éligibilité
  technique, SAUF un devis dans un montage "devis + PV de réception".

# SORTIE
Termine TOUJOURS ton analyse par UN SEUL appel à l'outil "produire_audit_cee"
avec le résultat structuré complet. Ne produis pas de texte libre en dehors
de cet appel d'outil.
""".strip()


def build_prompt(
    docs: Dict[str, dict],
    core_rules_text: str,
    variable_rules_text: str,
    classification: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Assemble le prompt complet (system + user) sans appeler l'API.
    Utilisé à la fois par analyze_with_claude() et par le mode dry-run.
    """
    docs_text = _build_docs_section(docs)

    context_info = f"""# CONTEXTE PRÉ-ANALYSÉ (à vérifier et corriger si besoin)
- Fiche(s) probable(s) : {', '.join(classification.get('fiches', [classification.get('fiche', 'INCONNUE')]))}
- Date d'engagement détectée : {classification.get('date_engagement') or 'NON DÉTERMINÉE — à identifier impérativement dans les documents pour sélectionner la bonne version de fiche'}
- Secteur : {classification.get('secteur', 'BAR')}
- Type d'engagement : {classification.get('type_engagement', 'inconnu')}
- Coup de pouce : {'Oui' if classification.get('coup_de_pouce') else 'Non'}
- Sous-traitance : {'Oui' if classification.get('sous_traitance') else 'Non'}"""

    core_block = "# RÈGLES MÉTIER CEE (SOCLE)\n\n" + (core_rules_text or "")
    variable_block = (
        "# RÈGLES SPÉCIFIQUES À LA FICHE\n\n"
        + (variable_rules_text or "(aucune)")
        + "\n\n" + context_info
        + "\n\n" + docs_text
        + "\n\nProcède à l'audit complet et appelle l'outil produire_audit_cee avec le résultat structuré."
    )

    return {
        "system": _SYSTEM_INSTRUCTIONS,
        "core_block": core_block,
        "variable_block": variable_block,
    }


def dry_run(
    docs: Dict[str, dict],
    core_rules_text: str,
    variable_rules_text: str,
    classification: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Assemble le prompt SANS appeler l'API. Gratuit.
    Retourne le prompt complet + une estimation de tokens et de coût.
    Note : l'appel réel utilise une sortie structurée (tool use, schéma
    AUDIT_TOOL_SCHEMA) plutôt qu'un texte libre — l'estimation d'output ici
    reste approximative (le JSON structuré peut être plus ou moins verbeux
    que la prose selon le nombre de fiches et de contrôles à documenter).
    """
    prompt = build_prompt(docs, core_rules_text, variable_rules_text, classification)

    system_chars = len(prompt["system"])
    core_chars = len(prompt["core_block"])
    var_chars = len(prompt["variable_block"])

    # Facteurs de calibration du tokenizer Sonnet 5, MESURÉS sur un appel réel
    # (dossier T249185, 07/2026) en comparant l'estimation chars/4 aux tokens
    # facturés par l'API :
    #   - blocs de PROSE française (system + socle de règles + schéma outil) :
    #     facturé 1,35x l'estimation chars/4 (borne haute documentée par
    #     Anthropic pour la prose avec le nouveau tokenizer) ;
    #   - bloc VARIABLE (documents extraits + référentiel structuré) : 0,9x —
    #     le nouveau tokenizer est légèrement plus efficace sur le contenu
    #     structuré ; on garde 1,0 par prudence.
    _F_PROSE = 1.35
    _F_VARIABLE = 1.0

    system_tk = int(system_chars // 4 * _F_PROSE)
    core_tk = int(core_chars // 4 * _F_PROSE)
    var_tk = int(var_chars // 4 * _F_VARIABLE)
    # Le schéma de l'outil est envoyé à chaque appel comme les autres blocs input.
    schema_tk = int(len(str(AUDIT_TOOL_SCHEMA)) // 4 * _F_PROSE)
    total_input_tk = system_tk + core_tk + var_tk + schema_tk

    # Estimation coût — 1er appel (cache write, facturé 1,25x le tarif input)
    # vs appels suivants (cache read à 0,1x). Le préfixe caché couvre le schéma
    # d'outil + system + socle (tout ce qui précède le point de rupture cache).
    cached_prefix_tk = system_tk + core_tk + schema_tk
    cost_first_usd = (cached_prefix_tk * PRICE_INPUT_USD_MTOK * 1.25
                       + var_tk * PRICE_INPUT_USD_MTOK) / 1_000_000
    cost_cached_usd = (cached_prefix_tk * PRICE_CACHE_READ_USD_MTOK
                        + var_tk * PRICE_INPUT_USD_MTOK) / 1_000_000
    # Sortie : calibrée sur appel réel Sonnet 5 (T249185 : 5 239 tk pour 1 fiche,
    # soit ~2x l'ancienne estimation) — l'adaptive thinking, activé par défaut,
    # est facturé en tokens de sortie et double environ le volume.
    n_fiches = max(1, len(classification.get("fiches", ["1"])))
    output_tk_est = (1800 + n_fiches * 700) * 2
    cost_first_usd += (output_tk_est * PRICE_OUTPUT_USD_MTOK) / 1_000_000
    cost_cached_usd += (output_tk_est * PRICE_OUTPUT_USD_MTOK) / 1_000_000

    return {
        "prompt_system": prompt["system"],
        "prompt_core": prompt["core_block"],
        "prompt_variable": prompt["variable_block"],
        "schema_outil": AUDIT_TOOL_SCHEMA,
        "tokens_estimation": {
            "system": system_tk,
            "core_socle": core_tk,
            "variable": var_tk,
            "schema_outil": schema_tk,
            "total_input": total_input_tk,
            "output_estime": output_tk_est,
        },
        "cout_estime_eur": {
            "premier_appel": round(cost_first_usd * 0.92, 4),
            "appels_suivants_avec_cache": round(cost_cached_usd * 0.92, 4),
        },
        "mode": "dry_run",
    }


def analyze_with_claude(
    docs: Dict[str, dict],
    rules_bundle: Dict[str, str] = None,
    classification: Dict[str, Any] = None,
    core_rules_text: str = None,
    variable_rules_text: str = None,
    verbose: bool = False,
    model: str = "claude-sonnet-5",  # tarif de lancement 2$/10$ par MTok jusqu au 31/08/2026, puis 3$/15$ (= tarif Sonnet 4.6)
    effort: str = "high",  # profondeur de raisonnement adaptatif : low|medium|high|xhigh|max.
    # "high" (défaut API) recommandé pour l audit : les recoupements croisés multi-documents,
    # l arbitrage de version et les comparaisons de seuils bénéficient directement du thinking.
    # "medium" = plus rapide et moins cher (thinking facturé en output), à ne descendre
    # qu après validation sur le harnais d éval (eval/run_eval.py).
    max_tokens: int = 6000,
    api_key: str = None,
) -> Dict[str, Any]:
    """
    Appelle l'API Claude avec prompt caching sur le socle de règles, et
    contraint la réponse au schéma structuré AUDIT_TOOL_SCHEMA via tool_choice.

    max_tokens relevé par rapport à l'ancien format prose (3000 -> 6000) :
    la sortie JSON structurée d'un dossier multi-fiches peut être plus
    volumineuse (plusieurs objets fiches + tous les axes détaillés).
    """
    client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
    classification = classification or {}

    if core_rules_text is None and rules_bundle:
        core_parts, var_parts = [], []
        for label, content in rules_bundle.items():
            block = f"--- {label.split(':', 1)[-1]} ---\n{content}"
            (core_parts if label.startswith("CORE:") else var_parts).append(block)
        core_rules_text = "\n\n".join(core_parts)
        variable_rules_text = "\n\n".join(var_parts)

    prompt = build_prompt(docs, core_rules_text, variable_rules_text, classification)

    user_content = [
        {
            "type": "text",
            "text": prompt["core_block"],
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": prompt["variable_block"],
        },
    ]

    if verbose:
        est = (len(prompt["system"]) + len(prompt["core_block"]) + len(prompt["variable_block"])) // 4
        print(f"   → Estimation tokens envoyés : ~{est:,}")

    MAX_TOKENS_CAP = 16000
    current_max_tokens = max_tokens
    response = None
    reponse_tronquee = False

    for attempt in range(4):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=current_max_tokens,
                system=[{
                    "type": "text",
                    "text": prompt["system"],
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_content}],
                tools=[AUDIT_TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": "produire_audit_cee"},
                output_config={"effort": effort},
            )
        except anthropic.RateLimitError:
            if attempt < 3:
                time.sleep((attempt + 1) * 10)
                continue
            raise
        except anthropic.APIStatusError as e:
            # 529 (overloaded) / 5xx transitoires : même traitement que le rate limit.
            if getattr(e, "status_code", 0) in (500, 502, 503, 529) and attempt < 3:
                time.sleep((attempt + 1) * 10)
                continue
            raise

        # Réponse coupée par max_tokens : le tool_use serait incomplet/absent et
        # le statut retomberait silencieusement en INDÉTERMINÉ. On relance UNE
        # fois avec un budget doublé (dossier multi-fiches volumineux) plutôt
        # que de rendre un résultat vide inexpliqué.
        if response.stop_reason == "max_tokens" and current_max_tokens < MAX_TOKENS_CAP:
            if verbose:
                print(f"   ⚠️ Réponse tronquée à {current_max_tokens} tokens — "
                      f"relance avec {min(MAX_TOKENS_CAP, current_max_tokens * 2)}")
            current_max_tokens = min(MAX_TOKENS_CAP, current_max_tokens * 2)
            continue

        reponse_tronquee = (response.stop_reason == "max_tokens")
        break

    audit_data = _extract_tool_use(response)
    if audit_data:
        audit_data = verify_citations(audit_data, docs)
    usage = response.usage

    return {
        "audit": audit_data,
        "statut": audit_data.get("statut_global", "INDÉTERMINÉ") if audit_data else "INDÉTERMINÉ",
        "analyse": audit_data.get("synthese_narrative", "") if audit_data else (
            "⚠️ Réponse API tronquée (limite de tokens atteinte malgré la relance) — "
            "résultat inexploitable, relancer l'analyse." if reponse_tronquee else ""
        ),
        "reponse_tronquee": reponse_tronquee,
        "tokens_used": {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "total": usage.input_tokens + usage.output_tokens,
        },
    }


def _extract_tool_use(response) -> Dict[str, Any]:
    """
    Extrait le contenu structuré du bloc tool_use de la réponse API.
    Avec tool_choice forcé, ce bloc est garanti présent en usage normal ;
    on reste défensif (dossier vide) en cas de réponse inattendue (ex:
    troncature par max_tokens atteint avant la fin de l'appel d'outil).
    """
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "produire_audit_cee":
            return block.input
    return {}


def verify_citations(audit_data: Dict[str, Any], docs: Dict[str, dict]) -> Dict[str, Any]:
    """
    Vérification a posteriori, sans appel API supplémentaire (coût nul) : pour
    chaque élément technique avec present=true, contrôle que "citation_verbatim"
    apparaît réellement (avec une tolérance sur les espaces/ponctuation) dans
    le texte d'un des documents envoyés à Claude.

    Portée réelle de cette vérification : elle détecte une citation FABRIQUÉE
    (qui n'existe nulle part dans les documents) -- pas une MAUVAISE
    ATTRIBUTION (citation réelle mais rattachée au mauvais composant). Ce
    deuxième risque, plus fréquent en pratique, reste à la charge du prompt
    (voir _SYSTEM_INSTRUCTIONS) et d'une relecture humaine de la citation
    affichée dans l'app -- cette fonction est un filet de sécurité
    complémentaire, pas une garantie de justesse d'attribution.

    Ajoute un champ "citation_verifiee": bool sur chaque élément technique
    et ne modifie rien d'autre (n'écrase aucune donnée, ne bloque rien).
    """
    import re as _re

    def _normalise(s: str) -> str:
        return _re.sub(r"\s+", " ", s.lower().strip())

    full_corpus = _normalise(" ".join(d.get("text", "") for d in docs.values()))

    for fiche_obj in audit_data.get("fiches", []):
        for el in fiche_obj.get("elements_techniques", []):
            citation = el.get("citation_verbatim")
            if not el.get("present") or not citation:
                el["citation_verifiee"] = None  # non applicable
                continue
            citation_norm = _normalise(citation)
            if citation_norm in full_corpus:
                el["citation_verifiee"] = True
            else:
                # Repli tolérant aux erreurs OCR : cherche au moins une fenêtre
                # de mots CONSÉCUTIFS de la citation présente telle quelle dans
                # le corpus. Bien plus discriminant que l'ancien critère "80%
                # des mots présents dans le désordre n'importe où" : une
                # citation fabriquée à partir de vocabulaire CEE courant
                # ('marque référence isolant résistance thermique') passait
                # presque toujours l'ancien test, alors qu'elle échoue à celui-ci.
                mots = citation_norm.split()
                if len(mots) < 4:
                    # Citation trop courte pour un test par fenêtre fiable :
                    # exiger le match exact (déjà échoué ci-dessus).
                    el["citation_verifiee"] = False
                else:
                    n = min(6, len(mots))
                    el["citation_verifiee"] = any(
                        " ".join(mots[i:i + n]) in full_corpus
                        for i in range(len(mots) - n + 1)
                    )

    return audit_data


# ---------------------------------------------------------------------------
# Budget documentaire : par défaut, TOUT le texte extrait est envoyé (les
# éléments techniques d'une facture se trouvent souvent dans les annexes,
# une troncature fixe par document coûtait des verdicts INCOMPLET à tort
# pour économiser ~0,004 € de tokens). La troncature ne s'applique QUE si le
# corpus total du dossier dépasse le budget global ci-dessous (~34k tokens,
# soit ~0,10 € d'input plein tarif) — cas pathologique, toujours signalé via
# les marqueurs [EXTRAIT PARTIEL]. En cas de dépassement, les preuves de
# réalisation (facture/DGD) sont servies en priorité et tronquées en dernier.
# ---------------------------------------------------------------------------
_DOCS_GLOBAL_MAX_CHARS = 120_000
_DOC_MIN_CHARS = 4_000
_PREUVE_KEYWORDS = ("facture", "dgd", "decompte", "décompte", "situation", "solde")


def _is_preuve_realisation(name: str) -> bool:
    return any(kw in name.lower() for kw in _PREUVE_KEYWORDS)


def _allocate_doc_budgets(docs: Dict[str, dict]) -> Dict[str, int]:
    """
    Répartit le budget global de caractères entre les documents.
    Si tout tient dans le budget : chaque doc reçoit sa longueur complète.
    Sinon : parcours par priorité (preuves de réalisation d'abord), chaque
    doc reçoit le maximum possible en réservant _DOC_MIN_CHARS à chacun des
    documents restants (aucun document n'est jamais totalement évincé).
    """
    total = sum(len(d.get("text", "")) for d in docs.values())
    if total <= _DOCS_GLOBAL_MAX_CHARS:
        return {name: len(d.get("text", "")) for name, d in docs.items()}

    ordered = sorted(docs.keys(), key=lambda n: (not _is_preuve_realisation(n), n))
    budgets = {}
    remaining = _DOCS_GLOBAL_MAX_CHARS
    for i, name in enumerate(ordered):
        n_rest = len(ordered) - i - 1
        length = len(docs[name].get("text", ""))
        cap = max(_DOC_MIN_CHARS, remaining - n_rest * _DOC_MIN_CHARS)
        budgets[name] = min(length, cap)
        remaining -= budgets[name]
    return budgets


def _build_docs_section(docs: Dict[str, dict]) -> str:
    """
    Assemble la section documents du prompt, avec pour CHAQUE document un
    marqueur de couverture explicite [DOCUMENT COMPLET] ou [EXTRAIT PARTIEL:...].

    Sans ce marqueur, Claude ne peut pas distinguer "absent du document" et
    "absent de l'extrait fourni" (le system prompt lui interdit désormais de
    conclure NON VALIDE sur une absence dans un extrait partiel). Les
    métadonnées de couverture viennent d'extractor.extract_document() ; si
    elles sont absentes (ancien appelant), la troncature appliquée ici est
    quand même détectée et signalée.
    """
    budgets = _allocate_doc_budgets(docs)

    parts = ["# DOCUMENTS DU DOSSIER À ANALYSER",
             "(chaque document indique sa couverture : COMPLET ou EXTRAIT PARTIEL "
             "— voir la section 'COUVERTURE DOCUMENTAIRE' des instructions)"]
    for name, doc in docs.items():
        scanned = " [SCANNÉ - OCR]" if doc.get("scanned") else ""
        text = doc.get("text", "")
        # smart_truncate préserve chaque section de fiche détectée (utile pour
        # les documents multi-fiches comme une AH à plusieurs parties A) au
        # lieu d'une troncature naïve tête+queue qui risquerait d'en effacer
        # une entièrement si elle se trouve au milieu d'un document long.
        truncated_text = smart_truncate(text, max_chars=budgets.get(name, len(text)))

        notes = []
        if doc.get("couverture"):
            notes.append(doc["couverture"])
        elif doc.get("truncated"):
            notes.append("couverture partielle (détail non disponible), coupures marquées [...]")
        if len(truncated_text) < len(text):
            notes.append(f"extrait re-tronqué à ~{budgets.get(name, 0):,} caractères "
                         f"(budget global du dossier dépassé), coupures marquées [...]")

        if notes:
            coverage = f" [EXTRAIT PARTIEL : {' ; '.join(notes)}]"
        else:
            coverage = " [DOCUMENT COMPLET]"

        parts.append(f"\n--- {name.upper()}{scanned}{coverage} ---\n{truncated_text}")
    return "\n".join(parts)
