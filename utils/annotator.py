"""
Annotation des PDF du dossier avec les éléments ayant servi à l'audit.

Principe : ZÉRO token API. L'audit retourne déjà les citations verbatim de
chaque élément technique et les valeurs clés (dates, montant, professionnel).
Ce module les RETROUVE dans les PDF et pose des surlignages par-dessus :

- PDF natifs : recherche directe dans la couche texte (page.search_for).
- PDF scannés : la couche texte est vide — on rastérise chaque page, on
  demande à Tesseract les BOÎTES de chaque mot (image_to_data), on cherche la
  séquence de mots de la citation, et on convertit les boîtes pixels en
  coordonnées PDF pour poser les surlignages au bon endroit.

Couleurs :
- vert  : élément présent et conforme
- jaune : élément présent, conformité non applicable / non tranchée
- rouge : élément présent mais NON conforme au seuil
- bleu  : valeurs clés transverses (dates, montant, professionnel, adresse)
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF

# (r, g, b) 0-1
_VERT = (0.55, 0.9, 0.55)
_JAUNE = (1.0, 0.92, 0.45)
_ROUGE = (1.0, 0.55, 0.55)
_BLEU = (0.55, 0.75, 1.0)

_OCR_DPI = 200  # rendu des pages scannées pour la localisation Tesseract


def _norm(s: str) -> str:
    """Normalisation tolérante : minuscules, accents retirés, espaces réduits,
    ponctuation neutralisée — même esprit que verify_citations."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def collect_targets(audit: dict) -> List[dict]:
    """
    Extrait de l'audit la liste des textes à surligner :
    [{"texte": ..., "label": ..., "couleur": (r,g,b)}]
    """
    targets: List[dict] = []

    for fiche in audit.get("fiches", []) or []:
        code = fiche.get("code", "?")
        for el in fiche.get("elements_techniques", []) or []:
            citation = el.get("citation_verbatim")
            if not citation or not el.get("present"):
                continue
            conforme = el.get("conforme")
            couleur = _VERT if conforme is True else (_ROUGE if conforme is False else _JAUNE)
            targets.append({
                "texte": citation,
                "label": f"[{code}] {el.get('champ', '?')}"
                          + (f" = {el['valeur_trouvee']}" if el.get("valeur_trouvee") else ""),
                "couleur": couleur,
            })

    # Valeurs clés transverses : recherchées telles quelles (courtes -> fiable)
    for key, label in [
        ("date_engagement_confirmee", "Date d'engagement retenue"),
        ("date_realisation", "Date de réalisation retenue"),
        ("montant_ht", "Montant HT (lien fort)"),
        ("adresse_travaux", "Adresse des travaux"),
    ]:
        val = audit.get(key)
        if val:
            targets.append({"texte": str(val), "label": label, "couleur": _BLEU})

    # Professionnel : surligner le SIRET s'il est extractible (plus discriminant
    # que la raison sociale, souvent répétée partout)
    pro = audit.get("professionnel_realisation") or ""
    m = re.search(r"\d[\d\s]{10,}\d", pro)
    if m:
        targets.append({"texte": m.group(0), "label": "SIRET professionnel (réalisation)",
                        "couleur": _BLEU})

    return targets


# ---------------------------------------------------------------------------
# PDF natifs
# ---------------------------------------------------------------------------

def _search_native(page: "fitz.Page", texte: str) -> List["fitz.Quad"]:
    """Recherche dans la couche texte, avec replis progressifs : texte entier,
    puis fenêtres de 8 mots (les citations longues traversent souvent des
    sauts de ligne/colonnes que search_for ne recolle pas toujours)."""
    quads = page.search_for(texte, quads=True)
    if quads:
        return quads
    mots = texte.split()
    if len(mots) <= 4:
        return []
    out: List[fitz.Quad] = []
    n = min(8, len(mots))
    step = max(1, n - 2)
    for i in range(0, len(mots) - n + 1, step):
        window = " ".join(mots[i:i + n])
        out.extend(page.search_for(window, quads=True))
    return out


