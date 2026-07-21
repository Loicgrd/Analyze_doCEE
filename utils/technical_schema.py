"""
Génération dynamique du schéma technique atomique, par fiche et par version.

Principe : au lieu d'un schéma JSON universel figé (qui forcerait Claude à
remplir ~35 champs par fiche, dont l'écrasante majorité resteraient "null"),
ce module construit à la volée — en PYTHON, sans appel IA, coût nul — le
sous-ensemble de champs atomiques réellement pertinents pour la fiche et la
version détectées, à partir du texte libre de la colonne "MENTIONS
OBLIGATOIRES SUR LA PREUVE DE REALISATION" du référentiel Fiche_BAR.xlsx.

Couverture mesurée sur le référentiel réel (132 lignes, 427 mentions
uniques) : ~76% des mentions sont capturées par le vocabulaire atomique fixe
ci-dessous ; le reste (mentions rares ou phrases de périmètre narratives,
non réductibles à un champ nommé) part dans le champ fourre-tout
"elements_specifiques", qui garde le libellé exact de l'Excel — aucune
perte d'information, juste moins de structuration pour la longue traîne.

À enrichir progressivement : si un nouveau libellé apparaît souvent dans le
fourre-tout sur vos dossiers réels, ajoutez une règle dans MAPPING plutôt
que de laisser la couverture se dégrader silencieusement.
"""

import re
from typing import Dict, List, Optional, Tuple


# Ordre important : du plus spécifique au plus générique. Un pattern générique
# placé trop tôt "avalerait" des lignes qui auraient dû matcher un pattern
# plus précis plus bas dans la liste.
MAPPING: List[Tuple[str, str]] = [
    # --- Identification produit ---
    (r"marque et r[ée]f[ée]rence|marques et r[ée]f[ée]rences|marque, r[ée]f[ée]rence", "marque_reference"),
    (r"\bmarque\b", "marque"),
    (r"\br[ée]f[ée]rence\b", "reference"),
    (r"\btype d[e']|typologie|leur type", "type_produit"),

    # --- Dimensions ---
    (r"surface habitable", "surface_habitable_m2"),
    (r"surface d[e']isolant|surface de l[e']isolant|surface install[ée]e|surface.{0,15}capteur|"
     r"surface chauff[ée]e|surface thermique|surface de r[ée]f[ée]rence|surface des logements|"
     r"surface des menuiseries|surface isol[ée]e", "surface_m2"),
    (r"[ée]paisseur", "epaisseur_mm"),
    (r"longueur", "longueur_m"),
    (r"volume du|volume du chauffe-eau|capacit[ée] de stockage|capacit[ée] des ballons", "volume_capacite_l"),
    (r"^nombre\b|^nombre d[e']|^nombre de|le nombre d|le nmobre", "nombre_unites"),
    (r"\bquantit[ée]\b", "quantite"),

    # --- Performance thermique / énergétique ---
    (r"r[ée]sistance thermique|valeur du ΔR|facteur r\b", "resistance_thermique_r"),
    (r"\betas\b|efficacit[ée] [ée]nerg[ée]tique saisonni[èe]re|\bη[sS]\b|efficacit[ée] de la|"
     r"efficacit[ée] du|efficacit[ée] saisonni[èe]re|^efficacit[ée]", "etas_pourcent"),
    (r"\bcop\b(?!\w)", "cop"),
    (r"\bscop\b", "scop"),
    (r"seer|\beer\b|valeur ere|valeur pue|valeurs pue", "seer_eer"),
    (r"rendement pci|rendement de|gain [ée]nerg[ée]tique", "rendement_pourcent"),
    (r"classe [ée]nerg[ée]tique|classe avant et apr[èe]s|classe d[e']efficacit[ée]", "classe_energetique"),
    (r"classe du r[ée]gulateur|classe r[ée]gulat|classe et marque|classe ou marque|"
     r"^r[ée]gulateur\b|mise en place de r[ée]gulateur", "classe_regulateur"),
    (r"delta ?t|Δ ?[rR]|ΔT", "delta_t_k"),
    (r"puissance [ée]lectrique|puissance wthc|puissance sp[ée]cifique|puissance pond[ée]r[ée]e",
     "puissance_elec_wthc"),
    (r"^puissance\b|puissance nominale|puissance de la|puissance du g[ée]n[ée]rateur|"
     r"puissance des radiateurs|puissance total|puissance de sortie|puissance r[ée]cup[ée]r[ée]e|"
     r"puissance chaufferie", "puissance_kw"),
    (r"r[ée]gime de temp[ée]rature", "regime_temperature"),
    (r"coefficient uw|coefficients uw|uw et sw", "coefficients_uw_sw"),

    # --- Certifications ---
    (r"acermi", "certification_acermi"),
    (r"cstbat", "certification_cstbat"),
    (r"avis technique", "avis_technique_numero"),
    (r"\bnf\b|norme nf|certifi[ée] nf|num[ée]ro de certification", "norme_nf"),
    (r"label flamme verte|\blabel\b", "label"),

    # --- Contexte bâtiment ---
    (r"nombre de logement|nombre d[e']appartement", "nombre_logements"),
    (r"type de logement|maison individuelle|logement collectif|maison existante", "type_logement"),
    (r"installation individuelle|installation collective|en collectif|en individuel|"
     r"si individuel|si collectif", "type_installation"),

    # --- Études / audits ---
    (r"date de l[e']audit|date de l[e'] ?[ée]tude", "date_audit_etude"),
    (r"r[ée]f[ée]rence de l[e'](audit|[ée]tude)|audit [ée]nerg[ée]tique|"
     r"synth[èe]se de l[e']([ée]tude)", "reference_audit_etude"),
    (r"cep ?initial|cep ?projet|cepbat|ceptmax", "cep_kwh"),
    (r"cef ?initial|cef ?projet|cefbat|cefmax", "cef_kwh"),
    (r"bbio|ic[ée]nergie", "bbio"),

    # --- Autres champs courants ---
    (r"date de visite pr[ée]alable", "date_visite_prealable"),
    (r"nature du fluide|type de fluide|fluide caloporteur|nature de l[e']appoint|combustible",
     "type_fluide_energie"),
    (r"[ée]missions? d[e']oxydes d[e']azote|\bnox\b", "emissions_nox"),
    (r"[ée]missions? de particules", "emissions_particules"),
    (r"dur[ée]e de vie|dur[ée]e du contrat", "duree_vie_contrat"),
]

