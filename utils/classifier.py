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

Ta mission : identifier la fiche BAR ou BAT applicable en te basant EN PRIORITÉ
sur la **nature réelle des travaux décrits sur la facture / DGD** (matériaux,
épaisseurs, équipements, prestations facturées), mise en correspondance avec
la nomenclature officielle ci-dessous.

C'est la méthode principale, pas un repli : sur un marché de travaux normal,
la facture ne mentionne QUASIMENT JAMAIS de code fiche explicite — seuls
certains documents propres à des bailleurs sociaux (AH, VISA) le font, et pas
systématiquement. Un dossier sans AH ni VISA doit être classifié avec la même
fiabilité qu'un dossier qui en a, à partir du seul contenu de la facture.

Un code fiche explicitement écrit quelque part (AH, VISA...) est un indice
complémentaire utile, mais PAS une vérité à recopier aveuglément :
- Le VISA porte parfois des mentions déclaratives non fiables. Cas connu :
  le libellé "opération standardisée BAR-TH-130" est imprimé par défaut à
  côté de la case "Construction d'un bâtiment neuf" sur certains modèles de
  VISA, même quand cette case n'est PAS cochée (bâtiment existant). Si la
  case "Bâtiment existant depuis plus de 2 ans" est cochée, IGNORE
  totalement cette mention BAR-TH-130 — ce n'est pas une fiche du dossier.
