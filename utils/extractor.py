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
    Détecte si un PDF doit être traité comme SCANNÉ (préférer une OCR fraîche
    à la couche texte native). Deux signaux, combinés :

    1. pdffonts : aucune police listée -> scanné à coup sûr (comportement
       historique).
    2. Page dominée par une image plein format (couvre la quasi-totalité de
       la page) : c'est le signe d'un scan/photocopie, MÊME si une police est
       listée. De nombreux copieurs/multifonctions embarquent leur propre
       couche OCR invisible (souvent une police générique non-embarquée type
       Helvetica/Arial) par-dessus l'image scannée -- cette couche est
       généralement d'une qualité BIEN INFÉRIEURE à un passage Tesseract
       frais à 200dpi (ex. observé : "Çlse en place d'un doublage )solanl
       sur mui(s)" contre "Mise en place d'un doublage Isolant sur mur(s)"
       pour Tesseract sur la même page). Sans ce second signal, ces PDF
       étaient classés "natifs" et leur mauvaise couche texte utilisée telle
       quelle -- d'où des citations et une extraction très dégradées malgré
       un pdffonts non vide.
    """
    try:
        result = subprocess.run(
            ["pdffonts", str(pdf_path)],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().split("\n")
        font_lines = [l for l in lines[2:] if l.strip()]
        if len(font_lines) == 0:
            return True
    except Exception:
        return False

    return _page_dominee_par_image(pdf_path)


def _page_dominee_par_image(pdf_path: Path, seuil_couverture: float = 0.85) -> bool:
    """Vrai si la première page contient une image couvrant au moins
    `seuil_couverture` de la surface de la page (signature d'un scan)."""
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        if doc.page_count == 0:
            return False
        page = doc[0]
        page_area = page.rect.width * page.rect.height
        couverte = 0.0
        for img in page.get_images(full=True):
            for rect in page.get_image_rects(img[0]):
                couverte += rect.width * rect.height
        doc.close()
        return page_area > 0 and (couverte / page_area) >= seuil_couverture
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


def _ocr_single_page(pdf_path: Path, page: int, dpi: int = 300) -> str:
    """
    OCR d'une seule page via pdftoppm + pytesseract.

    dpi=300 (au lieu de 200 historiquement) : les tableaux de chiffrage denses
    (quantités, dimensions, prix unitaires en colonnes serrées) perdent des
    valeurs à 200dpi -- cas réel observé (T233337) où la quantité et les
    dimensions d'un poste de menuiseries ("24 U", "Dimensions : 3800x2200")
    étaient absentes à 200dpi mais lisibles à 300dpi sans aucun autre
    changement. Le surcoût est purement local (temps de traitement, pas de
    tokens Claude) -- cohérent avec le principe déjà appliqué ailleurs dans
    l'app (préférer la fiabilité au token près qui coûte quasi rien).
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return "[OCR non disponible: pip install pytesseract pillow]"

    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = os.path.join(tmpdir, "page")
        subprocess.run(
            [
                "pdftoppm", "-jpeg", "-r", str(dpi),
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
    dpi: int = 300,
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
        text = _ocr_single_page(pdf_path, p, dpi=dpi)
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


def _extract_native_text_only(pdf_path: Path) -> str:
    """Texte de la couche native (pdftotext), sans jugement sur sa qualité --
    utilisé uniquement pour repêcher un éventuel résidu (ajout numérique en
    petite police) absent de l'OCR d'un document par ailleurs traité comme
    scanné. Jamais utilisé seul comme source principale ici."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _normalise_pour_comparaison(s: str) -> str:
    """Normalisation grossière (minuscule, espaces réduits) pour la
    comparaison natif/OCR."""
    return " ".join(s.lower().split())


def _couverture_floue(ligne: str, ocr_norm: str) -> float:
    """
    Proportion (0-1) des caractères de `ligne` (normalisée, espaces retirés)
    qui se retrouvent dans `ocr_norm` via les plus longs segments communs
    (difflib), tolérant à la déformation OCR -- voir docstring d'appel dans
    extract_document pour le raisonnement complet.
    """
    import difflib
    import re as _re

    l = _re.sub(r"[^a-z0-9]+", "", ligne.lower())
    if len(l) < 4:
        return 1.0  # trop court pour juger sans risque -> considéré couvert
    ocr_c = _re.sub(r"[^a-z0-9]+", "", ocr_norm)
    sm = difflib.SequenceMatcher(None, ocr_c, l, autojunk=False)
    total = sum(b.size for b in sm.get_matching_blocks() if b.size >= 3)
    return total / len(l)


def extract_document(pdf_path: Path, max_chars_text: int = 200000,
                     max_pages_ocr: int = 6, max_chars_ocr: int = 8000) -> dict:
    """
    Point d'entrée unique d'extraction d'un PDF (texte natif OU scanné+OCR),
    qui retourne le texte ET les métadonnées de couverture. À utiliser à la
    place du couple is_scanned_pdf()/extract_text_from_pdf()/ocr_pdf_smart()
    pour que le prompt d'audit connaisse la couverture réelle de chaque
    document (voir claude_client._build_docs_section).

    max_chars_text (200 000 ≈ 55k tokens) est un pur garde-fou contre un PDF
    natif pathologique, PAS la contrainte réelle : c'est le budget GLOBAL du
    dossier, appliqué dans claude_client._build_docs_section (120k caractères
    tous documents confondus, en priorisant les preuves de réalisation), qui
    doit arbitrer en cas de dossier réellement volumineux. Historiquement ce
    plafond était à 60 000 et agissait, lui, comme la contrainte RÉELLE sur
    tout document natif dépassant ce seuil -- même quand le dossier complet
    tenait largement sous les 120k du budget global (cas réel observé :
    dossier à 72k caractères au total, facture seule à 72k tronquée à 60k
    alors que 48k de marge dormaient inutilisés ailleurs dans le budget
    global). Le budget global ne peut pas "récupérer" du texte déjà coupé en
    amont -- ce plafond doit donc rester nettement au-dessus de ce que le
    budget global laisserait jamais passer pour un seul document.
    """
    pdf_path = Path(pdf_path)
    pages_total = get_page_count(pdf_path)

    if is_scanned_pdf(pdf_path):
        # Exception ciblée : une PREUVE DE RÉALISATION scannée (facture, DGD,
        # décompte...) OU un document de SOUS-TRAITANCE (DC4...) porte les
        # éléments techniques ou l'identification du sous-traitant, souvent
        # dans des annexes en pages intermédiaires ET dans des tableaux de
        # chiffrage denses (quantités/dimensions serrées entre plusieurs
        # colonnes de prix, cas réel T233337). On élargit son OCR (toutes
        # pages jusqu'à un plafond plus haut, résolution plus fine) plutôt
        # que le schéma générique 3 début + 3 fin à 300dpi — le surcoût est
        # du temps de traitement local (~4-5s/page à cette résolution), pas
        # des tokens. Cas réel (T186090) : DC4 de 10 pages limitée à 6 (1-3 +
        # 8-10), la section identifiant précisément la nature des travaux
        # sous-traités tombait dans les 4 pages intermédiaires non lues.
        _preuve_kw = ("facture", "dgd", "decompte", "décompte", "situation", "solde",
                      "dc4", "sous-traitance", "sous_traitance", "soustraitance")
        ocr_dpi = 300
        if any(kw in pdf_path.name.lower() for kw in _preuve_kw):
            max_pages_ocr = max(max_pages_ocr, 14)
            max_chars_ocr = max(max_chars_ocr, 30000)
            ocr_dpi = 350
        text, meta = ocr_pdf_smart_meta(pdf_path, max_pages_ocr=max_pages_ocr,
                                        max_chars=max_chars_ocr, dpi=ocr_dpi)

        # Compléter avec la couche texte NATIVE résiduelle, si elle existe.
        # Cas réel (T233337) : un décompte scanné porte un AJOUT NUMÉRIQUE
        # tapé par-dessus (ex: "Uw : 1,4 W/m².K - Sw : 0.5" en police 5pt,
        # ajouté après coup) -- ce texte est un vrai objet PDF, lu parfaitement
        # par pdftotext, mais quasi illisible pour Tesseract une fois
        # rasterisé à petite taille : sans fusion, cet ajout (souvent la
        # valeur clé la plus récente/corrigée) disparaissait purement et
        # simplement.
        #
        # Un match EXACT par ligne normalisée est insuffisant pour décider ce
        # qui est déjà couvert : le cas visé (T233337) coexiste avec le cas
        # opposé (T267191) où le PDF porte une couche texte native de MAUVAISE
        # QUALITÉ (OCR bas de gamme d'un copieur) que Tesseract, en repartant
        # de l'image, lit MIEUX -- dans ce cas la couche native décrit le MÊME
        # contenu que l'OCR, juste déformé différemment ("MOTQllë" vs
        # "Marque"), et une comparaison exacte les aurait à tort considérées
        # comme absentes de l'OCR, réinjectant du bruit déjà écarté à dessein.
        # On utilise donc une couverture par PLUS LONGS SEGMENTS COMMUNS de
        # caractères (tolère la déformation OCR) : une ligne n'est ajoutée
        # comme résidu que si une faible part de ses caractères se retrouve
        # dans l'OCR (seuil validé à 0.15 sur cas réels : 3 lignes de bruit
        # vide laissées passer sur 114 lignes d'une couche copieur mal
        # OCRisée, 0 contenu substantiel).
        texte_natif = _extract_native_text_only(pdf_path)
        if texte_natif:
            ocr_norm = _normalise_pour_comparaison(text)
            residu = "\n".join(
                ligne for ligne in texte_natif.split("\n")
                if ligne.strip() and len(_normalise_pour_comparaison(ligne)) >= 6
                and _couverture_floue(ligne, ocr_norm) < 0.15
            )
            if residu.strip():
                text += ("\n\n[TEXTE NUMÉRIQUE SUPERPOSÉ AU SCAN -- objet texte réel du PDF, "
                         "possiblement un ajout/une correction tapée après coup, non capturé par "
                         "l'OCR de l'image (souvent en trop petite police) -- fait foi comme les "
                         "autres mentions du document] :\n" + residu)

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