# Description humaine de chaque champ atomique, utilisée pour construire le
# schéma JSON envoyé à Claude (chaque champ a un nom stable ET une
# description qui rappelle le contexte, utile car un même champ atomique
# — ex: "puissance_kw" — sert à des grandeurs différentes selon la fiche).
FIELD_DESCRIPTIONS: Dict[str, str] = {
    "marque_reference": "Marque et référence du produit/équipement (valeur combinée)",
    "marque": "Marque du produit/équipement",
    "marque__caisson": "Marque du caisson de ventilation (VMC)",
    "marque__bouches_extraction": "Marque des bouches d'extraction (VMC)",
    "marque__entree_air": "Marque des entrées d'air (VMC)",
    "reference__caisson": "Référence commerciale exacte du caisson de ventilation (VMC), hors N° ACERMI/certification",
    "reference__bouches_extraction": "Référence commerciale exacte des bouches d'extraction (VMC), hors N° ACERMI/certification",
    "reference__entree_air": "Référence commerciale exacte des entrées d'air (VMC), hors N° ACERMI/certification",
    "type_ventilation": ("Type de VMC si marque/référence non applicables : hygroréglable A, "
                          "hygroréglable B, ou basse pression (alternative admise à la place de "
                          "marque+référence par composant, selon la formulation de la fiche)"),
    "reference": ("Référence commerciale exacte du produit/équipement (nom de gamme/modèle). "
                   "NE JAMAIS y inclure le numéro ACERMI ni aucun numéro de certification — "
                   "ils vont dans le champ distinct `certification_acermi`"),
    "type_produit": "Type ou modèle du produit/équipement",
    "surface_habitable_m2": "Surface habitable du logement (m²)",
    "surface_m2": "Surface (isolant, capteurs, menuiseries...) en m²",
    "epaisseur_mm": "Épaisseur en mm",
    "longueur_m": "Longueur (réseau, conduit...) en m",
    "volume_capacite_l": "Volume ou capacité de stockage en litres",
    "nombre_unites": "Nombre d'unités posées/installées",
    "quantite": "Quantité",
    "resistance_thermique_r": "Résistance thermique R (m².K/W)",
    "etas_pourcent": "Efficacité énergétique saisonnière Etas (%)",
    "cop": "Coefficient de performance (COP)",
    "scop": "Coefficient de performance saisonnier (SCOP)",
    "seer_eer": "SEER / EER (efficacité frigorifique)",
    "rendement_pourcent": "Rendement (%)",
    "classe_energetique": "Classe énergétique",
    "classe_regulateur": "Classe du régulateur",
    "delta_t_k": "ΔT nominal (K)",
    "puissance_elec_wthc": "Puissance électrique absorbée pondérée (WThC)",
    "puissance_kw": "Puissance (kW)",
    "regime_temperature": "Régime de température (basse/moyenne/haute)",
    "coefficients_uw_sw": "Coefficients Uw et Sw des menuiseries",
    "certification_acermi": "Numéro/référence de certification ACERMI",
    "certification_cstbat": "Numéro/référence de certification CSTBat",
    "avis_technique_numero": "Numéro et validité de l'avis technique",
    "norme_nf": "Référence de norme NF ou certification associée",
    "label": "Label (Flamme Verte...)",
    "nombre_logements": "Nombre de logements concernés",
    "type_logement": "Type de logement (maison individuelle / appartement)",
    "type_installation": "Type d'installation (individuelle / collective)",
    "date_audit_etude": "Date de l'audit ou de l'étude thermique",
    "reference_audit_etude": "Référence / conformité de l'audit ou de l'étude thermique",
    "cep_kwh": "Cep initial et Cep projet (kWh/m².an)",
    "cef_kwh": "Cef initial et Cef projet (kWh/m².an)",
    "bbio": "Bbio initial et Bbio max",
    "date_visite_prealable": "Date de la visite préalable",
    "type_fluide_energie": "Type de fluide ou d'énergie (chauffage, appoint...)",
    "emissions_nox": "Émissions d'oxydes d'azote (NOx, mg/Nm³)",
    "emissions_particules": "Émissions de particules (mg/Nm³)",
    "duree_vie_contrat": "Durée de vie ou durée du contrat",
}


