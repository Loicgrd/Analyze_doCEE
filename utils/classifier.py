"""
Classifier de dossier CEE.
Détecte la fiche BAR/BAT, le secteur et le coup de pouce.

Deux modes disponibles :
- Mode REGEX (rapide, gratuit, ~90% fiable) : classify_dossier()
- Mode IA   (fiable, ~0.001€/dossier)       : classify_dossier_ia()

Recommandation : utiliser classify_dossier_ia() en production.
Le mode regex peut servir de fallback si l'API est indisponible.
"""

import os
import re
import json
import anthropic
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# MODE IA — Classification par Haiku 4.5 (recommandé)
# ---------------------------------------------------------------------------

_CLASSIFIER_SYSTEM = """
Tu es un expert CEE. On te donne des extraits de documents d'un dossier de travaux.
Tu dois identifier la fiche BAR ou BAT applicable, le secteur et le contexte.

Réponds UNIQUEMENT en JSON valide, sans texte avant ou après, avec exactement ces clés :
{
  "fiche": "BAR-EN-105",
  "secteur": "BAR",
  "coup_de_pouce": false,
  "type_engagement": "ordre_de_service",
  "sous_traitance": false,
  "confiance": "haute",
  "raisonnement": "explication courte"
}

Valeurs possibles :
- fiche : code exact (ex: BAR-EN-101, BAR-TH-129, BAT-EN-102...) ou "INCONNUE"
- secteur : "BAR" (résidentiel) ou "BAT" (tertiaire)
- coup_de_pouce : true/false
- type_engagement : "ordre_de_service" | "bon_de_commande" | "acte_engagement" | "devis" | "inconnu"
- sous_traitance : true si un sous-traitant est mentionné
- confiance : "haute" | "moyenne" | "faible"
- raisonnement : 1 phrase expliquant le choix de la fiche

RÈGLE IMPORTANTE : si plusieurs fiches sont mentionnées dans les documents,
privilégie celle qui correspond réellement aux travaux décrits (nature des travaux,
matériaux, équipements), pas nécessairement celle écrite dans un formulaire.
Ex: si le VISA dit BAR-TH-130 mais que les travaux sont de l'isolation toiture
terrasse sur bâtiment existant, la bonne fiche est BAR-EN-105.
""".strip()


