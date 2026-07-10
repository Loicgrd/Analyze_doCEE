"""
Extraction de texte depuis les PDFs (avec détection scan + OCR fallback intelligent).
"""

import subprocess
import zipfile
import glob
import tempfile
import os
from pathlib import Path
from typing import List


def extract_zip(zip_path: Path, dest_dir: str) -> List[Path]:
    """Extrait un ZIP et retourne la liste des PDFs extraits."""
    extracted = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.lower().endswith(".pdf"):
                target = Path(dest_dir) / Path(name).name
                target.write_bytes(zf.read(name))
                extracted.append(target)
    return extracted


def is_scanned_pdf(pdf_path: Path) -> bool:
    """
    Détecte si un PDF est scanné (pas de couche texte extractible).
    Utilise pdffonts : si aucune police listée → scanné.
    """
    try:
        result = subprocess.run(
            ["pdffonts", str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = result.stdout.strip().split("\n")
        font_lines = [l for l in lines[2:] if l.strip()]
        return len(font_lines) == 0
    except Exception:
        return False


def get_page_count(pdf_path: Path) -> int:
    """Retourne le nombre de pages d'un PDF."""
    try:
        result = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.split("\n"):
            if line.startswith("Pages"):
                return int(line.split()[-1])
    except Exception:
        pass
    return 1


import re as _re

_FICHE_SECTION_PATTERN = _re.compile(
    r"BA[RT]-(?:EN|TH|SE)-\d+", _re.IGNORECASE
)


def smart_truncate(text: str, max_chars: int = 15000) -> str:
    """
    Troncature "intelligente" d'un texte de document CEE, qui préserve
    chaque section de fiche détectée (utile pour les documents multi-fiches
    comme une AH à plusieurs parties A, où une troncature naïve tête+queue
    peut effacer entièrement une fiche située au milieu d'un document long).

    Utilisée à la fois lors de l'extraction (extract_text_from_pdf) et lors
    de l'assemblage final du prompt (claude_client._build_docs_section), pour
    qu'aucune des deux étapes ne réintroduise le bug par une troncature
    naïve appliquée après coup.
    """
    if len(text) <= max_chars:
        return text

    fiche_positions = [m.start() for m in _FICHE_SECTION_PATTERN.finditer(text)]

    # Un seul code fiche (ou aucun) trouvé -> troncature simple tête+queue,
    # comportement historique, suffisant pour un document mono-fiche.
    if len(fiche_positions) <= 1:
        half = max_chars // 2
        return text[:half] + "\n\n[...]\n\n" + text[-half:]

    # Plusieurs codes fiche détectés, potentiellement espacés dans le
    # document (AH multi-fiches) -> on garde une fenêtre de contexte
    # autour de CHAQUE occurrence, plutôt que de risquer d'en effacer une.
    window = 1200  # caractères de contexte avant/après chaque occurrence
    ranges = []
    for pos in fiche_positions:
        start = max(0, pos - 200)
        end = min(len(text), pos + window)
        ranges.append((start, end))

    # Toujours garder le tout début (identité, numéro dossier) et la
    # toute fin (signatures) du document, en plus des fenêtres par fiche.
    ranges.append((0, min(800, len(text))))
    ranges.append((max(0, len(text) - 800), len(text)))

    # Fusionner les plages qui se chevauchent ou se touchent
    ranges.sort()
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 100:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    blocks = [text[s:e] for s, e in merged]
    assembled = "\n\n[...]\n\n".join(blocks)

    # Filet de sécurité : si l'assemblage dépasse encore largement la
    # limite (cas extrême, beaucoup de fiches très espacées), retomber
    # sur une troncature tête+queue globale plutôt que d'exploser les tokens.
    if len(assembled) > max_chars * 2:
        half = max_chars // 2
        return text[:half] + "\n\n[...]\n\n" + text[-half:]

    return assembled


def extract_text_from_pdf(pdf_path: Path, max_chars: int = 15000) -> str:
    """
    Extrait le texte d'un PDF avec pdftotext.
    Limite à max_chars pour maîtriser les tokens, via smart_truncate().
    """
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        text = result.stdout.strip()
        return smart_truncate(text, max_chars)
    except Exception as e:
        return f"[Erreur extraction texte: {e}]"


def _ocr_single_page(pdf_path: Path, page: int) -> str:
    """OCR d'une seule page via pdftoppm + pytesseract."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return "[OCR non disponible: pip install pytesseract pillow]"

    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = os.path.join(tmpdir, "page")
        subprocess.run(
            [
                "pdftoppm", "-jpeg", "-r", "200",
                "-f", str(page), "-l", str(page),
                str(pdf_path), prefix,
            ],
            capture_output=True,
            timeout=30,
        )
        imgs = glob.glob(f"{prefix}*.jpg")
        if not imgs:
            return ""
        img = Image.open(imgs[0])
        return pytesseract.image_to_string(img, lang="fra").strip()


def ocr_pdf_page(pdf_path: Path, page: int = 1) -> str:
    """OCR d'une seule page — conservé pour compatibilité, préférer ocr_pdf_smart()."""
    return _ocr_single_page(pdf_path, page)


def ocr_pdf_smart(
    pdf_path: Path,
    max_pages_ocr: int = 6,
    max_chars: int = 8000,
) -> str:
    """
    OCR intelligent multi-pages pour documents scannés.

    Stratégie : les informations utiles (fiche, montants, signatures) se
    trouvent typiquement en début et fin de document (en-tête, tableau
    récapitulatif, page de signature/totaux). On OCRise donc en priorité :
      - Les premières pages (identification, objet, détail travaux)
      - Les dernières pages (totaux, signatures, mentions finales)
    Pour un document court (<= max_pages_ocr pages), tout est OCRisé.
    Le nombre de pages OCRisées est plafonné pour maîtriser le temps de
    traitement local (le coût en tokens Claude est ensuite maîtrisé via
    max_chars sur le texte concaténé).

    Args:
        pdf_path: chemin du PDF scanné
        max_pages_ocr: nombre max de pages à OCRiser (défaut 6 : ~3 début + 3 fin)
        max_chars: troncature du texte final pour maîtriser les tokens

    Returns:
        Texte OCR concaténé, avec repères de page.
    """
    text, _meta = ocr_pdf_smart_meta(pdf_path, max_pages_ocr=max_pages_ocr, max_chars=max_chars)
    return text


def ocr_pdf_smart_meta(
    pdf_path: Path,
    max_pages_ocr: int = 6,
    max_chars: int = 8000,
) -> tuple:
    """
    Comme ocr_pdf_smart(), mais retourne aussi les MÉTADONNÉES DE COUVERTURE :
    quelles pages ont été OCRisées, combien ont été sautées, si le texte a été
    tronqué. Ces métadonnées sont injectées dans le prompt d'audit pour que
    Claude sache distinguer "absent du document" et "absent de l'extrait
    fourni" — sans elles, un élément situé sur une page intermédiaire non
    OCRisée serait déclaré manquant à tort (faux verdict INCOMPLET/NON VALIDE).

    Returns:
        (texte, meta) avec meta = {
            "truncated": bool,        # couverture partielle (pages sautées OU texte tronqué)
            "pages_total": int,
            "pages_ocr": [int],
            "pages_sautees": int,
            "couverture": str|None,   # phrase prête à injecter dans le prompt
        }
    """
    total_pages = get_page_count(pdf_path)
    half = max(1, max_pages_ocr // 2)

    if total_pages <= max_pages_ocr:
        pages_to_ocr = list(range(1, total_pages + 1))
    else:
        first_pages = list(range(1, half + 1))
        last_pages = list(range(total_pages - half + 1, total_pages + 1))
        pages_to_ocr = sorted(set(first_pages + last_pages))

    parts = []
    for p in pages_to_ocr:
        text = _ocr_single_page(pdf_path, p)
        if text:
            parts.append(f"[page {p}/{total_pages}]\n{text}")

    skipped = total_pages - len(pages_to_ocr)
    if skipped > 0 and len(parts) > half:
        parts.insert(half, f"[... {skipped} page(s) intermédiaire(s) non OCRisée(s) ...]")

    full_text = "\n\n".join(parts)
    text_truncated = len(full_text) > max_chars
    if text_truncated:
        half_c = max_chars // 2
        full_text = full_text[:half_c] + "\n\n[...]\n\n" + full_text[-half_c:]

    notes = []
    if skipped > 0:
        notes.append(
            f"OCR partiel : pages {pages_to_ocr[0]}-{pages_to_ocr[half - 1]} et "
            f"{pages_to_ocr[half]}-{pages_to_ocr[-1]} sur {total_pages} "
            f"({skipped} page(s) intermédiaire(s) NON lues)"
        )
    if text_truncated:
        notes.append("texte OCR tronqué, coupures marquées [...]")

    meta = {
        "truncated": skipped > 0 or text_truncated,
        "pages_total": total_pages,
        "pages_ocr": pages_to_ocr,
        "pages_sautees": skipped,
        "couverture": " ; ".join(notes) if notes else None,
    }
    return full_text, meta


def extract_document(pdf_path: Path, max_chars_text: int = 60000,
                     max_pages_ocr: int = 6, max_chars_ocr: int = 8000) -> dict:
    """
    Point d'entrée unique d'extraction d'un PDF (texte natif OU scanné+OCR),
    qui retourne le texte ET les métadonnées de couverture. À utiliser à la
    place du couple is_scanned_pdf()/extract_text_from_pdf()/ocr_pdf_smart()
    pour que le prompt d'audit connaisse la couverture réelle de chaque
    document (voir claude_client._build_docs_section).

    max_chars_text (60 000 ≈ 17k tokens) est un simple garde-fou contre un
    PDF natif aberrant : pour les éléments techniques, la préservation des
    annexes de facture prime largement sur l'économie de tokens (~3 €/million
    de tokens input). C'est le budget GLOBAL du dossier, appliqué dans
    claude_client._build_docs_section, qui arbitre en cas de dossier
    réellement volumineux — en priorisant les preuves de réalisation.
    """
    pdf_path = Path(pdf_path)
    pages_total = get_page_count(pdf_path)

    if is_scanned_pdf(pdf_path):
        # Exception ciblée : une PREUVE DE RÉALISATION scannée (facture, DGD,
        # décompte...) porte les éléments techniques, souvent dans des annexes
        # en pages intermédiaires. On élargit son OCR (toutes pages jusqu'à un
        # plafond plus haut) plutôt que le schéma générique 3 début + 3 fin —
        # le surcoût est du temps de traitement local (~3s/page), pas des
        # tokens, et il évite des verdicts INCOMPLET 'hors extrait' évitables.
        _preuve_kw = ("facture", "dgd", "decompte", "décompte", "situation", "solde")
        if any(kw in pdf_path.name.lower() for kw in _preuve_kw):
            max_pages_ocr = max(max_pages_ocr, 14)
            max_chars_ocr = max(max_chars_ocr, 30000)
        text, meta = ocr_pdf_smart_meta(pdf_path, max_pages_ocr=max_pages_ocr,
                                        max_chars=max_chars_ocr)
        return {"text": text, "scanned": True, "path": str(pdf_path),
                "pages_total": pages_total, **meta}

    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        full = result.stdout.strip()
    except Exception as e:
        return {"text": f"[Erreur extraction texte: {e}]", "scanned": False,
                "path": str(pdf_path), "pages_total": pages_total,
                "truncated": False, "orig_chars": 0, "couverture": None}

    text = smart_truncate(full, max_chars_text)
    truncated = len(text) < len(full)
    couverture = None
    if truncated:
        pct = min(99, round(100 * len(text) / max(1, len(full))))
        couverture = (f"texte tronqué : ~{pct}% des {len(full):,} caractères du "
                      f"document ({pages_total} page(s)) sont fournis, "
                      f"coupures marquées [...]")
    return {"text": text, "scanned": False, "path": str(pdf_path),
            "pages_total": pages_total, "truncated": truncated,
            "orig_chars": len(full), "couverture": couverture}