- Si un code trouvé sur l'AH/VISA ne correspond à AUCUN travail décrit sur
  la facture, ne le retiens pas : la nature réelle des travaux facturés prime
  toujours sur une mention déclarative isolée.
{table_block}
Réponds UNIQUEMENT en JSON valide, sans texte avant ou après, avec exactement ces clés :
{{
  "fiches": ["BAR-EN-105"],
  "secteur": "BAR",
  "coup_de_pouce": false,
  "type_engagement": "ordre_de_service",
  "sous_traitance": false,
  "date_engagement": "23/02/2024",
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
- date_engagement : date d'engagement du dossier au format JJ/MM/AAAA, telle qu'elle
  apparaît sur le document d'engagement (OS, AE, devis, BC) ou à défaut sur le VISA
  ("Date d'engagement : ..."). CRITIQUE : cette date détermine quelle VERSION de la
  fiche s'applique (les fiches CEE ont plusieurs versions successives avec des
  critères techniques différents selon la période). Mets null si aucune date
  d'engagement n'est identifiable dans les documents fournis.
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
    max_chars_per_doc: int = 6000,  # marge suffisante pour préserver 2-3 fiches via smart_truncate
    max_chars_facture: int = 12000,  # budget élargi : c'est le document le plus fiable/descriptif
    regex_hint: list = None,
) -> Dict:
    """
    Classification par IA — reconnaissance sémantique de la nature des travaux.

    Priorité donnée à la FACTURE / DGD : c'est le document quasi toujours
    présent dans un dossier CEE, et le plus descriptif des travaux réellement
    réalisés (contrairement à l'AH ou au VISA, souvent absents ou porteurs de
    mentions déclaratives peu fiables). Sur un marché normal, la facture ne
    mentionne quasiment jamais de code fiche explicite -- c'est le texte
    descriptif des prestations qu'il faut analyser sémantiquement.

    Utilise Sonnet par défaut : la tâche (repérer un panneau isolant au milieu
    d'une facture multi-lignes, distinguer les prestations) demande davantage
    de raisonnement qu'une simple extraction, Sonnet est nettement plus fiable
    que Haiku sur ce type de lecture.

    Coût : ~0.01-0.02€ par dossier (vs ~0.001€ en Haiku) — reste marginal
    face au coût de l'analyse complète (~0.05€), pour un gain de fiabilité
    important puisque c'est désormais le chemin par défaut, pas un fallback.

    Args:
        docs: dict {nom_fichier: {"text": str, "scanned": bool}}
        model: modèle à utiliser (Sonnet par défaut)
        correspondance_table: table fiche<->travaux (depuis RuleLoader), fortement
            recommandée pour éviter les hallucinations de code fiche
        max_chars_per_doc: extrait par document autre que la facture
        max_chars_facture: extrait pour la facture/DGD spécifiquement (plus
            large, car c'est la source principale de détection sémantique)
        regex_hint: codes fiche éventuellement repérés par le regex, fournis
            comme SIMPLE INDICE à corroborer -- pas une vérité à copier telle
            quelle (un VISA peut porter des mentions déclaratives fausses,
            cf. le cas connu du libellé "BAR-TH-130" imprimé par défaut).

    Returns:
        dict avec fiches, secteur, coup_de_pouce, type_engagement,
              sous_traitance, date_engagement, confiance, raisonnement
    """
    import anthropic
    from utils.extractor import smart_truncate

    # Priorité d'ordre et de budget : facture/DGD en premier avec un budget
    # élargi, car c'est le document le plus fiable pour la détection sémantique.
    FACTURE_KEYWORDS = ("facture", "dgd", "decompte", "décompte", "dgd", "situation")

    def _is_facture(name: str) -> bool:
        return any(kw in name.lower() for kw in FACTURE_KEYWORDS)

    ordered_names = sorted(docs.keys(), key=lambda n: (not _is_facture(n), n))

    extraits = []
    for name in ordered_names:
        doc = docs[name]
        text = doc.get("text", "")
        budget = max_chars_facture if _is_facture(name) else max_chars_per_doc
        extrait = smart_truncate(text, max_chars=budget).strip()
        if extrait:
            tag = " (PROBABLE PREUVE DE RÉALISATION — priorité pour la détection sémantique)" if _is_facture(name) else ""
            extraits.append(f"[{name.upper()}{tag}]\n{extrait}")

    if not extraits:
        return _fallback_classification()

    hint_block = ""
    if regex_hint:
        hint_block = (
            f"\n\nIndice (non fiable à lui seul, à corroborer) : une recherche automatique "
            f"a repéré la présence textuelle du/des code(s) suivant(s) quelque part dans les "
            f"documents : {', '.join(regex_hint)}. Vérifie s'ils correspondent réellement à des "
            f"travaux décrits (notamment sur la facture), ou s'il s'agit d'une mention "
            f"déclarative non fiable (VISA, AH) sans rapport avec les travaux effectifs."
        )

    user_prompt = (
        "Voici les extraits des documents du dossier CEE à classifier. Analyse en "
        "PRIORITÉ la nature des travaux décrits sur la facture/DGD (prestations, "
        "matériaux, équipements facturés) : c'est la source la plus fiable, bien plus "
        "que la présence ou l'absence d'un code fiche explicite ailleurs.\n\n"
        + "\n\n".join(extraits)
        + hint_block
        + "\n\nIdentifie la ou les fiche(s) BAR/BAT applicable(s) et le contexte du dossier."
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
        result.setdefault("date_engagement", None)
        result.setdefault("confiance", "moyenne")
        result.setdefault("raisonnement", "")

        # Filet de sécurité : si Sonnet n'a pas trouvé de date malgré la demande,
        # tenter une extraction regex en secours plutôt que de rester sans date.
        if not result.get("date_engagement"):
            full_text = " ".join(d.get("text", "") for d in docs.values())
            result["date_engagement"] = _extract_date_engagement_regex(full_text)

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
    (r"BAR-TH-107", "BAR-TH-107"), (r"BAR-TH-110", "BAR-TH-110"),
    (r"BAR-TH-112", "BAR-TH-112"),
    (r"BAR-TH-113", "BAR-TH-113"), (r"BAR-TH-116", "BAR-TH-116"),
    (r"BAR-TH-127", "BAR-TH-127"), (r"BAR-TH-129", "BAR-TH-129"),
    (r"BAR-TH-137", "BAR-TH-137"),
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
    """Classification par regex — ne fonctionne que si le/les code(s) fiche sont
    écrits explicitement ou qu'un mot-clé générique évident est présent. Fallback
    uniquement. Détecte TOUS les codes fiche présents (dossier multi-fiches)."""
    full_text = " ".join(d.get("text", "") for d in docs.values())
    full_text_lower = full_text.lower()

    # Collecte TOUS les codes fiche explicitement présents dans le texte
    # (dossier multi-fiches possible), pas seulement le premier trouvé.
    fiches_detected = []
    for pattern, fiche_name in FICHE_PATTERNS:
        if re.search(pattern, full_text, re.IGNORECASE) and fiche_name not in fiches_detected:
            fiches_detected.append(fiche_name)

    # Note : "BAR-TH-130" est volontairement absent de FICHE_PATTERNS ci-dessus
    # et ne peut donc jamais être auto-détecté par regex. Sur le modèle de VISA
    # utilisé, "opération standardisée BAR-TH-130" est un libellé fixe imprimé
    # par défaut à côté de la case "Construction d'un bâtiment neuf", jamais
    # une fiche réellement déclarée pour le dossier (confirmé sur plusieurs
    # dossiers réels) — règle permanente, cf. regles_autres_documents.md.
    # Seul un mode manuel explicite (--fiche BAR-TH-130) permet de l'utiliser,
    # pour le cas rare et légitime d'un dossier réellement en construction neuve.

    via_pattern_explicite = bool(fiches_detected)

    if not fiches_detected:
        for keyword, fiche_name in KEYWORD_TO_FICHE.items():
            if keyword in full_text_lower:
                fiches_detected.append(fiche_name)
                break

    secteur = "BAT" if any(
        k in full_text_lower for k in ["tertiaire", "bat-", "bâtiment non résidentiel"]
    ) else "BAR"

    date_engagement = _extract_date_engagement_regex(full_text)

    if via_pattern_explicite:
        confiance = "haute"
        raisonnement = (f"{len(fiches_detected)} code(s) fiche trouvé(s) explicitement dans "
                         f"les documents : {', '.join(fiches_detected)}.")
    elif fiches_detected:
        confiance = "moyenne"
        raisonnement = ("Fiche déduite d'un mot-clé générique (aucun code explicite trouvé) "
                         "— fiabilité moindre, vérification IA recommandée.")
    else:
        confiance = "faible"
        raisonnement = "Aucun code fiche ni mot-clé générique identifiable dans les documents."

    if not date_engagement:
        raisonnement += (" Date d'engagement non détectée par regex : le filtrage de "
                          "version de fiche par date sera dégradé (toutes les versions "
                          "seront envoyées).")

    return {
        "fiches": fiches_detected if fiches_detected else ["INCONNUE"],
        "secteur": secteur,
        "coup_de_pouce": any(k in full_text_lower for k in ["coup de pouce", "cdp", "charte"]),
        "type_engagement": _detect_engagement_type(full_text_lower),
        "sous_traitance": any(k in full_text_lower for k in ["sous-traitant", "dc4"]),
        "date_engagement": date_engagement,
        "confiance": confiance,
        "raisonnement": raisonnement,
    }


def classify_dossier(
    docs: Dict[str, dict],
    correspondance_table: str = "",
    use_correspondance_table: bool = True,
) -> Dict:
    """
    Point d'entrée principal.

    Stratégie : l'IA (Sonnet) tourne PAR DÉFAUT dès qu'une clé API est
    disponible — c'est elle qui fait la détection sémantique des travaux
    décrits, en priorité sur la FACTURE / DGD (document quasi toujours
    présent et descriptif), pas seulement sur l'AH ou le VISA (souvent
    absents, ou porteurs de mentions déclaratives non fiables — cf. le cas
    connu du libellé "BAR-TH-130" imprimé par défaut sur certains VISA).

    Le regex ne sert plus de raccourci qui court-circuite l'IA : il reste
    utile pour deux choses seulement :
      1. Fournir un indice de corroboration passé à l'IA (codes explicites
         repérés, à confirmer par elle, pas à prendre pour argent comptant) ;
      2. Servir de fallback dégradé si aucune clé API n'est disponible.
    La plupart des factures de marchés normaux ne mentionnent JAMAIS de code
    fiche explicite — seuls certains documents de bailleurs sociaux (AH,
    VISA) le font, et pas systématiquement. Se reposer sur le regex comme
    méthode principale sous-détecterait donc la majorité des dossiers réels.

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

    if not api_key:
        regex_result["raisonnement"] = (
            "ANTHROPIC_API_KEY absent — fallback regex uniquement, dégradé. "
            "La nature des travaux n'a pas pu être analysée sémantiquement "
            "sur la facture : seuls les codes fiche explicitement écrits "
            "(rares dans les factures de marchés normaux) ont pu être "
            "détectés. Résultat probablement incomplet. Envisager --fiche "
            "pour l'indiquer manuellement, ou relancer avec une clé API."
        )
        return regex_result

    table_to_use = correspondance_table if use_correspondance_table else ""
    regex_hint = regex_result["fiches"] if regex_result["fiches"] != ["INCONNUE"] else []
    result = classify_dossier_ia(
        docs, correspondance_table=table_to_use, regex_hint=regex_hint
    )
    result["_table_utilisee"] = bool(table_to_use)
    return result


_DATE_PATTERN = re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})")

def _extract_date_engagement_regex(full_text: str) -> str:
    """
    Recherche une date d'engagement par proximité textuelle avec des mots-clés
    usuels ("date d'engagement", "fait à ... le", date de signature MOA...).
    Fallback utilisé quand la classification IA n'est pas déclenchée (code fiche
    trouvé explicitement par regex) — indispensable pour que le filtrage de
    version de fiche par date fonctionne même sur le chemin rapide sans IA.
    Retourne None si rien de fiable n'est trouvé (mieux vaut aucune date qu'une
    date fausse qui sélectionnerait la mauvaise version de fiche).
    """
    keywords = [
        "date d'engagement",
        "date d engagement",
        "engagement :",
    ]
    text_lower = full_text.lower()

    for kw in keywords:
        idx = text_lower.find(kw)
        if idx == -1:
            continue
        window = full_text[idx:idx + 60]
        m = _DATE_PATTERN.search(window)
        if m:
            day, month, year = m.groups()
            if len(year) == 2:
                year = "20" + year
            try:
                day_i, month_i = int(day), int(month)
                if 1 <= day_i <= 31 and 1 <= month_i <= 12:
                    return f"{day_i:02d}/{month_i:02d}/{year}"
            except ValueError:
                continue
    return None


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