def match_field(line: str) -> Optional[str]:
    """Associe une ligne de texte libre (une mention du référentiel) à un
    champ atomique fixe, ou None si aucune règle ne correspond (-> fourre-tout)."""
    line_low = line.lower()
    for pattern, field in MAPPING:
        if re.search(pattern, line_low):
            return field
    return None


# Conjonction "marque et référence" / "marque, référence" (pluriels inclus)
_MARQUE_REF_CONJ = re.compile(r"marques?\s*(?:et|,)\s*r[ée]f[ée]rences?")
# Disjonction : la présence d'un "ou" dans la ligne signale une ALTERNATIVE
# ("classe OU marque et référence", "etas OU marque et référence de la PAC",
# "Marque et références de ces éléments OU le type hygroréglable...") — la
# scinder casserait la sémantique "l'un des deux suffit".
_DISJONCTION = re.compile(r"\bou\b")

# Cas spécial VMC (BAR-TH-127 et fiches proches) : la ligne "Marque et
# références de ces éléments OU le type (...)" désigne EN RÉALITÉ trois
# composants distincts (caisson, bouches d'extraction, entrées d'air) dont
# les comptes sont listés séparément juste avant dans le référentiel. Une
# scission générique ne peut pas le deviner (la ligne elle-même ne nomme pas
# les composants), donc on la détecte par signature : "marque et référence(s)
# de ces éléments" + présence de "caisson" ET "bouche" ET "entrée" quelque
# part dans le bloc de mentions. Sans ce cas spécial, la valeur "marque +
# référence" des 3 composants finit compressée dans un seul champ générique
# `marque_reference` -- impossible de savoir lequel des 3 composants manque.
_VMC_ELEMENTS_CONJ = re.compile(r"marques?\s*et\s*r[ée]f[ée]rences?\s+de\s+ces\s+[ée]l[ée]ments")
_VMC_COMPOSANTS = [
    ("caisson", "caisson"),
    ("bouche", "bouches_extraction"),
    ("entr[ée]e", "entree_air"),
]


