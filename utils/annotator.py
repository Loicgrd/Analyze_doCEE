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


def _find_in_ocr_words(words: List[dict], texte: str) -> List[List[dict]]:
    """Cherche la séquence de mots normalisés de `texte` dans la liste OCR de
    la page (fenêtres de 6 mots consécutifs pour tolérer les erreurs OCR sur
    les citations longues, comme verify_citations)."""
    cible = _norm(texte).split()
    if not cible:
        return []
    page_tokens = [w["t"] for w in words]

    def _match_at(start: int, seq: List[str]) -> bool:
        return page_tokens[start:start + len(seq)] == seq

    matches: List[List[dict]] = []
    n = len(cible) if len(cible) <= 6 else 6
    fenetres = ([cible] if len(cible) <= 6 else
                [cible[i:i + n] for i in range(0, len(cible) - n + 1, max(1, n - 2))])
    for seq in fenetres:
        for i in range(len(page_tokens) - len(seq) + 1):
            if _match_at(i, seq):
                matches.append(words[i:i + len(seq)])
    return matches


def _rects_from_words(match: List[dict], scale: float) -> List["fitz.Rect"]:
    """Fusionne les boîtes d'une séquence de mots en rectangles par ligne."""
    rects: List[fitz.Rect] = []
    ligne: List[dict] = []
    for w in match:
        if ligne and abs(w["y0"] - ligne[-1]["y0"]) > (ligne[-1]["y1"] - ligne[-1]["y0"]):
            rects.append(_bbox(ligne, scale))
            ligne = []
        ligne.append(w)
    if ligne:
        rects.append(_bbox(ligne, scale))
    return rects


def _bbox(ws: List[dict], scale: float) -> "fitz.Rect":
    return fitz.Rect(min(w["x0"] for w in ws) * scale - 1,
                      min(w["y0"] for w in ws) * scale - 1,
                      max(w["x1"] for w in ws) * scale + 1,
                      max(w["y1"] for w in ws) * scale + 1)


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
        for t in targets:
            if has_text:
                for quad in _search_native(page, t["texte"]):
                    a = page.add_highlight_annot(quad)
                    a.set_colors(stroke=t["couleur"])
                    a.set_info(content=t["label"])
                    a.update()
                    n_annots += 1
            else:
                if page.number not in ocr_cache:
                    try:
                        ocr_cache[page.number] = _page_ocr_words(page)
                    except Exception:
                        ocr_cache[page.number] = ([], 1.0)
                words, scale = ocr_cache[page.number]
                if not words:
                    continue
                for match in _find_in_ocr_words(words, t["texte"]):
                    for rect in _rects_from_words(match, scale):
                        a = page.add_highlight_annot(rect)
                        a.set_colors(stroke=t["couleur"])
                        a.set_info(content=t["label"])
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
