"""
Client API Claude pour l'analyse CEE — avec prompt caching + mode dry-run.

Architecture du prompt :
  [system + socle de règles MD]  ← bloc STABLE, marqué cache_control
  [fiche filtrée + docs dossier] ← bloc VARIABLE

Mode dry-run : assemble le prompt complet SANS appeler l'API.
Permet de vérifier gratuitement que l'extraction, la classification et le
chargement des règles fonctionnent avant de payer un vrai appel.
"""

import os
import time
from typing import Dict, Any

import anthropic


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
   date d'engagement que tu identifies dans les documents, et de le justifier.
3. Vérifier que TOUS les éléments techniques minimums de cette version précise de la
   fiche (résistance thermique, efficacité, puissance, ΔT, marque, référence, surface,
   quantité, certification produit...) sont présents ET conformes sur la PREUVE DE
   RÉALISATION elle-même (jamais uniquement sur l'AH — voir règle générale de
   `regles_ah.md`). Lire l'intégralité du document, y compris les annexes techniques
   multi-pages qui accompagnent souvent une facture ou un DGD.

## Temps 2 — Les règles de validation globales
Une fois le cœur technique établi, vérifier la cohérence et la conformité de
l'ensemble du dossier selon TOUTES les règles de validation fournies : validité
structurelle et métier de chaque document (engagement, réalisation, RGE, AH),
cohérence entre documents (liens forts), documents annexes requis, etc.
Un dossier peut avoir un cœur technique parfaitement valide et être NON VALIDE
ou INCOMPLET à cause d'un défaut sur ces règles de validation (document manquant,
signature absente, incohérence de prix...), et inversement.

# RÈGLES DE CONTRÔLE PAR POINT
Pour chaque point de contrôle :
1. RÈGLE BRUTE : quelle est l'exigence de base (règles fournies) ?
2. EXCEPTION / TOLÉRANCE : une alternative est-elle prévue par les règles ?
3. CONDITION DE LIEN : quelle condition stricte rend l'alternative recevable ?

# RÈGLES DE CITATION
- Chaque point bloquant DOIT citer le document source du dossier qui le justifie.
- Cite le nom exact des fichiers de règles entre parenthèses.
- Ne jamais inventer une règle : si non couvert, écris
  "Les documents fournis ne permettent pas de statuer sur ce cas."

# ATTENTION PARTICULIÈRE
- La fiche mentionnée sur le VISA est déclarative : vérifie qu'elle correspond
  à la nature réelle des travaux. En cas d'écart, signale-le comme anomalie.
- Une preuve de réalisation doit être un document FINAL (solde, DGD, facture
  finale). Les situations/acomptes partiels ne sont PAS conformes, sauf
  situation de solde à 100% identifiable comme dernier document du marché.
- Distingue PRGE et RGE complet sur les certificats Qualifelec.
- Le délai engagement → réalisation ne doit pas dépasser 12 mois (alerte non
  bloquante dès 10 mois).
- Les éléments techniques de l'engagement ne valident jamais l'éligibilité
  technique, SAUF un devis dans un montage "devis + PV de réception".

# FORMAT DE RÉPONSE OBLIGATOIRE
## FICHE APPLICABLE
[Fiche + justification]

## 1. LOGIQUE GLOBALE (documents présents/manquants)
## 2. VALIDATION ENGAGEMENT
## 3. VALIDATION RÉALISATION (dont éligibilité technique de la fiche)
## 4. VALIDATION RGE
## 5. VALIDATION AH
## 6. VALIDATION COHÉRENCE (liens engagement ↔ réalisation)
## 7. DOCUMENTS ANNEXES (si exigés par la fiche)

Pour chaque axe : verdict VALIDE / NON VALIDE / INCOMPLET
+ liste des éléments non valides ou manquants avec le document source cité.

## STATUT GLOBAL
VALIDE / NON VALIDE / INCOMPLET
(un seul axe non valide => dossier NON VALIDE ;
 un élément manquant sans non-conformité => INCOMPLET)
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
        + "\n\nProcède à l'audit complet selon le format imposé."
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
    """
    prompt = build_prompt(docs, core_rules_text, variable_rules_text, classification)

    system_chars = len(prompt["system"])
    core_chars = len(prompt["core_block"])
    var_chars = len(prompt["variable_block"])

    system_tk = system_chars // 4
    core_tk = core_chars // 4
    var_tk = var_chars // 4
    total_input_tk = system_tk + core_tk + var_tk

    # Estimation coût — 1er appel (cache write) vs appels suivants (cache read)
    cost_first_usd = (total_input_tk * 3) / 1_000_000
    cost_cached_usd = ((system_tk + core_tk) * 0.3 + var_tk * 3) / 1_000_000
    # + estimation output ~2000 tk
    output_tk_est = 2000
    cost_first_usd += (output_tk_est * 15) / 1_000_000
    cost_cached_usd += (output_tk_est * 15) / 1_000_000

    return {
        "prompt_system": prompt["system"],
        "prompt_core": prompt["core_block"],
        "prompt_variable": prompt["variable_block"],
        "tokens_estimation": {
            "system": system_tk,
            "core_socle": core_tk,
            "variable": var_tk,
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
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 3000,
    api_key: str = None,
) -> Dict[str, Any]:
    """Appelle l'API Claude avec prompt caching sur le socle de règles."""
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

    for attempt in range(3):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{
                    "type": "text",
                    "text": prompt["system"],
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_content}],
            )
            break
        except anthropic.RateLimitError:
            if attempt < 2:
                time.sleep((attempt + 1) * 10)
            else:
                raise

    analyse_text = response.content[0].text
    usage = response.usage

    return {
        "analyse": analyse_text,
        "statut": _extract_statut(analyse_text),
        "tokens_used": {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "total": usage.input_tokens + usage.output_tokens,
        },
    }


def _build_docs_section(docs: Dict[str, dict]) -> str:
    parts = ["# DOCUMENTS DU DOSSIER À ANALYSER"]
    for name, doc in docs.items():
        scanned = " [SCANNÉ - OCR]" if doc.get("scanned") else ""
        text = doc.get("text", "")
        if len(text) > 8000:
            text = text[:4000] + "\n\n[... tronqué ...]\n\n" + text[-4000:]
        parts.append(f"\n--- {name.upper()}{scanned} ---\n{text}")
    return "\n".join(parts)


def _extract_statut(analyse_text: str) -> str:
    text_upper = analyse_text.upper()
    idx = text_upper.find("STATUT GLOBAL")
    window = text_upper[idx:idx + 300] if idx != -1 else text_upper
    if "NON VALIDE" in window:
        return "NON VALIDE"
    if "INCOMPLET" in window:
        return "INCOMPLET"
    if "VALIDE" in window:
        return "VALIDE"
    return "INDÉTERMINÉ"
