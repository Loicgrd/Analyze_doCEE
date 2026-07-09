"""
Chargement sélectif des règles métier CEE — version Markdown.

Socle : les 6 fichiers Markdown (règles Doc + validation), toujours chargés (~6,7k tokens).
Variable : la ligne de la fiche BAR/BAT concernée, filtrée depuis le CSV (~0,3k tokens).
Les Grimoires ne sont PAS chargés par défaut (option --with-grimoires possible).
"""

from pathlib import Path
from typing import Dict, Optional


# Socle Markdown — toujours chargé, stable => idéal pour le prompt caching
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
    """Charge le socle Markdown + la fiche BAR/BAT filtrée."""

    def __init__(self, rules_dir: Path):
        self.rules_dir = Path(rules_dir)
        self._cache: Dict[str, str] = {}

    def load_for_classification(self, classification: Dict, verbose: bool = False) -> Dict[str, str]:
        bundle = {}
        loaded = []

        # --- Socle Markdown (stable → cacheable) ---
        for md in MD_CORE:
            text = self._read_file(md, encoding="utf-8")
            if text:
                bundle[f"CORE:{md}"] = text
                loaded.append(md)

        # --- Fiche BAR/BAT filtrée (variable selon dossier) ---
        secteur = classification.get("secteur", "BAR")
        fiche = classification.get("fiche", "INCONNUE")
        csv_name = FICHE_CSV.get(secteur)
        if csv_name:
            text = self._read_csv_filtered(csv_name, fiche)
            if text:
                bundle[f"FICHE:{csv_name}"] = text
                loaded.append(f"{csv_name} (filtré: {fiche})")

        # --- Coup de pouce si détecté ---
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
        """Retourne le socle stable (pour le bloc caché du prompt).
        Inclut automatiquement tout GRIMOIRE__*.md générique déposé dans rules_data."""
        parts = []
        for md in MD_CORE:
            text = self._read_file(md, encoding="utf-8")
            if text:
                parts.append(f"--- {md} ---\n{text}")
        # Auto-découverte : grimoires génériques (sans code fiche dans le nom)
        for path in sorted(self.rules_dir.glob("GRIMOIRE__*.md")):
            if not self._extract_fiche_code(path.name):
                text = self._read_file(path.name, encoding="utf-8")
                if text:
                    parts.append(f"--- {path.name} ---\n{text}")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_fiche_code(filename: str):
        """Détecte un code fiche (BARTH130, BAR-EN-105...) dans un nom de fichier."""
        import re
        m = re.search(r"(BA[RT])[-_]?(EN|TH)[-_]?(\d{3})", filename.upper())
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return None

    def get_variable_rules_text(self, classification: Dict) -> str:
        """Retourne la partie variable (fiche filtrée + grimoires spécifiques) — hors cache."""
        parts = []
        secteur = classification.get("secteur", "BAR")
        fiche = classification.get("fiche", "INCONNUE")
        # Auto-découverte : grimoires spécifiques à la fiche détectée
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

    # ------------------------------------------------------------------

    def _read_file(self, filename: str, encoding: str = "latin-1") -> Optional[str]:
        key = f"{encoding}:{filename}"
        if key in self._cache:
            return self._cache[key]
        path = self.rules_dir / filename
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding=encoding, errors="replace")
            self._cache[key] = text
            return text
        except Exception:
            return None

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