def _norm_texte_complet(bloc: str) -> str:
    """Normalisation légère (minuscule + accents conservés pour les classes
    de caractères [ée] du pattern) pour la détection de contexte VMC sur
    l'ensemble du bloc de mentions, pas ligne par ligne."""
    return bloc.lower()


def _est_contexte_vmc(bloc_complet_normalise: str) -> bool:
    return all(re.search(pat, bloc_complet_normalise) for pat, _ in _VMC_COMPOSANTS)


def match_fields_multi(line: str) -> List[str]:
    """
    Comme match_field, mais peut retourner PLUSIEURS champs atomiques pour une
    même mention. Cas traité : « Marque et référence de X » est scindé en deux
    champs distincts `marque` + `reference` — sur le terrain, il est fréquent
    qu'un seul des deux figure sur la facture, et un point unique
    `marque_reference` ne permettait pas de savoir LEQUEL manque.

    Règles :
    - Ligne avec disjonction (« ou ») -> comportement historique inchangé
      (un seul champ, ex: marque_reference) : c'est une alternative, pas une
      double exigence.
    - Conjonction pure « marque et/,' référence [de X] » -> ['marque',
      'reference'], plus tout autre champ conjoint détecté sur le reste de la
      ligne (ex: « Marque, référence et épaisseur de l'isolant » ->
      ['marque', 'reference', 'epaisseur_mm'] — l'épaisseur était auparavant
      avalée par le match unique marque_reference).
    - Sinon -> [match_field(line)] (ou [] si aucun match).
    """
    line_low = line.lower()
    m = _MARQUE_REF_CONJ.search(line_low)
    if m and not _DISJONCTION.search(line_low):
        fields = ["marque", "reference"]
        # Chercher d'éventuels attributs conjoints sur le reste de la ligne
        residual = (line_low[:m.start()] + " " + line_low[m.end():]).strip()
        extra = match_field(residual) if len(residual) > 3 else None
        if extra and extra not in fields:
            fields.append(extra)
        return fields
    single = match_field(line)
    return [single] if single else []


def build_schema_for_fiche(mentions_obligatoires: str) -> Dict[str, dict]:
    """
    Construit le schéma JSON (format "properties" JSON Schema) des champs
    atomiques réellement pertinents pour une fiche, à partir du texte de sa
    colonne "MENTIONS OBLIGATOIRES SUR LA PREUVE DE REALISATION".

    Args:
        mentions_obligatoires: texte brut de la cellule Excel (une mention
            par ligne, éventuellement préfixée par "▪").

    Returns:
        dict {nom_champ: {"type": [...], "description": str}} — à injecter
        dans le schéma "properties" d'un objet de fiche pour l'appel API en
        tool use. Contient toujours "elements_specifiques" en plus des
        champs atomiques trouvés, pour couvrir la longue traîne.
    """
    if not mentions_obligatoires:
        return _base_schema()

    schema = _base_schema()
    for line in mentions_obligatoires.split("\n"):
        line = line.strip().lstrip("▪").strip()
        if not line or len(line) < 3:
            continue
        field = match_field(line)
        if field and field not in schema:
            schema[field] = {
                "type": ["string", "null"],
                "description": FIELD_DESCRIPTIONS.get(field, field),
            }
    return schema