def classify_dossier_ia(
    docs: Dict[str, dict],
    model: str = "claude-haiku-4-5-20251001",
) -> Dict:
    """
    Classification fiable via Haiku 4.5.
    Coût : ~0.001€ par dossier. Temps : ~1-2s.

    Args:
        docs: dict {nom_fichier: {"text": str, "scanned": bool}}
        model: modèle à utiliser (Haiku par défaut)

    Returns:
        dict avec fiche, secteur, coup_de_pouce, type_engagement,
              sous_traitance, confiance, raisonnement
    """
    # Construire un extrait court et représentatif de chaque document
    extraits = []
    for name, doc in docs.items():
        text = doc.get("text", "")
        # On prend les 800 premiers caractères — suffisant pour la classification
        extrait = text[:800].replace("\n", " ").strip()
        if extrait:
            extraits.append(f"[{name.upper()}]\n{extrait}")

    if not extraits:
        return _fallback_classification()

    user_prompt = (
        "Voici les extraits des documents du dossier CEE à classifier :\n\n"
        + "\n\n".join(extraits)
        + "\n\nIdentifie la fiche BAR/BAT applicable et le contexte du dossier."
    )

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    try:
        response = client.messages.create(
            model=model,
            max_tokens=300,
            system=_CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text.strip()

        # Nettoyage robuste du JSON (parfois Haiku ajoute des backticks)
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        result = json.loads(raw)

        # Garantir toutes les clés attendues
        result.setdefault("fiche", "INCONNUE")
        result.setdefault("secteur", "BAR")
        result.setdefault("coup_de_pouce", False)
        result.setdefault("type_engagement", "inconnu")
        result.setdefault("sous_traitance", False)
        result.setdefault("confiance", "moyenne")
        result.setdefault("raisonnement", "")

        return result

    except (json.JSONDecodeError, Exception) as e:
        # Fallback regex si Haiku échoue
        fallback = classify_dossier_regex(docs)
        fallback["confiance"] = "faible"
        fallback["raisonnement"] = f"Fallback regex (erreur IA: {e})"
        return fallback


# ---------------------------------------------------------------------------
# MODE REGEX — Fallback rapide sans appel API
# ---------------------------------------------------------------------------

FICHE_PATTERNS = [
    (r"BAR-EN-101", "BAR-EN-101"), (r"BAR-EN-102", "BAR-EN-102"),
    (r"BAR-EN-103", "BAR-EN-103"), (r"BAR-EN-104", "BAR-EN-104"),
    (r"BAR-EN-105", "BAR-EN-105"), (r"BAR-EN-106", "BAR-EN-106"),
    (r"BAR-TH-104", "BAR-TH-104"), (r"BAR-TH-106", "BAR-TH-106"),
    (r"BAR-TH-107", "BAR-TH-107"), (r"BAR-TH-112", "BAR-TH-112"),
    (r"BAR-TH-113", "BAR-TH-113"), (r"BAR-TH-116", "BAR-TH-116"),
    (r"BAR-TH-127", "BAR-TH-127"), (r"BAR-TH-129", "BAR-TH-129"),
    (r"BAR-TH-130", "BAR-TH-130"), (r"BAR-TH-137", "BAR-TH-137"),
    (r"BAR-TH-143", "BAR-TH-143"), (r"BAR-TH-148", "BAR-TH-148"),
    (r"BAR-TH-159", "BAR-TH-159"), (r"BAR-TH-164", "BAR-TH-164"),
    (r"BAR-TH-169", "BAR-TH-169"), (r"BAR-TH-174", "BAR-TH-174"),
    (r"BAR-TH-175", "BAR-TH-175"), (r"BAR-TH-176", "BAR-TH-176"),
    (r"BAR-TH-177", "BAR-TH-177"), (r"BAT-EN-101", "BAT-EN-101"),
    (r"BAT-EN-102", "BAT-EN-102"), (r"BAT-EN-103", "BAT-EN-103"),
    (r"BAT-EN-104", "BAT-EN-104"), (r"BAT-TH-104", "BAT-TH-104"),
    (r"BAT-TH-116", "BAT-TH-116"), (r"BAREN-104",  "BAR-EN-104"),
]

KEYWORD_TO_FICHE = {
    "toiture terrasse": "BAR-EN-105",
    "isolation toiture terrasse": "BAR-EN-105",
    "comble perdu": "BAR-EN-101",
    "rampant de toiture": "BAR-EN-101",
    "isolation des murs": "BAR-EN-102",
    "isolation thermique par l'extérieur": "BAR-EN-102",
    "plancher bas": "BAR-EN-103",
    "fenêtre": "BAR-EN-104",
    "porte-fenêtre": "BAR-EN-104",
    "chaudière à condensation": "BAR-TH-106",
    "pompe à chaleur": "BAR-TH-129",
    "pac air/air": "BAR-TH-104",
    "pac air/eau": "BAR-TH-129",
    "réseau de chaleur": "BAR-TH-137",
    "ventilation mécanique": "BAR-TH-127",
    "vmc": "BAR-TH-127",
    "rénovation globale": "BAR-TH-177",
    "rénovation d'ampleur": "BAR-TH-174",
    "chauffe-eau thermodynamique": "BAR-TH-148",
    "chauffe-eau solaire": "BAR-TH-143",
    "robinet thermostatique": "BAR-TH-116",
    "calorifugeage": "BAR-TH-159",
}


def classify_dossier_regex(docs: Dict[str, dict]) -> Dict:
    """
    Classification par regex — rapide mais moins fiable.
    Utiliser comme fallback uniquement.
    """
    full_text = " ".join(d.get("text", "") for d in docs.values())
    full_text_lower = full_text.lower()

    fiche_detected = None
    for pattern, fiche_name in FICHE_PATTERNS:
        if re.search(pattern, full_text, re.IGNORECASE):
            fiche_detected = fiche_name
            break

    if not fiche_detected:
        for keyword, fiche_name in KEYWORD_TO_FICHE.items():
            if keyword in full_text_lower:
                fiche_detected = fiche_name
                break

    secteur = "BAT" if any(
        k in full_text_lower for k in ["tertiaire", "bat-", "bâtiment non résidentiel"]
    ) else "BAR"

    return {
        "fiche": fiche_detected or "INCONNUE",
        "secteur": secteur,
        "coup_de_pouce": any(k in full_text_lower for k in ["coup de pouce", "cdp", "charte"]),
        "type_engagement": _detect_engagement_type(full_text_lower),
        "sous_traitance": any(k in full_text_lower for k in ["sous-traitant", "dc4"]),
        "confiance": "moyenne",
        "raisonnement": "Classification par regex (sans IA)",
    }


def classify_dossier(docs: Dict[str, dict]) -> Dict:
    """
    Point d'entrée principal.
    Tente la classification IA (Haiku), bascule en regex si l'API est indisponible.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        result = classify_dossier_regex(docs)
        result["raisonnement"] = "ANTHROPIC_API_KEY absent — fallback regex"
        return result
    return classify_dossier_ia(docs)


def _detect_engagement_type(text_lower: str) -> str:
    if any(k in text_lower for k in ["ordre de service", "numéro d'os"]):
        return "ordre_de_service"
    if any(k in text_lower for k in ["bon de commande", "commande n°"]):
        return "bon_de_commande"
    if any(k in text_lower for k in ["acte d'engagement", "dpgf"]):
        return "acte_engagement"
    if "devis" in text_lower:
        return "devis"
    return "inconnu"


def _fallback_classification() -> Dict:
    return {
        "fiche": "INCONNUE",
        "secteur": "BAR",
        "coup_de_pouce": False,
        "type_engagement": "inconnu",
        "sous_traitance": False,
        "confiance": "faible",
        "raisonnement": "Aucun texte exploitable trouvé",
    }
