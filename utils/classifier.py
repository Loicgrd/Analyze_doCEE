"""
Classifier de dossier CEE.
Détecte la fiche BAR/BAT, le secteur et le coup de pouce.

Trois modes disponibles :
- REGEX (rapide, gratuit, ~90% fiable SI le code fiche est écrit explicitement)
- IA (classify_dossier_ia) : reconnaissance sémantique de la nature des travaux
  à partir de la table officielle fiche<->travaux, quand aucun code n'est
  explicitement mentionné. Utilise Sonnet par défaut (meilleure reconnaissance
  que Haiku sur ce type de lecture de facture/devis).
- MANUEL : fiche imposée directement par l'utilisateur (voir analyzer.py --fiche
  ou le sélecteur dans app.py) — à privilégier quand le classifier échoue ou
  pour un contrôle total.

classify_dossier() bascule automatiquement selon la présence de la clé API.
"""

import os
import re
import json
from typing import Dict, Optional


def _build_classifier_system(correspondance_table: str = "") -> str:
    table_block = ""
    if correspondance_table:
        table_block = f"""
# NOMENCLATURE OFFICIELLE DES FICHES (à utiliser en priorité)
Voici la table officielle des fiches BAR/BAT avec leur libellé de travaux.
Base-toi sur CETTE liste pour associer les travaux décrits dans les documents
à un code fiche — n'invente jamais un code qui n'y figure pas.

{correspondance_table}
"""

    return f"""
Tu es un expert CEE. On te donne des extraits de documents d'un dossier de travaux
(facture, devis, DGD, ordre de service...).

Ta mission : identifier la fiche BAR ou BAT applicable en te basant sur :
1. Un code fiche explicitement écrit dans les documents (ex: "BAR-EN-105") si présent
2. À défaut, la **nature réelle des travaux décrits** (matériaux, épaisseurs,
   équipements, prestations facturées) mise en correspondance avec la
   nomenclature officielle ci-dessous — c'est le cas le plus fréquent, les
   documents mentionnent rarement le code fiche explicitement.
{table_block}
Réponds UNIQUEMENT en JSON valide, sans texte avant ou après, avec exactement ces clés :
{{
  "fiches": ["BAR-EN-105"],
  "secteur": "BAR",
  "coup_de_pouce": false,
  "type_engagement": "ordre_de_service",
  "sous_traitance": false,
  "confiance": "haute",
  "raisonnement": "explication courte citant les éléments des documents qui ont motivé ce choix"
}}

Valeurs possibles :
- fiches : LISTE de codes exacts présents dans la nomenclature fournie. La plupart des
  dossiers n'ont qu'UNE fiche -> liste à un seul élément (ex: ["BAR-EN-105"]). Mais un
  dossier peut couvrir PLUSIEURS fiches simultanément si plusieurs types de travaux
  distincts sont facturés/décrits pour un même marché (ex: un lot "chauffage-ventilation"
  qui inclut une chaudière (BAR-TH-106) ET une VMC (BAR-TH-127) ET des radiateurs
  (BAR-TH-110) ET un désembouage (BAR-SE-108) dans le même décompte de travaux).
  Liste ["INCONNUE"] si aucune correspondance raisonnable n'est identifiable.
  N'ajoute une fiche à la liste QUE si des éléments techniques concrets et distincts
  justifient chacune séparément — ne liste pas une fiche juste parce qu'elle apparaît
  sur un VISA déclaratif sans travaux correspondants dans les documents.
- secteur : "BAR" (résidentiel) ou "BAT" (tertiaire)
- coup_de_pouce : true/false
- type_engagement : "ordre_de_service" | "bon_de_commande" | "acte_engagement" | "devis" | "inconnu"
- sous_traitance : true si un sous-traitant est mentionné
- confiance : "haute" (code explicite ou nature des travaux sans ambiguïté) |
  "moyenne" (déduit de la nature des travaux, quelques incertitudes) |
  "faible" (peu d'éléments techniques exploitables)
- raisonnement : 1-2 phrases, cite les éléments concrets des documents (matériau,
  épaisseur, équipement...) qui justifient le choix de fiche

RÈGLE IMPORTANTE : si plusieurs fiches sont mentionnées dans les documents,
privilégie celle qui correspond réellement aux travaux décrits (nature des travaux,
matériaux, équipements), pas nécessairement celle écrite dans un formulaire
déclaratif type VISA. Ex: si le VISA dit BAR-TH-130 mais que les travaux sont de
l'isolation toiture terrasse sur bâtiment existant, la bonne fiche est BAR-EN-105.
""".strip()


