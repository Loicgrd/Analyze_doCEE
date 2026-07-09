"""
Chargement sélectif des règles métier CEE — version Markdown.

Socle : les 6 fichiers Markdown (règles Doc + validation), toujours chargés (~6,7k tokens).
Variable : la ligne de la fiche BAR/BAT concernée, filtrée depuis le CSV (~0,3k tokens).

Grimoires (optionnels, à déposer dans rules_data/) :
- GRIMOIRE__NomGenerique.md  → rejoint le socle (chargé à chaque analyse, caché)
- GRIMOIRE__BARTH130.md      → chargé seulement si cette fiche est détectée (variable)
"""

import re
from pathlib import Path
from typing import Dict, Optional


MD_CORE = [
    "regles_validation.md",
    "regles_engagement.md",
    "regles_realisation.md",
    "regles_rge.md",
    "regles_ah.md",
    "regles_autres_documents.md",
]

FICHE_CSV = {
    "BAR": "Fiche__Récapitulatif_des_fiches_BAR.csv",
    "BAT": "Fiche__Récapitulatif_des_fiches_BAT.csv",
}

CDP_CSV = "Fiche__Récapitulatif_des_COUP_DE_POUCE.csv"


class RuleLoader:
    """Charge le socle Markdown + la fiche BAR/BAT filtrée + grimoires auto-détectés."""

    def __init__(self, rules_dir: Path):
        self.rules_dir = Path(rules_dir)
        self._cache: Dict[str, str] = {}

    def load_for_classification(self, classification: Dict, verbose: bool = False) -> Dict[str, str]:
        """API legacy — retourne un dict {label: contenu}."""
        bundle = {}
        loaded = []

        for md in MD_CORE:
            text = self._read_file(md, encoding="utf-8")
            if text:
                bundle[f"CORE:{md}"] = text
                loaded.append(md)

        for path in sorted(self.rules_dir.glob("GRIMOIRE__*.md")):
            if not self._extract_fiche_code(path.name):
                text = self._read_file(path.name, encoding="utf-8")
                if text:
                    bundle[f"CORE:{path.name}"] = text
                    loaded.append(path.name)

        secteur = classification.get("secteur", "BAR")
        fiche = classification.get("fiche", "INCONNUE")
        csv_name = FICHE_CSV.get(secteur)
        if csv_name:
            text = self._read_csv_filtered(csv_name, fiche)
            if text:
                bundle[f"FICHE:{csv_name}"] = text
                loaded.append(f"{csv_name} (filtré: {fiche})")

        for path in sorted(self.rules_dir.glob("GRIMOIRE__*.md")):
            if self._extract_fiche_code(path.name) == fiche:
                text = self._read_file(path.name, encoding="utf-8")
                if text:
                    bundle[f"FICHE:{path.name}"] = text
                    loaded.append(path.name)

        if classification.get("coup_de_pouce"):
            text = self._read_csv_filtered(CDP_CSV, fiche)
            if text:
                bundle[f"CDP:{CDP_CSV}"] = text
                loaded.append(f"{CDP_CSV} (filtré: {fiche})")

        if verbose:
            total = sum(len(v) for v in bundle.values())
            print(f"   → {len(loaded)} fichier(s) chargés, ~{total // 4:,} tokens")
            for f in loaded:
                print(f"      • {f}")

        return bundle

    def get_core_rules_text(self) -> str:
        """Socle stable (bloc caché du prompt) : 6 MD + grimoires génériques."""
        parts = []
        for md in MD_CORE:
            text = self._read_file(md, encoding="utf-8")
            if text:
                parts.append(f"--- {md} ---\n{text}")
        for path in sorted(self.rules_dir.glob("GRIMOIRE__*.md")):
            if not self._extract_fiche_code(path.name):
                text = self._read_file(path.name, encoding="utf-8")
                if text:
                    parts.append(f"--- {path.name} ---\n{text}")
        return "\n\n".join(parts)

    def get_variable_rules_text(self, classification: Dict) -> str:
        """Partie variable : fiche filtrée + grimoire spécifique à la fiche — hors cache."""
        parts = []
        secteur = classification.get("secteur", "BAR")
        fiche = classification.get("fiche", "INCONNUE")

        for path in sorted(self.rules_dir.glob("GRIMOIRE__*.md")):
            if self._extract_fiche_code(path.name) == fiche:
                text = self._read_file(path.name, encoding="utf-8")
                if text:
                    parts.append(f"--- {path.name} ---\n{text}")

        csv_name = FICHE_CSV.get(secteur)
        if csv_name:
            text = self._read_csv_filtered(csv_name, fiche)
            if text:
                parts.append(f"--- {csv_name} (fiche {fiche}) ---\n{text}")

        if classification.get("coup_de_pouce"):
            text = self._read_csv_filtered(CDP_CSV, fiche)
            if text:
                parts.append(f"--- {CDP_CSV} (fiche {fiche}) ---\n{text}")

        return "\n\n".join(parts)


    def get_fiche_correspondance_table(self) -> str:
        """
        Génère la table 'code fiche -> libellé des travaux' à partir des CSV
        Fiche BAR/BAT — utilisée pour la classification IA, afin que le modèle
        s'appuie sur la nomenclature officielle plutôt que sur sa mémoire générale.
        """
        import csv
        import io
        import re

        lines_out = []
        for secteur, filename in FICHE_CSV.items():
            text = self._read_file(filename, encoding="latin-1")
            if not text:
                continue
            reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=";")
            seen = {}
            for row in reader:
                fiche_raw = (row.get("FICHE") or "").strip()
                travaux = (row.get("TRAVAUX") or "").strip()
                if not fiche_raw or not travaux:
                    continue
                m = re.match(r"(BA[RT]-(?:EN|TH)-\d+)", fiche_raw.upper())
                if not m:
                    continue
                code = m.group(1)
                if code not in seen:
                    seen[code] = travaux
            for code, travaux in sorted(seen.items()):
                lines_out.append(f"- {code} ({secteur}) : {travaux}")

        return "\n".join(lines_out)

    # ------------------------------------------------------------------

    @staticmethod
    def _extract_fiche_code(filename: str) -> Optional[str]:
        """Détecte un code fiche (BARTH130, BAR-EN-105...) dans un nom de fichier."""
        m = re.search(r"(BA[RT])[-_]?(EN|TH)[-_]?(\d{3})", filename.upper())
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return None

    def _read_file(self, filename: str, encoding: str = "latin-1") -> Optional[str]:
        """
        Lit un fichier texte. Pour les CSV (encoding="latin-1" passé par défaut),
        essaie plusieurs encodages candidats car les exports Excel Mac produisent
        du Mac Roman (accents type "faade" -> "façade" mal décodés en Latin-1),
        et on choisit celui qui produit le moins de caractères de remplacement.
        Les fichiers Markdown (encoding="utf-8") sont lus directement.
        """
        key = f"{encoding}:{filename}"
        if key in self._cache:
            return self._cache[key]
        path = self.rules_dir / filename
        if not path.exists():
            return None

        if encoding == "utf-8":
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                self._cache[key] = text
                return text
            except Exception:
                return None

        # Détection auto pour les CSV : on teste plusieurs encodages candidats
        # et on garde celui qui produit le moins d'échecs de décodage.
        raw = path.read_bytes()
        candidates = ["mac_roman", "cp850", "latin-1", "utf-8"]
        best_text, best_errors = None, None
        for cand in candidates:
            try:
                text = raw.decode(cand)
                errors = text.count("�")
                if best_errors is None or errors < best_errors:
                    best_text, best_errors = text, errors
                if errors == 0:
                    break
            except (UnicodeDecodeError, LookupError):
                continue

        if best_text is None:
            best_text = raw.decode("latin-1", errors="replace")

        self._cache[key] = best_text
        return best_text

    def _read_csv_filtered(self, filename: str, fiche: str) -> Optional[str]:
        full_text = self._read_file(filename, encoding="latin-1")
        if not full_text:
            return None
        if fiche == "INCONNUE":
            return full_text[:3000]

        lines = full_text.split("\n")
        header = lines[0] if lines else ""
        fiche_base = fiche.replace("-", "").upper()

        filtered = [header]
        for line in lines[1:]:
            line_norm = line.upper().replace("-", "")
            if fiche_base in line_norm or fiche.upper() in line.upper():
                filtered.append(line)

        if len(filtered) <= 1:
            return "\n".join(lines[:50])
        return "\n".join(filtered)
