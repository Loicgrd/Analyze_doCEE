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


def extract_text_from_pdf(pdf_path: Path, max_chars: int = 6000) -> str:
    """
    Extrait le texte d'un PDF avec pdftotext.
    Limite à max_chars pour maîtriser les tokens.
    """
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        text = result.stdout.strip()
        if len(text) > max_chars:
            half = max_chars // 2
            text = text[:half] + "\n\n[...]\n\n" + text[-half:]
        return text
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
    if len(full_text) > max_chars:
        half_c = max_chars // 2
        full_text = full_text[:half_c] + "\n\n[...]\n\n" + full_text[-half_c:]

    return full_text