# ---------------------------------------------------------------------------
# PDF scannés : localisation par boîtes OCR
# ---------------------------------------------------------------------------

def _page_ocr_words(page: "fitz.Page") -> Tuple[List[dict], float]:
    """OCR de la page avec boîtes par mot. Retourne (mots, échelle px->pt).
    Chaque mot : {"t": texte normalisé, "x0", "y0", "x1", "y1"} en pixels."""
    import pytesseract
    from PIL import Image
    import io

    pix = page.get_pixmap(dpi=_OCR_DPI)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    data = pytesseract.image_to_data(img, lang="fra", output_type=pytesseract.Output.DICT)
    words = []
    for i, w in enumerate(data["text"]):
        wn = _norm(w)
        if not wn:
            continue
        words.append({
            "t": wn,
            "x0": data["left"][i],
            "y0": data["top"][i],
            "x1": data["left"][i] + data["width"][i],
            "y1": data["top"][i] + data["height"][i],
        })
    scale = page.rect.width / pix.width  # px -> points PDF
    return words, scale


def _find_in_ocr_words(words: List[dict], texte: str) -> List[List[int]]:
    """
    Cherche `texte` dans la liste OCR de la page, en deux temps :

    1) Correspondance par MOTS (rapide, fenêtres de 6 mots consécutifs pour
       tolérer les erreurs OCR sur les citations longues, comme
       verify_citations). Fonctionne quand la page OCR découpe les mots de
       façon comparable à l'OCR d'origine (celui qui a produit la citation).

    2) Repli par CARACTÈRES si (1) échoue : sur certaines pages, l'OCR
       d'origine colle plusieurs mots sans espace (ex: un bloc de libellés
       tassés donne "Efficaciteenergetiquesaisonniere94" d'un bloc), et la
       segmentation en mots de la citation ne correspondra JAMAIS à un
       nouveau passage OCR qui segmente différemment la même image. On
       compare alors la citation et le texte de page comme deux CHAÎNES DE
       CARACTÈRES concaténées (sans espaces), via le plus long segment
       commun (difflib) — insensible à l'endroit où les espaces tombent.

    Retourne des listes d'INDICES de mots (des spans fusionnés, pas des
    copies) pour permettre un regroupement propre en rectangles par ligne.
    """
    cible = _norm(texte).split()
    if not cible:
        return []
    page_tokens = [w["t"] for w in words]

    n = min(6, len(cible)) if len(cible) > 6 else len(cible)
    fenetres = ([cible] if len(cible) <= 6 else
                [cible[i:i + n] for i in range(0, len(cible) - n + 1, max(1, n - 2))])

    raw_spans: List[Tuple[int, int]] = []
    for seq in fenetres:
        for i in range(len(page_tokens) - len(seq) + 1):
            if page_tokens[i:i + len(seq)] == seq:
                raw_spans.append((i, i + len(seq)))

    if not raw_spans:
        raw_spans = _find_by_chars(words, cible)
        if not raw_spans:
            return []

    raw_spans.sort()
    merged: List[List[int]] = []
    cur_start, cur_end = raw_spans[0]
    for s, e in raw_spans[1:]:
        if s <= cur_end + 2:
            cur_end = max(cur_end, e)
        else:
            merged.append(list(range(cur_start, cur_end)))
            cur_start, cur_end = s, e
    merged.append(list(range(cur_start, cur_end)))
    return merged


