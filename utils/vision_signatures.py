"""
Vérification VISUELLE des signatures et tampons sur les documents d'engagement.

Pourquoi : l'audit principal travaille sur le TEXTE extrait des PDF — une
signature manuscrite ou un tampon n'y existent pas (limite structurelle,
documentée dans le prompt d'audit : "non détectable par extraction de texte").
Ce module comble ce trou avec la vision native de Claude : les pages du
document d'engagement sont rendues en image (PyMuPDF) et envoyées à l'API
avec UNE question fermée et une sortie structurée forcée.

Coût maîtrisé : ~1 600 tokens par page d'image + ~400 tokens de prompt,
effort 'low' (tâche de perception simple, pas de raisonnement réglementaire),
soit ~0,01 € par dossier. Aucune règle CEE envoyée — ce n'est PAS un audit,
c'est un constat visuel qui alimente l'audit.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Dict, List, Optional

MAX_PAGES_VISION = 8    # plafond de pages envoyées en vision par document
RENDER_DPI = 120         # ~1400x2000 px sur A4 : lisible sans exploser les tokens
_SCAN_OCR_DPI = 100      # OCR rapide de pré-sélection sur les pages scannées

# Libellés IMPRIMÉS qui accompagnent les blocs de signature — détectables dans
# le texte même quand la signature manuscrite ne l'est pas. C'est ce qui permet
# de trouver une page de signature AU MILIEU d'un acte d'engagement multi-pages
# sans envoyer tout le document en vision.
_KW_SIGNATURE = (
    "signature", "signataire", "signe", "signé", "lu et approuv",
    "bon pour accord", "pour acceptation", "cachet", "tampon", "fait a", "fait à",
    "maitre d'ouvrage", "maître d'ouvrage", "moa", "visa", "pour l'entreprise",
    "le titulaire", "le pouvoir adjudicateur", "acte d'engagement",
)


def _pages_candidates(pdf_path: Path) -> List[int]:
    """
    Indices (0-based) des pages à envoyer en vision : celles dont le texte
    (couche native, ou OCR rapide si page scannée) contient un libellé de bloc
    de signature — la signature d'un acte d'engagement est souvent sur une
    page INTERMÉDIAIRE. La première et la dernière page sont toujours
    incluses (usages BC/devis). Plafonné à MAX_PAGES_VISION.
    """
    import fitz
    import unicodedata

    def _n(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        return s.lower()

    kw = [_n(k) for k in _KW_SIGNATURE]
    doc = fitz.open(str(pdf_path))
    n_pages = doc.page_count

    if n_pages <= MAX_PAGES_VISION:
        doc.close()
        return list(range(n_pages))

    candidates = {0, n_pages - 1}
    for i, page in enumerate(doc):
        texte = page.get_text() or ""
        if not texte.strip():
            # Page scannée : OCR rapide basse résolution, uniquement pour la
            # détection de mots-clés (~1-2 s/page).
            try:
                import pytesseract
                from PIL import Image
                import io
                pix = page.get_pixmap(dpi=_SCAN_OCR_DPI)
                texte = pytesseract.image_to_string(
                    Image.open(io.BytesIO(pix.tobytes("png"))), lang="fra")
            except Exception:
                texte = ""
        t = _n(texte)
        if any(k in t for k in kw):
            candidates.add(i)
    doc.close()

    ordered = sorted(candidates)
    if len(ordered) > MAX_PAGES_VISION:
        # Prioriser les pages à mots-clés du MILIEU (les extrémités sont déjà
        # incluses d'office et sont souvent pages de garde/annexes génériques).
        interieur = [p for p in ordered if p not in (0, n_pages - 1)]
        ordered = sorted(set([0, n_pages - 1] + interieur[:MAX_PAGES_VISION - 2]))
    return ordered

VISION_TOOL_SCHEMA = {
    "name": "rapport_signatures",
    "description": "Constat visuel des signatures et tampons sur les pages fournies.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "document": {"type": "string"},
                        "page": {"type": "integer"},
                        "signature_manuscrite_presente": {
                            "type": "boolean",
                            "description": "Une signature manuscrite (tracé à la main) est visible sur la page.",
                        },
                        "tampon_present": {
                            "type": "boolean",
                            "description": "Un tampon/cachet d'entreprise ou d'organisme est visible.",
                        },
                        "date_manuscrite_ou_tamponnee": {
                            "type": ["string", "null"],
                            "description": ("Date associée à la signature/au tampon si LISIBLE "
                                             "(JJ/MM/AAAA). null si absente ou illisible. Ne pas "
                                             "confondre avec les dates imprimées du document."),
                        },
                        "bloc": {
                            "type": ["string", "null"],
                            "description": ("Où se trouve la signature/le tampon : libellé du bloc "
                                             "adjacent tel qu'imprimé (ex: 'Le Maître d'ouvrage', "
                                             "'Bon pour accord', 'L'entreprise'). null si aucun "
                                             "libellé adjacent."),
                        },
                        "commentaire": {
                            "type": ["string", "null"],
                            "description": "Toute observation utile (signature partielle, tampon illisible...).",
                        },
                    },
                    "required": ["document", "page", "signature_manuscrite_presente",
                                  "tampon_present", "date_manuscrite_ou_tamponnee", "bloc"],
                },
            },
        },
        "required": ["pages"],
    },
}

_SYSTEM = (
    "Tu examines VISUELLEMENT des pages de documents contractuels français "
    "(bons de commande, ordres de service, devis). Ta seule mission : constater "
    "la présence de signatures MANUSCRITES et de tampons/cachets, avec leur "
    "date et le bloc où ils se trouvent. Règles strictes :\n"
    "- Une signature manuscrite est un tracé à la main — un nom DACTYLOGRAPHIÉ "
    "n'est PAS une signature.\n"
    "- Ne déduis rien : si une zone de signature est vide, signature absente.\n"
    "- Rapporte le libellé imprimé du bloc tel quel, sans interpréter qui est "
    "MOA ou entreprise.\n"
    "Réponds uniquement via l'outil rapport_signatures."
)




def render_pages(pdf_path: Path) -> List[Dict]:
    """Rend les pages pertinentes d'un PDF en PNG base64 (sélection par
    mots-clés de blocs de signature — voir _pages_candidates)."""
    import fitz
    pages_idx = _pages_candidates(Path(pdf_path))
    doc = fitz.open(str(pdf_path))
    out = []
    for i in pages_idx:
        pix = doc[i].get_pixmap(dpi=RENDER_DPI)
        out.append({
            "document": Path(pdf_path).name,
            "page": i + 1,
            "b64": base64.standard_b64encode(pix.tobytes("png")).decode(),
        })
    doc.close()
    return out


def check_signatures(pdf_paths: List, model: str = "claude-sonnet-5") -> Dict:
    """
    Constat visuel des signatures/tampons sur les documents fournis
    (typiquement : les documents d'engagement identifiés par l'audit).

    Returns:
        {"pages": [...], "tokens_used": {...}} ou {"erreur": str}
    """
    import anthropic

    pages = []
    for p in pdf_paths:
        try:
            pages.extend(render_pages(Path(p)))
        except Exception as e:
            return {"erreur": f"Rendu impossible pour {p}: {e}"}
    if not pages:
        return {"erreur": "Aucune page à examiner."}

    content = []
    for pg in pages:
        content.append({"type": "text",
                         "text": f"Document « {pg['document']} », page {pg['page']} :"})
        content.append({"type": "image",
                         "source": {"type": "base64", "media_type": "image/png",
                                     "data": pg["b64"]}})
    content.append({"type": "text",
                     "text": ("Constate signatures manuscrites, tampons, dates et blocs "
                              "pour CHACUNE des pages ci-dessus via l'outil "
                              "rapport_signatures (une entrée par page).")})

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=1500,
        system=_SYSTEM,
        messages=[{"role": "user", "content": content}],
        tools=[VISION_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "rapport_signatures"},
        # Perception simple : pas besoin de raisonnement profond.
        output_config={"effort": "low"},
    )

    rapport = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "rapport_signatures":
            rapport = dict(block.input)
            break
    if rapport is None:
        return {"erreur": f"Réponse vision inattendue (stop_reason={response.stop_reason})"}

    return {
        "pages": rapport.get("pages", []),
        "tokens_used": {
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
        },
    }


def selectionner_docs_engagement(pdf_paths: List, audit: dict) -> List:
    """
    Sélectionne les PDF à examiner : ceux catégorisés documents_engagement par
    l'audit (correspondance par inclusion de nom), sinon repli sur les mots-clés
    de noms de fichiers usuels des preuves d'engagement.
    """
    import unicodedata

    def _n(s: str) -> str:
        s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
        return "".join(c for c in s.lower() if c.isalnum())

    noms_eng = [_n(x) for x in (audit.get("documents_engagement") or [])]
    selection = []
    for p in pdf_paths:
        stem = _n(Path(p).stem)
        if noms_eng and any(ne in stem or stem in ne for ne in noms_eng):
            selection.append(p)
    if not selection:
        _KW = ("bc", "bondecommande", "os", "ordredeservice", "devis",
                "acteengagement", "engagement", "marche", "bondetravaux")
        selection = [p for p in pdf_paths if any(k in _n(Path(p).stem) for k in _KW)]
    return selection