def classify_dossier_ia(
    docs: Dict[str, dict],
    model: str = "claude-sonnet-4-6",
    correspondance_table: str = "",
    max_chars_per_doc: int = 2500,
) -> Dict:
    """
    Classification par IA — reconnaissance sémantique de la nature des travaux.

    Utilise Sonnet par défaut : la tâche (repérer un panneau isolant au milieu
    d'une facture multi-lignes, distinguer les prestations) demande davantage
    de raisonnement qu'une simple extraction, Sonnet est nettement plus fiable
    que Haiku sur ce type de lecture.

    Coût : ~0.01-0.02€ par dossier (vs ~0.001€ en Haiku) — reste marginal
    face au coût de l'analyse complète (~0.05€), pour un gain de fiabilité
    important quand le code fiche n'est pas écrit explicitement.

    Args:
        docs: dict {nom_fichier: {"text": str, "scanned": bool}}
        model: modèle à utiliser (Sonnet par défaut)
        correspondance_table: table fiche<->travaux (depuis RuleLoader), fortement
            recommandée pour éviter les hallucinations de code fiche
        max_chars_per_doc: extrait par document (plus large qu'avant pour capter
            les lignes techniques qui ne sont pas forcément en tête de document)

    Returns:
        dict avec fiche, secteur, coup_de_pouce, type_engagement,
              sous_traitance, confiance, raisonnement
    """
    import anthropic

    extraits = []
    for name, doc in docs.items():
        text = doc.get("text", "")
        extrait = text[:max_chars_per_doc].strip()
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
    system_prompt = _build_classifier_system(correspondance_table)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)

        result.setdefault("fiches", ["INCONNUE"])
        if "fiche" in result and "fiches" not in result:  # rétrocompatibilité si le modèle répond à l'ancien format
            result["fiches"] = [result.pop("fiche")]
        result.setdefault("secteur", "BAR")
        result.setdefault("coup_de_pouce", False)
        result.setdefault("type_engagement", "inconnu")
        result.setdefault("sous_traitance", False)
        result.setdefault("confiance", "moyenne")
        result.setdefault("raisonnement", "")
        return result

    except Exception as e:
        fallback = classify_dossier_regex(docs)
        fallback["confiance"] = "faible"
        fallback["raisonnement"] = f"Fallback regex (erreur IA: {e})"
        return fallback


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
    """Classification par regex — ne fonctionne que si le code fiche est écrit
    explicitement ou qu'un mot-clé générique évident est présent. Fallback uniquement."""
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
        "fiches": [fiche_detected] if fiche_detected else ["INCONNUE"],
        "secteur": secteur,
        "coup_de_pouce": any(k in full_text_lower for k in ["coup de pouce", "cdp", "charte"]),
        "type_engagement": _detect_engagement_type(full_text_lower),
        "sous_traitance": any(k in full_text_lower for k in ["sous-traitant", "dc4"]),
        "confiance": "moyenne",
        "raisonnement": "Classification par regex (sans IA) — fiable seulement si le code "
                         "fiche ou un mot-clé générique est explicitement écrit. Le regex ne "
                         "détecte qu'UNE fiche à la fois : si le dossier en couvre plusieurs, "
                         "utiliser la classification IA ou le mode manuel multi-fiches.",
    }


def classify_dossier(
    docs: Dict[str, dict],
    correspondance_table: str = "",
    use_correspondance_table: bool = True,
) -> Dict:
    """
    Point d'entrée principal.

    Stratégie : essaye d'abord le regex (gratuit, instantané). S'il trouve un
    code fiche explicite, on le garde tel quel (haute confiance, aucun coût).
    Sinon, bascule sur la classification IA (Sonnet) qui raisonne sur la
    nature des travaux — c'est le cas le plus fréquent en pratique, la
    plupart des documents ne citent pas le code fiche.

    Args:
        correspondance_table: table fiche<->travaux, à passer depuis
            RuleLoader.get_fiche_correspondance_table().
        use_correspondance_table: si False, la table n'est PAS envoyée au
            modèle même si fournie — Claude s'appuie alors uniquement sur sa
            connaissance générale des fiches CEE. Permet un test A/B pour
            vérifier empiriquement sur vos dossiers si la table change
            réellement les résultats (voir eval/run_eval.py --no-table).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    regex_result = classify_dossier_regex(docs)
    single_fiche = regex_result["fiches"][0] if regex_result["fiches"] else "INCONNUE"
    if single_fiche != "INCONNUE" and single_fiche in _known_fiches_from_patterns():
        # Code fiche trouvé explicitement dans le texte -> fiable, pas besoin d'IA.
        # Note : le regex ne peut confirmer qu'UNE fiche explicite à la fois. Si le
        # dossier en contient réellement plusieurs, l'IA (appelée si confiance jugée
        # insuffisante par l'utilisateur) ou le mode manuel doivent prendre le relai.
        regex_result["confiance"] = "haute"
        regex_result["raisonnement"] = ("Code fiche trouvé explicitement dans les documents. "
                                          "Si le dossier couvre plusieurs fiches, vérifier "
                                          "manuellement ou forcer le mode IA.")
        return regex_result

    if not api_key:
        regex_result["raisonnement"] = (
            "ANTHROPIC_API_KEY absent — fallback regex uniquement. "
            "La nature des travaux n'a pas pu être analysée sémantiquement : "
            "si aucun code fiche n'était écrit explicitement, ce résultat peut "
            "être peu fiable. Envisager --fiche pour l'indiquer manuellement."
        )
        return regex_result

    table_to_use = correspondance_table if use_correspondance_table else ""
    result = classify_dossier_ia(docs, correspondance_table=table_to_use)
    result["_table_utilisee"] = bool(table_to_use)
    return result


def _known_fiches_from_patterns() -> set:
    return {name for _, name in FICHE_PATTERNS}


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