def _find_by_chars(words: List[dict], cible_mots: List[str]) -> List[Tuple[int, int]]:
    """
    Repli caractère : concatène le texte de la page (sans séparateur) avec une
    table de correspondance position-caractère -> index de mot, fait de même
    pour la citation cible, et cherche le plus long segment commun. Accepté si
    la couverture atteint au moins 60% de la longueur de la cible (tolère les
    erreurs OCR résiduelles tout en évitant les faux positifs sur du
    vocabulaire CEE générique très court).
    """
    import difflib

    page_chars: List[str] = []
    char_to_word: List[int] = []
    for wi, w in enumerate(words):
        for ch in w["t"]:
            page_chars.append(ch)
            char_to_word.append(wi)
    page_str = "".join(page_chars)
    cible_str = "".join(cible_mots)
    if len(cible_str) < 6 or not page_str:
        return []

    sm = difflib.SequenceMatcher(None, page_str, cible_str, autojunk=False)
    # Les blocs de 1-2 caractères sont du bruit statistique (n'importe quelle
    # lettre commune matche par hasard n'importe où sur la page) : ils
    # gonflaient l'enveloppe jusqu'à couvrir toute la page. Seuls les blocs
    # d'au moins 3 caractères sont significatifs et utilisés pour la
    # couverture ET l'enveloppe.
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 3]
    if not blocks:
        return []
    total_matched = sum(b.size for b in blocks)
    if total_matched < max(6, int(0.4 * len(cible_str))):
        return []
    a_start = min(b.a for b in blocks)
    a_end = max(b.a + b.size for b in blocks)
    # Rejette une enveloppe démesurément plus large que la cible (signe d'un
    # faux positif dispersé sur toute la page plutôt qu'un vrai passage local).
    if (a_end - a_start) > 3 * len(cible_str):
        return []

    wi_start = char_to_word[a_start]
    wi_end = char_to_word[min(a_end - 1, len(char_to_word) - 1)]
    return [(wi_start, wi_end + 1)]