def _base_schema() -> Dict[str, dict]:
    """Le fourre-tout est toujours présent, quelle que soit la fiche."""
    return {
        "elements_specifiques": {
            "type": "array",
            "description": (
                "Éléments techniques exigés par cette fiche qui ne rentrent "
                "dans aucun champ standard ci-dessus (mentions rares ou "
                "propres à cette fiche). Un objet par élément, avec son "
                "libellé exact tel qu'il apparaît dans le référentiel."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "libelle": {"type": "string", "description": "Libellé exact de l'exigence"},
                    "present": {"type": "boolean"},
                    "valeur_trouvee": {"type": ["string", "null"]},
                    "source": {"type": ["string", "null"], "description": "Document et emplacement où l'élément a été trouvé"},
                },
                "required": ["libelle", "present"],
            },
        }
    }


def coverage_stats(all_mentions: List[str]) -> Dict[str, float]:
    """Utilitaire de diagnostic : mesure la couverture du mapping sur un
    corpus de lignes (utile pour vérifier l'impact d'un ajout de règle)."""
    unique = set(l.strip() for l in all_mentions if l.strip())
    matched = sum(1 for l in unique if match_field(l))
    total = len(unique) or 1
    return {
        "total_lignes_uniques": total,
        "matchees": matched,
        "taux_couverture_pct": round(matched / total * 100, 1),
        "champs_distincts_utilises": len(set(match_field(l) for l in unique if match_field(l))),
    }


# Mots vides à ignorer lors de la génération d'un suffixe de désambiguïsation
_STOPWORDS_SUFFIXE = {
    "de", "des", "du", "la", "le", "les", "l", "d", "et", "ou", "un", "une",
    "avec", "leur", "cas", "si", "dans", "pour", "sur", "en", "à", "au",
}


def _suffixe_depuis_ligne(line: str, max_mots: int = 2) -> str:
    """Dérive un court suffixe lisible (snake_case ASCII) depuis une ligne source,
    pour désambiguïser deux occurrences d'un même champ atomique au sein
    d'une fiche (ex: "nombre_unites__caissons" vs "nombre_unites__bouches_extraction").

    Priorité à l'objet qualifié après "de/des/du/de la/de l'" (construction
    typique en français : "marque et référence DE LA PAC" -> "pac" est le
    mot réellement distinctif, pas "marque" ni "référence" qui sont déjà
    dans le nom du champ atomique lui-même et n'apportent aucune distinction).
    """
    import re as _re
    import unicodedata as _ud

    def _ascii(s: str) -> str:
        # 'régulateurs' -> 'regulateurs' : les noms de champs restent en ASCII
        return _ud.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")

    # Mots déjà présents dans le vocabulaire des noms de champs eux-mêmes
    # (marque, référence, classe, nombre...) : jamais distinctifs en tant
    # que suffixe puisqu'ils désignent LE TYPE d'info, pas LE COMPOSANT.
    mots_generiques = {"marque", "reference", "référence", "classe", "nombre",
                        "etas", "type", "surface", "puissance", "quantite",
                        "quantité", "valeur"}

    line_low = line.lower()
    # Cherche "de la X", "des X", "du X", "de l'X", "de X" -> capture X
    m = _re.search(r"\bde\s+la\s+(\w+)|\bdes\s+(\w+)|\bdu\s+(\w+)|\bde\s+l['’](\w+)|\bde\s+(\w+)",
                    line_low)
    if m:
        candidat = next(g for g in m.groups() if g)
        if candidat not in mots_generiques and len(candidat) > 2:
            return _ascii(candidat)

    # Repli : mots non génériques/non vides de la ligne, dans l'ordre
    words = _re.findall(r"[a-zàâäéèêëïîôöùûüç]+", line_low)
    words = [w for w in words if w not in _STOPWORDS_SUFFIXE and w not in mots_generiques and len(w) > 2]
    return _ascii("_".join(words[:max_mots])) or "autre"


