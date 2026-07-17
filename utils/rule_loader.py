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

# Nouveau format Excel enrichi (colonnes obligatoire/non-obligatoire distinctes,
# dates réelles, disponible pour BAR). Le format CSV legacy reste utilisé pour BAT
# tant qu'un fichier Excel équivalent n'a pas été fourni pour ce secteur.
FICHE_XLSX = {
    "BAR": "Fiche_BAR.xlsx",
}
FICHE_CSV = {
    "BAR": "Fiche__Récapitulatif_des_fiches_BAR.csv",  # fallback si Fiche_BAR.xlsx absent
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
        fiches = classification.get("fiches")
        if fiches is None:
            single = classification.get("fiche", "INCONNUE")
            fiches = [single] if single else ["INCONNUE"]
        fiches = [f for f in fiches if f and f != "INCONNUE"]
        csv_name = FICHE_CSV.get(secteur)

        for fiche in fiches:
            if csv_name:
                text = self._read_csv_filtered(csv_name, fiche)
                if text:
                    bundle[f"FICHE:{csv_name}:{fiche}"] = text
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
                    bundle[f"CDP:{CDP_CSV}:{fiche}"] = text
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
        """
        Partie variable : fiches filtrées PAR VERSION + grimoires spécifiques — hors cache.

        Cœur du dispositif : utilise classification["date_engagement"] (si disponible)
        pour ne charger que la version de chaque fiche applicable à cette date
        (colonnes DEBUT/FIN D'APPLICATION du CSV Fiche), plutôt que toutes les
        versions. Réduit les tokens et élimine l'ambiguïté de version pour Claude.

        Gère un dossier multi-fiches : classification["fiches"] est une liste.
        Rétrocompatible avec l'ancien format classification["fiche"] (str unique).
        """
        parts = []
        secteur = classification.get("secteur", "BAR")
        date_engagement = classification.get("date_engagement")
        fiches = classification.get("fiches")
        if fiches is None:
            single = classification.get("fiche", "INCONNUE")
            fiches = [single] if single else ["INCONNUE"]
        fiches = [f for f in fiches if f and f != "INCONNUE"]

        if not fiches:
            return ""

        xlsx_name = FICHE_XLSX.get(secteur)
        csv_name = FICHE_CSV.get(secteur)
        version_notes = []

        for fiche in fiches:
            for path in sorted(self.rules_dir.glob("GRIMOIRE__*.md")):
                if self._extract_fiche_code(path.name) == fiche:
                    text = self._read_file(path.name, encoding="utf-8")
                    if text:
                        parts.append(f"--- {path.name} ---\n{text}")

            # Priorité au référentiel Excel enrichi (obligatoire/non-obligatoire
            # distincts, dates réelles) ; repli sur le CSV legacy si absent pour
            # ce secteur (ex: BAT tant qu'aucun Excel équivalent n'existe).
            result = None
            source_label = None
            if xlsx_name and (self.rules_dir / xlsx_name).exists():
                result = self._read_xlsx_filtered_by_date(xlsx_name, fiche, date_engagement)
                source_label = xlsx_name
            if (result is None or not result.get("found")) and csv_name:
                result = self._read_csv_filtered_by_date(csv_name, fiche, date_engagement)
                source_label = csv_name

            # Recoupement date/version : quel que soit le filtrage appliqué,
            # fournir la liste compacte des périodes d'application de TOUTES
            # les versions de la fiche (~quelques dizaines de tokens). Permet
            # à Claude de détecter qu'une date d'engagement qu'il identifie
            # lui-même dans les documents sort de la période de la version
            # chargée (cas d'une date pré-analysée FAUSSE, non couvert par le
            # seul mécanisme TOUTES VERSIONS/AMBIGU qui ne traite que les cas
            # 'pas de date' et 'chevauchement').
            periods_text = self._versions_periods_text(fiche, secteur)
            if periods_text:
                parts.append(periods_text)

            if result and result["text"]:
                label_bits = [f"fiche {fiche}"]
                if result["no_date"]:
                    label_bits.append("TOUTES VERSIONS — date d'engagement non fournie, "
                                       "à déterminer depuis les documents et à recouper "
                                       "manuellement avec les périodes d'application ci-dessous")
                elif result["ambiguous"]:
                    label_bits.append(f"⚠️ AMBIGU — plusieurs versions couvrent la date "
                                       f"{date_engagement} ({', '.join(result['versions_matched'])}) "
                                       f"— la plus récente est listée en premier, à trancher explicitement")
                elif result["versions_matched"]:
                    label_bits.append(f"version applicable au {date_engagement} : "
                                       f"{result['versions_matched'][0]}")
                parts.append(f"--- {source_label} ({', '.join(label_bits)}) ---\n{result['text']}")
                if result["ambiguous"]:
                    version_notes.append(fiche)

            if classification.get("coup_de_pouce"):
                text = self._read_csv_filtered(CDP_CSV, fiche)
                if text:
                    parts.append(f"--- {CDP_CSV} (fiche {fiche}) ---\n{text}")

        return "\n\n".join(parts)


    def get_fiche_correspondance_table(self) -> str:
        """
        Génère la table 'code fiche -> libellé des travaux' — garde-fou
        ESSENTIEL de la classification IA : sans elle, le modèle s'appuie sur
        sa mémoire générale des fiches CEE et peut inverser des codes voisins
        (cas réel observé : combles classés BAR-EN-103 'plancher bas' au lieu
        de BAR-EN-101, et BAR-EN-102 'murs' écarté comme 'toitures terrasses').

        Source PRIMAIRE : le xlsx récapitulatif (même fichier que les règles,
        donc présent dès que l'audit fonctionne). Les CSV ne servent plus que
        de secours si le xlsx manque — l'ancienne implémentation ne lisait QUE
        les CSV et retournait silencieusement vide quand ils n'étaient pas
        déployés (_table_utilisee: false).
        """
        import csv
        import io
        import re

        lines_out = []
        code_re = re.compile(r"(BA[RT]-(?:EN|TH|EQ|SE)-\d+)")

        for secteur in FICHE_XLSX.keys() | FICHE_CSV.keys():
            seen = {}

            # 1) Source primaire : xlsx
            xlsx_name = FICHE_XLSX.get(secteur)
            if xlsx_name and (self.rules_dir / xlsx_name).exists():
                import pandas as pd
                cache_key = f"xlsx_df:{xlsx_name}"
                if cache_key not in self._cache:
                    self._cache[cache_key] = pd.read_excel(self.rules_dir / xlsx_name)
                df = self._cache[cache_key]
                for _, row in df.iterrows():
                    fiche_raw = str(row.get("FICHE") or "").strip()
                    travaux = str(row.get("TRAVAUX") or "").strip()
                    if not fiche_raw or not travaux or travaux.lower() == "nan":
                        continue
                    m = code_re.match(fiche_raw.upper())
                    if m and m.group(1) not in seen:
                        seen[m.group(1)] = travaux

            # 2) Secours : CSV (complète les codes éventuellement absents du xlsx)
            csv_name = FICHE_CSV.get(secteur)
            if csv_name:
                text = self._read_file(csv_name, encoding="latin-1")
                if text:
                    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=";")
                    for row in reader:
                        fiche_raw = (row.get("FICHE") or "").strip()
                        travaux = (row.get("TRAVAUX") or "").strip()
                        if not fiche_raw or not travaux:
                            continue
                        m = code_re.match(fiche_raw.upper())
                        if m and m.group(1) not in seen:
                            seen[m.group(1)] = travaux

            for code, travaux in sorted(seen.items()):
                lines_out.append(f"- {code} ({secteur}) : {travaux}")

        return "\n".join(lines_out)

    def _versions_periods_text(self, fiche: str, secteur: str) -> str:
        """
        Liste compacte 'version : période d'application' de TOUTES les versions
        d'une fiche — injectée dans le bloc variable du prompt pour que Claude
        puisse recouper la date d'engagement qu'il confirme lui-même avec la
        version dont les seuils lui ont été fournis (cf. champ
        date_engagement_confirmee du schéma d'audit). Coût : quelques dizaines
        de tokens par fiche.
        """
        lines = []

        xlsx_name = FICHE_XLSX.get(secteur)
        if xlsx_name and (self.rules_dir / xlsx_name).exists():
            import pandas as pd
            cache_key = f"xlsx_df:{xlsx_name}"
            if cache_key not in self._cache:
                self._cache[cache_key] = pd.read_excel(self.rules_dir / xlsx_name)
            df = self._cache[cache_key]
            fiche_base = fiche.replace("-", "").upper()
            code_col = (df["FICHE"].astype(str).str.split("\n").str[0]
                        .str.replace("-", "").str.strip().str.upper())
            for _, row in df[code_col == fiche_base].iterrows():
                label = str(row.get("FICHE", "")).replace("\n", " ").strip()
                debut = row.get("DEBUT D'APPLICATION ")
                fin = row.get("FIN D'APPLICATION ")
                debut_s = debut.strftime("%d/%m/%Y") if pd.notna(debut) else "?"
                fin_s = fin.strftime("%d/%m/%Y") if pd.notna(fin) else "en cours"
                lines.append(f"- {label} : {debut_s} → {fin_s}")
        else:
            import csv as _csv
            import io as _io
            csv_name = FICHE_CSV.get(secteur)
            full_text = self._read_file(csv_name, encoding="latin-1") if csv_name else None
            if full_text:
                fiche_base = fiche.replace("-", "").upper()
                reader = _csv.DictReader(_io.StringIO(full_text, newline=""), delimiter=";")
                for row in reader:
                    fiche_raw = (row.get("FICHE") or "").strip()
                    if not fiche_raw or fiche_base not in fiche_raw.upper().replace("-", ""):
                        continue
                    debut = (row.get("DEBUT D'APPLICATION (Date d'engagement)") or "").strip() or "?"
                    fin = (row.get("FIN D'APPLICATION (Date d'engagement)") or "").strip() or "en cours"
                    lines.append(f"- {fiche_raw} : {debut} → {fin}")

        if not lines:
            return ""
        return (f"--- Périodes d'application de TOUTES les versions de la fiche {fiche} "
                f"(pour recouper la date d'engagement confirmée) ---\n" + "\n".join(lines))

    def get_qualification_requise(self, fiche: str, secteur: str,
                                   date_engagement: Optional[str] = None) -> dict:
        """
        Indique si une qualification (RGE) est exigée pour cette fiche à cette
        date d'engagement, d'après la colonne 'QUALIFICATION DU PROFESSIONNEL'
        du récap xlsx. Utilisé par l'app pour n'afficher le statut RGE que
        quand il est pertinent (55 versions BAR sur 132 n'exigent rien).

        Returns:
            {"requise": bool|None, "texte": str|None}
            - requise=None si la fiche/version est introuvable dans le xlsx.
            - Le texte peut contenir des exigences dépendant de sous-périodes
              d'engagement ('Trx engagés à partir du 01/01/2021 : ...') — il
              est retourné brut pour affichage.
        """
        xlsx_name = FICHE_XLSX.get(secteur)
        if not (xlsx_name and (self.rules_dir / xlsx_name).exists()):
            return {"requise": None, "texte": None}
        import pandas as pd
        cache_key = f"xlsx_df:{xlsx_name}"
        if cache_key not in self._cache:
            self._cache[cache_key] = pd.read_excel(self.rules_dir / xlsx_name)
        df = self._cache[cache_key]
        fiche_base = fiche.replace("-", "").upper()
        code_col = (df["FICHE"].astype(str).str.split("\n").str[0]
                    .str.replace("-", "").str.strip().str.upper())
        rows = df[code_col == fiche_base]
        if rows.empty:
            return {"requise": None, "texte": None}

        # Filtrer par date d'engagement si fournie, sinon version la plus récente
        row = None
        if date_engagement:
            try:
                from datetime import datetime as _dt
                d = _dt.strptime(date_engagement, "%d/%m/%Y")
                for _, r in rows.iterrows():
                    debut, fin = r.get("DEBUT D'APPLICATION "), r.get("FIN D'APPLICATION ")
                    if pd.notna(debut) and debut <= d and (pd.isna(fin) or d <= fin):
                        row = r
                        break
            except ValueError:
                pass
        if row is None:
            row = rows.iloc[-1]  # version la plus récente par défaut

        val = row.get("QUALIFICATION DU PROFESSIONNEL")
        texte = str(val).strip() if pd.notna(val) and str(val).strip() else None
        if texte is None or "non obligatoire" in texte.lower():
            return {"requise": False, "texte": texte}
        return {"requise": True, "texte": texte}

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
        """Retourne TOUTES les versions d'une fiche (comportement générique, sans
        connaissance de la date d'engagement). Préférer _read_csv_filtered_by_date()
        quand une date d'engagement est disponible, pour ne charger que la version
        applicable — plus fiable et moins coûteux en tokens."""
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

    def _read_xlsx_filtered_by_date(
        self, filename: str, fiche: str, date_engagement: Optional[str]
    ) -> Dict[str, object]:
        """
        Équivalent de _read_csv_filtered_by_date() pour le nouveau format Excel
        enrichi (colonnes obligatoire/non-obligatoire distinctes, dates réelles).
        Formate le résultat en Markdown lisible plutôt qu'en ligne CSV brute,
        pour une meilleure lecture par Claude et une consommation de tokens
        équivalente ou meilleure (pas de répétition des noms de colonnes vides).
        """
        import pandas as pd
        from datetime import datetime

        path = self.rules_dir / filename
        if not path.exists():
            return {"text": "", "versions_matched": [], "ambiguous": False,
                     "no_date": True, "found": False}

        cache_key = f"xlsx_df:{filename}"
        if cache_key not in self._cache:
            self._cache[cache_key] = pd.read_excel(path)
        df = self._cache[cache_key]

        fiche_base = fiche.replace("-", "").upper()
        # La colonne FICHE contient "BAR-EN-101\nA14.1" (code + version sur 2 lignes)
        code_col = df["FICHE"].astype(str).str.split("\n").str[0].str.replace("-", "").str.strip().str.upper()
        rows = df[code_col == fiche_base]

        if rows.empty:
            return {"text": "", "versions_matched": [], "ambiguous": False,
                     "no_date": True, "found": False}

        debut_col = "DEBUT D'APPLICATION "
        fin_col = "FIN D'APPLICATION "

        if not date_engagement:
            return {"text": self._format_xlsx_rows(rows), "versions_matched": [],
                     "ambiguous": False, "no_date": True, "found": True}

        try:
            dt_engagement = datetime.strptime(date_engagement.strip(), "%d/%m/%Y")
        except ValueError:
            return {"text": self._format_xlsx_rows(rows), "versions_matched": [],
                     "ambiguous": False, "no_date": True, "found": True}

        def _in_range(row):
            debut = row.get(debut_col)
            fin = row.get(fin_col)
            if pd.notna(debut) and dt_engagement < debut:
                return False
            if pd.notna(fin) and dt_engagement > fin:
                return False
            return True

        matched = rows[rows.apply(_in_range, axis=1)]

        if matched.empty:
            # Aucune version ne couvre cette date -> renvoyer tout + signaler
            return {"text": self._format_xlsx_rows(rows), "versions_matched": [],
                     "ambiguous": False, "no_date": False, "found": True}

        versions = [str(v).replace("\n", " ").strip() for v in matched["FICHE"]]

        if len(matched) == 1:
            return {"text": self._format_xlsx_rows(matched), "versions_matched": versions,
                     "ambiguous": False, "no_date": False, "found": True}

        # Chevauchement de versions sur cette date : la plus récente en premier
        matched_sorted = matched.sort_values(by=debut_col, ascending=False)
        versions_sorted = [str(v).replace("\n", " ").strip() for v in matched_sorted["FICHE"]]
        return {"text": self._format_xlsx_rows(matched_sorted), "versions_matched": versions_sorted,
                 "ambiguous": True, "no_date": False, "found": True}

    @staticmethod
    def _format_xlsx_rows(rows) -> str:
        """Formate les lignes du référentiel Excel en Markdown lisible.
        Injecte aussi la checklist des champs atomiques (technical_schema)
        pour guider la réponse structurée de l'appel d'audit."""
        import pandas as pd
        from utils.technical_schema import build_fields_checklist_text

        blocks = []
        for _, row in rows.iterrows():
            fiche_label = str(row.get("FICHE", "")).replace("\n", " ").strip()
            lines = [f"#### {fiche_label} — {row.get('TRAVAUX', '')}"]

            debut = row.get("DEBUT D'APPLICATION ")
            fin = row.get("FIN D'APPLICATION ")
            debut_s = debut.strftime("%d/%m/%Y") if pd.notna(debut) else "?"
            fin_s = fin.strftime("%d/%m/%Y") if pd.notna(fin) else "non définie"
            lines.append(f"*Période d'application : {debut_s} → {fin_s}*")

            field_labels = [
                ("CONDITIONS TECHNIQUES D'ELIGIBILITE AUX CEE", "**Conditions techniques d'éligibilité (seuils minimums)**"),
                ("MENTIONS OBLIGATOIRES SUR LA PREUVE DE REALISATION", "**Mentions OBLIGATOIRES sur la preuve de réalisation**"),
                ("MENTION NON OBLIGATOIRE SUR LA PREUVE DE REALISAITON MAIS NECESSAIRE", "**Mentions non obligatoires mais nécessaires (tolérance possible)**"),
                ("QUALIFICATION DU PROFESSIONNEL", "**Qualification du professionnel requise**"),
                ("CONDITIONS SUPPLEMENTAIRES POUR LE COUP DE POUCE", "**Conditions Coup de Pouce (si applicable)**"),
                ("ELIGIBILITE AUX CONTROLES\n(Date d'engagement)", "**Éligibilité aux contrôles**"),
                ("FICHES\nINCOMPATIBILITE ", "**Fiches incompatibles**"),
            ]
            mentions_obligatoires_raw = None
            mentions_necessaires_raw = None
            qualification_presente = False
            for col, label in field_labels:
                val = row.get(col)
                if pd.notna(val) and str(val).strip():
                    lines.append(f"{label} :\n{str(val).strip()}")
                    if col == "MENTIONS OBLIGATOIRES SUR LA PREUVE DE REALISATION":
                        mentions_obligatoires_raw = str(val).strip()
                    elif col == "MENTION NON OBLIGATOIRE SUR LA PREUVE DE REALISAITON MAIS NECESSAIRE":
                        mentions_necessaires_raw = str(val).strip()
                    elif col == "QUALIFICATION DU PROFESSIONNEL":
                        qualification_presente = True

            # RGE non requise : quand la colonne QUALIFICATION est vide pour cette
            # version, le dire EXPLICITEMENT à Claude — sinon les règles générales
            # de regles_rge.md le pousseraient à exiger un certificat RGE à tort
            # (55 versions sur 132 du référentiel BAR ne l'exigent pas).
            if not qualification_presente:
                lines.append("**Qualification du professionnel requise** :\n"
                             "▪ AUCUNE qualification RGE n'est exigée pour cette fiche/version. "
                             "L'axe RGE doit être évalué 'non requis' : contrôles RGE marqués "
                             "conformes avec la mention 'non requis pour cette fiche', et "
                             "l'absence de certificat RGE n'est PAS une anomalie.")

            if mentions_obligatoires_raw:
                checklist = build_fields_checklist_text(mentions_obligatoires_raw, severite="obligatoire")
                if checklist:
                    lines.append(checklist)

            if mentions_necessaires_raw:
                checklist_necessaire = build_fields_checklist_text(mentions_necessaires_raw, severite="necessaire")
                if checklist_necessaire:
                    lines.append(checklist_necessaire)

            blocks.append("\n".join(lines))

        return "\n\n".join(blocks)

    def _read_csv_filtered_by_date(
        self, filename: str, fiche: str, date_engagement: Optional[str]
    ) -> Dict[str, object]:
        """
        Filtre le CSV Fiche sur LA version applicable à la date d'engagement,
        au lieu de renvoyer toutes les versions. C'est le cœur du dispositif :
        chaque fiche a plusieurs versions (A14, A27, A39...) avec des périodes
        d'application et des critères techniques potentiellement différents —
        seule la version couvrant la date d'engagement du dossier doit être
        utilisée pour vérifier les éléments techniques minimums.

        Returns:
            {
                "text": str,            # ligne(s) CSV retenue(s)
                "versions_matched": [str],  # codes fiche exacts retenus (ex: "BAR-EN-101 A54.5")
                "ambiguous": bool,      # True si plusieurs versions se chevauchent sur cette date
                "no_date": bool,        # True si aucune date d'engagement fournie -> fallback toutes versions
            }
        """
        import csv as _csv
        import io as _io
        from datetime import datetime

        full_text = self._read_file(filename, encoding="latin-1")
        if not full_text or fiche == "INCONNUE":
            return {"text": full_text[:3000] if full_text else "", "versions_matched": [],
                    "ambiguous": False, "no_date": True}

        if not date_engagement:
            # Pas de date -> comportement de repli : toutes les versions
            text = self._read_csv_filtered(filename, fiche)
            return {"text": text or "", "versions_matched": [], "ambiguous": False, "no_date": True}

        try:
            dt_engagement = datetime.strptime(date_engagement.strip(), "%d/%m/%Y")
        except ValueError:
            text = self._read_csv_filtered(filename, fiche)
            return {"text": text or "", "versions_matched": [], "ambiguous": False, "no_date": True}

        reader = _csv.DictReader(_io.StringIO(full_text, newline=""), delimiter=";")
        fiche_base = fiche.replace("-", "").upper()
        matches = []

        for row in reader:
            fiche_raw = (row.get("FICHE") or "").strip()
            if not fiche_raw:
                continue
            if fiche_base not in fiche_raw.upper().replace("-", ""):
                continue

            debut_str = (row.get("DEBUT D'APPLICATION (Date d'engagement)") or "").strip()
            fin_str = (row.get("FIN D'APPLICATION (Date d'engagement)") or "").strip()
            try:
                debut = datetime.strptime(debut_str, "%d/%m/%Y") if debut_str else None
            except ValueError:
                debut = None
            try:
                fin = datetime.strptime(fin_str, "%d/%m/%Y") if fin_str else None
            except ValueError:
                fin = None

            if debut and dt_engagement < debut:
                continue
            if fin and dt_engagement > fin:
                continue
            # Ligne applicable à cette date (ou dates non renseignées -> on la garde par prudence)
            matches.append(row)

        if not matches:
            # Aucune version ne couvre cette date -> fallback toutes versions + alerte
            text = self._read_csv_filtered(filename, fiche)
            return {"text": text or "", "versions_matched": [], "ambiguous": False, "no_date": False}

        if len(matches) == 1:
            row = matches[0]
            text = ";".join(str(v) for v in row.values())
            return {
                "text": text, "versions_matched": [row.get("FICHE", "")],
                "ambiguous": False, "no_date": False,
            }

        # Plusieurs versions couvrent la même date (chevauchement de périodes) :
        # on retient la version au DEBUT D'APPLICATION le plus récent (la plus à
        # jour), mais on renvoie TOUTES les versions concurrentes + flag ambiguous
        # pour que l'audit final signale explicitement le choix à l'utilisateur.
        def _debut_key(row):
            try:
                return datetime.strptime(row.get("DEBUT D'APPLICATION (Date d'engagement)", ""), "%d/%m/%Y")
            except (ValueError, TypeError):
                return datetime.min

        matches_sorted = sorted(matches, key=_debut_key, reverse=True)
        text = "\n".join(";".join(str(v) for v in row.values()) for row in matches_sorted)
        return {
            "text": text,
            "versions_matched": [row.get("FICHE", "") for row in matches_sorted],
            "ambiguous": True,
            "no_date": False,
        }