def _merge_rects_par_ligne(rects: List["fitz.Rect"]) -> List["fitz.Rect"]:
    """
    Fusionne une liste de rectangles PDF en rectangles par ligne visuelle
    (regroupement par centre Y, tolérance = hauteur médiane des rects).

    Nécessaire même côté PDF NATIF : PyMuPDF peut retourner PLUSIEURS quads
    pour une seule occurrence d'un search_for() quand le texte trouvé
    traverse des segments de police différents dans le flux PDF (changement
    de style, espacement...) — même symptôme "confettis" que côté OCR, cause
    différente. On applique donc la même fusion aux deux chemins.
    """
    if not rects:
        return []
    hauteurs = [r.y1 - r.y0 for r in rects]
    tol = (sorted(hauteurs)[len(hauteurs) // 2] or 10) * 0.6
    rects_sorted = sorted(rects, key=lambda r: ((r.y0 + r.y1) / 2, r.x0))

    lignes: List[List["fitz.Rect"]] = []
    for r in rects_sorted:
        yc = (r.y0 + r.y1) / 2
        placed = False
        for ligne in lignes:
            yc_ligne = sum((x.y0 + x.y1) / 2 for x in ligne) / len(ligne)
            if abs(yc - yc_ligne) <= tol:
                ligne.append(r)
                placed = True
                break
        if not placed:
            lignes.append([r])

    out = []
    for ligne in lignes:
        out.append(fitz.Rect(min(r.x0 for r in ligne), min(r.y0 for r in ligne),
                              max(r.x1 for r in ligne), max(r.y1 for r in ligne)))
    return out


def _rects_from_indices(idx: List[int], words: List[dict], scale: float) -> List["fitz.Rect"]:
    """Fusionne les boîtes des mots d'un span OCR en rectangles par ligne."""
    ws = [words[i] for i in idx]
    rects_mots = [fitz.Rect(w["x0"] * scale, w["y0"] * scale,
                             w["x1"] * scale, w["y1"] * scale) for w in ws]
    return _merge_rects_par_ligne(rects_mots)


def _dedup_rects(items: List[Tuple["fitz.Rect", str, tuple]],
                  iou_seuil: float = 0.6) -> List[Tuple["fitz.Rect", str, tuple]]:
    """
    Fusionne les rectangles très proches (IoU élevé) qui viendraient d'éléments
    DIFFÉRENTS pointant vers le même passage (ex: `reference` et
    `reference__chaudiere` matchant la même ligne source) — pose UN seul
    surlignage avec les labels concaténés plutôt que plusieurs superposés
    (empilement de couleurs illisible).
    """
    out: List[Tuple[fitz.Rect, str, tuple]] = []
    for rect, label, couleur in items:
        fusionne = False
        for i, (r2, lab2, coul2) in enumerate(out):
            inter = (rect & r2).get_area()
            union = rect.get_area() + r2.get_area() - inter
            if union > 0 and inter / union >= iou_seuil:
                out[i] = (r2, lab2 + " · " + label, coul2)
                fusionne = True
                break
        if not fusionne:
            out.append((rect, label, couleur))
    return out


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def annotate_pdf(pdf_path: Path, targets: List[dict]) -> Tuple[bytes, int]:
    """
    Surligne dans un PDF tous les textes cibles trouvés.
    Retourne (bytes du PDF annoté, nombre de surlignages posés).
    """
    doc = fitz.open(str(pdf_path))
    n_annots = 0
    ocr_cache: Dict[int, Tuple[List[dict], float]] = {}

    for page in doc:
        has_text = bool(page.get_text().strip())
        a_poser: List[Tuple[fitz.Rect, str, tuple]] = []

        if has_text:
            for t in targets:
                quads = _search_native(page, t["texte"])
                rects_bruts = [q.rect if hasattr(q, "rect") else fitz.Rect(q) for q in quads]
                for rect in _merge_rects_par_ligne(rects_bruts):
                    a_poser.append((rect, t["label"], t["couleur"]))
        else:
            if page.number not in ocr_cache:
                try:
                    ocr_cache[page.number] = _page_ocr_words(page)
                except Exception:
                    ocr_cache[page.number] = ([], 1.0)
            words, scale = ocr_cache[page.number]
            if words:
                for t in targets:
                    for idx in _find_in_ocr_words(words, t["texte"]):
                        for rect in _rects_from_indices(idx, words, scale):
                            a_poser.append((rect, t["label"], t["couleur"]))

        for rect, label, couleur in _dedup_rects(a_poser):
            a = page.add_highlight_annot(rect)
            a.set_colors(stroke=couleur)
            a.set_info(content=label)
            a.update()
            n_annots += 1

    out = doc.tobytes(deflate=True, garbage=3)
    doc.close()
    return out, n_annots


def annotate_dossier(pdf_paths: List, audit: dict) -> Dict[str, dict]:
    """
    Annote tous les PDF d'un dossier avec les éléments de l'audit.

    Returns:
        {nom_fichier: {"bytes": ..., "n_annotations": int, "role": str}}
        role: 'engagement' / 'realisation' / 'autre' d'après la
        catégorisation retournée par l'audit (correspondance par inclusion de
        nom, insensible à la casse).
    """
    targets = collect_targets(audit)
    docs_eng = [_norm(x) for x in (audit.get("documents_engagement") or [])]
    docs_rea = [_norm(x) for x in (audit.get("documents_realisation") or [])]

    out: Dict[str, dict] = {}
    for p in pdf_paths:
        p = Path(p)
        try:
            data, n = annotate_pdf(p, targets)
        except Exception as e:  # un PDF illisible ne bloque pas les autres
            out[p.name] = {"bytes": None, "n_annotations": 0, "role": "autre",
                            "erreur": str(e)}
            continue
        stem_n = _norm(p.stem)
        role = "autre"
        if any(d in stem_n or stem_n in d for d in docs_eng):
            role = "engagement"
        elif any(d in stem_n or stem_n in d for d in docs_rea):
            role = "realisation"
        out[p.name] = {"bytes": data, "n_annotations": n, "role": role}
    return out
