"""
Extraction de texte depuis les PDFs (avec détection scan + OCR fallback).
"""

import subprocess
import zipfile
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
        # Les 2 premières lignes sont l'entête de pdffonts
        font_lines = [l for l in lines[2:] if l.strip()]
        return len(font_lines) == 0
    except Exception:
        return False


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
            # On garde le début (engagement/entête) et la fin (montants/signature)
            half = max_chars // 2
            text = text[:half] + "\n\n[...]\n\n" + text[-half:]
        return text
    except Exception as e:
        return f"[Erreur extraction texte: {e}]"


def ocr_pdf_page(pdf_path: Path, page: int = 1) -> str:
    """
    OCR d'une page d'un PDF scanné via pdftoppm + pytesseract.
    Retourne le texte extrait.
    """
    import tempfile
    import os

    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return "[OCR non disponible: pip install pytesseract pillow]"

    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = os.path.join(tmpdir, "page")
        subprocess.run(
            [
                "pdftoppm",
                "-jpeg",
                "-r", "200",
                "-f", str(page),
                "-l", str(page),
                str(pdf_path),
                prefix,
            ],
            capture_output=True,
            timeout=30,
        )
        # Trouver le fichier généré
        import glob
        imgs = glob.glob(f"{prefix}*.jpg")
        if not imgs:
            return "[Aucune image générée par pdftoppm]"

        img = Image.open(imgs[0])
        text = pytesseract.image_to_string(img, lang="fra")
        return text.strip()
