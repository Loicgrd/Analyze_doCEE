"""
Client API Claude pour l'analyse CEE — avec prompt caching.

Architecture du prompt :
  [system + socle de règles MD]  ← bloc STABLE, marqué cache_control
  [fiche filtrée + docs dossier] ← bloc VARIABLE

Le socle (~7k tokens) est mis en cache par Anthropic : à partir du 2e appel
dans les 5 minutes, il est facturé à 10% du prix input (0,30$/M au lieu de 3$/M).
"""

import os
import time
from typing import Dict, Any

import anthropic


_SYSTEM_INSTRUCTIONS = """
Tu es un Expert Auditeur Senior en Certificats d'Économies d'Énergie (CEE),
spécialisé dans l'analyse de conformité des dossiers réglementaires.

# PROCESSUS OBLIGATOIRE
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
  finale). Les situations/acomptes partiels ne sont PAS conformes.
- Distingue PRGE et RGE complet sur les certificats Qualifelec.
- Le délai engagement → réalisation ne doit pas dépasser 12 mois.

# FORMAT DE RÉPONSE OBLIGATOIRE (structure de la règle de validation)
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


def analyze_with_claude(
    docs: Dict[str, dict],
    rules_bundle: Dict[str, str] = None,
    classification: Dict[str, Any] = None,
    core_rules_text: str = None,
    variable_rules_text: str = None,
    verbose: bool = False,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 3000,
) -> Dict[str, Any]:
    """
    Appelle l'API Claude avec prompt caching sur le socle de règles.

    Deux modes d'appel :
    - Nouveau (recommandé) : core_rules_text + variable_rules_text
    - Legacy : rules_bundle (dict) — reconstruit les deux blocs
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    classification = classification or {}

    # Reconstruction legacy si nécessaire
    if core_rules_text is None and rules_bundle:
        core_parts, var_parts = [], []
        for label, content in rules_bundle.items():
            block = f"--- {label.split(':', 1)[-1]} ---\n{content}"
            (core_parts if label.startswith("CORE:") else var_parts).append(block)
        core_rules_text = "\n\n".join(core_parts)
        variable_rules_text = "\n\n".join(var_parts)

    docs_text = _build_docs_section(docs)

    context_info = f"""# CONTEXTE PRÉ-ANALYSÉ (à vérifier et corriger si besoin)
- Fiche probable : {classification.get('fiche', 'INCONNUE')}
- Secteur : {classification.get('secteur', 'BAR')}
- Type d'engagement : {classification.get('type_engagement', 'inconnu')}
- Coup de pouce : {'Oui' if classification.get('coup_de_pouce') else 'Non'}
- Sous-traitance : {'Oui' if classification.get('sous_traitance') else 'Non'}"""

    # --- Prompt avec caching ---
    # Bloc 1 (system) : instructions — stable
    # Bloc 2 (user, cache_control) : socle de règles MD — stable
    # Bloc 3 (user) : fiche filtrée + contexte + documents — variable
    user_content = [
        {
            "type": "text",
            "text": "# RÈGLES MÉTIER CEE (SOCLE)\n\n" + (core_rules_text or ""),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": (
                "# RÈGLES SPÉCIFIQUES À LA FICHE\n\n"
                + (variable_rules_text or "(aucune)")
                + "\n\n" + context_info
                + "\n\n" + docs_text
                + "\n\nProcède à l'audit complet selon le format imposé."
            ),
        },
    ]

    if verbose:
        est = (len(_SYSTEM_INSTRUCTIONS) + sum(len(b["text"]) for b in user_content)) // 4
        print(f"   → Estimation tokens envoyés : ~{est:,}")

    for attempt in range(3):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{
                    "type": "text",
                    "text": _SYSTEM_INSTRUCTIONS,
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