def build_fields_checklist_text(mentions_obligatoires: str, severite: str = "obligatoire") -> str:
    """
    Construit un texte de checklist (pour injection dans le prompt, pas un
    schéma JSON) listant les champs atomiques attendus pour cette fiche,
    avec leur nom technique exact à utiliser dans la réponse structurée.

    Le schéma JSON de l'outil d'audit reste FIXE et universel (un objet
    générique {champ, valeur_trouvee, present, conforme, source} par
    élément) — c'est ce texte de checklist, injecté dans le bloc de règles
    spécifique à la fiche, qui indique à Claude QUELS noms de champs utiliser
    pour CETTE fiche précise. Ça évite de faire varier la forme du schéma
    JSON Schema à chaque appel (plus simple et plus robuste côté API).

    Désambiguïsation : certaines fiches exigent le MÊME type d'information
    pour plusieurs composants distincts (ex: "marque et référence de la PAC"
    ET "marque et référence des régulateurs" -> toutes deux mappées sur
    l'atomique "marque_reference"). Sans distinction, Claude ne saurait pas
    à quel composant rattacher la valeur trouvée. Dans ce cas, le nom de
    champ est automatiquement suffixé avec un extrait du texte source
    (ex: "marque_reference__pac", "marque_reference__regulateurs") pour
    garder chaque occurrence identifiable, tout en restant dérivé du même
    vocabulaire atomique (donc toujours regroupable/filtrable côté Python).

    Args:
        mentions_obligatoires: texte de la colonne (obligatoire OU nécessaire
            selon `severite`, malgré le nom du paramètre conservé pour compat).
        severite: "obligatoire" (absence = élément manquant sur un point
            requis de la preuve de réalisation) ou "necessaire" (absence
            n'invalide PAS le document en tant que preuve de réalisation,
            mais l'information reste requise pour que le DOSSIER dans son
            ensemble soit jugé conforme -- distinction demandée explicitement :
            "non obligatoire sur la preuve" ne veut jamais dire "à ignorer").
    """
    if not mentions_obligatoires:
        return ""

    # 1er passage : regrouper les lignes par champ atomique détecté.
    # match_fields_multi peut retourner PLUSIEURS champs pour une même ligne
    # (« Marque et référence de X » -> marque + reference, séparés pour savoir
    # lequel des deux manque quand un seul figure sur la facture).
    by_field: Dict[str, List[str]] = {}
    unmatched = []
    split_marque_ref = False
    split_vmc = False
    bloc_norm = _norm_texte_complet(mentions_obligatoires)
    contexte_vmc = _est_contexte_vmc(bloc_norm)

    for line in mentions_obligatoires.split("\n"):
        line = line.strip().lstrip("▪").strip()
        if not line or len(line) < 3:
            continue

        if contexte_vmc and _VMC_ELEMENTS_CONJ.search(line.lower()):
            # Cas spécial VMC : 1 ligne -> 6 champs (marque+reference des 3
            # composants). La ligne mentionne aussi une alternative "type"
            # (hygroréglable A/B ou basse pression), conservée comme 7e champ.
            split_vmc = True
            for _, suffixe in _VMC_COMPOSANTS:
                by_field.setdefault(f"marque__{suffixe}", []).append(line)
                by_field.setdefault(f"reference__{suffixe}", []).append(line)
            by_field.setdefault("type_ventilation", []).append(line)
            continue

        fields = match_fields_multi(line)
        if fields:
            if "marque" in fields and "reference" in fields:
                split_marque_ref = True
            for field in fields:
                by_field.setdefault(field, []).append(line)
        else:
            unmatched.append(line)

    if not by_field and not unmatched:
        return ""

    if severite == "obligatoire":
        titre = ("**Champs atomiques OBLIGATOIRES à remplir dans `elements_techniques` pour "
                  "cette fiche** (utiliser EXACTEMENT ces noms dans le champ \"champ\"). "
                  "Absence = élément manquant sur la preuve de réalisation, potentiellement "
                  "bloquant selon la règle générale :")
    else:
        titre = ("**Champs NÉCESSAIRES (mais non obligatoires sur la preuve de réalisation "
                  "elle-même) à remplir dans `elements_techniques` pour cette fiche** — leur "
                  "absence NE rend PAS la preuve de réalisation non conforme en tant que "
                  "document, MAIS l'information reste requise pour juger le DOSSIER "
                  "pleinement conforme : chercher ces éléments partout dans le dossier "
                  "(facture, AH, annexe technique...) avant de les déclarer absents. Si "
                  "réellement introuvable nulle part, mettre \"present\": false et signaler "
                  "ce manque en anomalie ou en INCOMPLET sur le verdict technique de la "
                  "fiche -- ne jamais l'ignorer silencieusement au prétexte que la colonne "
                  "n'est pas \"obligatoire\" :")

    lines_out = [titre]

    for field, source_lines in by_field.items():
        desc = FIELD_DESCRIPTIONS.get(field, field)
        if len(source_lines) == 1:
            lines_out.append(f"- `{field}` : {desc} (texte source : « {source_lines[0]} »)")
        else:
            # Collision : plusieurs composants distincts partagent ce champ
            # atomique -> un nom désambiguïsé par occurrence, à utiliser TEL
            # QUEL (ne pas revenir au nom générique, la distinction est requise).
            lines_out.append(f"- Attention, {len(source_lines)} éléments distincts de type "
                              f"« {desc} » sont exigés sur cette fiche — utiliser un nom de "
                              f"champ DIFFÉRENT et EXPLICITE pour chacun (ne pas les fusionner) :")
            used_suffixes = set()
            for sl in source_lines:
                suffixe = _suffixe_depuis_ligne(sl)
                base_suffixe = suffixe
                i = 2
                while suffixe in used_suffixes:
                    suffixe = f"{base_suffixe}_{i}"
                    i += 1
                used_suffixes.add(suffixe)
                lines_out.append(f"  - `{field}__{suffixe}` : (texte source : « {sl} »)")

    if unmatched:
        lines_out.append("\nAutres exigences de cette fiche, sans nom de champ standard "
                          "(à mettre dans `elements_techniques` avec ce libellé exact comme `champ`) :")
        for u in unmatched:
            lines_out.append(f"- {u}")

    if split_marque_ref:
        lines_out.append(
            "\nNB : les mentions « marque et référence » de cette fiche sont volontairement "
            "SCINDÉES en deux champs distincts (`marque` / `reference`) : évalue la présence "
            "de CHACUN séparément — il est fréquent qu'un seul des deux figure sur la facture, "
            "et le vérificateur doit savoir précisément LEQUEL manque. Ne fusionne pas les "
            "deux dans un seul élément, et ne déduis jamais l'un de la présence de l'autre."
        )

    if split_vmc:
        lines_out.append(
            "\nNB : le système de ventilation comporte 3 composants distincts (caisson, "
            "bouches d'extraction, entrées d'air) : `marque__caisson`/`reference__caisson`, "
            "`marque__bouches_extraction`/`reference__bouches_extraction`, "
            "`marque__entree_air`/`reference__entree_air` sont volontairement SCINDÉS -- "
            "évalue chaque composant séparément (la facture liste souvent une marque/référence "
            "différente par composant). La fiche accepte une ALTERNATIVE : si le document "
            "précise seulement le type de VMC (hygroréglable A, hygroréglable B, ou basse "
            "pression) sans marque/référence détaillée par composant, remplis `type_ventilation` "
            "à la place -- dans ce cas les 6 champs marque/référence peuvent être absents sans "
            "que ce soit une non-conformité (l'un OU l'autre suffit, pas les deux).\n"
            "NB (surface habitable) : la condition d'éligibilité de cette fiche dépend de la "
            "surface habitable UNIQUEMENT pour une installation INDIVIDUELLE (logement par "
            "logement, seuil en WThC absolu) -- PAS pour une installation COLLECTIVE (seuil "
            "en WThC/(m3/h), indépendant de la surface). Cherche donc `surface_habitable_m2` "
            "et vérifie son seuil SEULEMENT si le document indique une pose individuelle ; si "
            "l'installation est collective, ne demande pas ce champ et n'en fais pas une "
            "anomalie d'absence."
        )

    return "\n".join(lines_out)
