"""
CEE Dossier Analyzer
====================
Analyse un dossier CEE (ZIP ou dossier de PDFs) en utilisant
un chargement sélectif des règles pour minimiser les tokens.
"""

import os
import sys
import json
import zipfile
import tempfile
import argparse
from pathlib import Path

from utils.extractor import extract_zip, extract_text_from_pdf, is_scanned_pdf, ocr_pdf_page
from utils.classifier import classify_dossier
from utils.rule_loader import RuleLoader
from utils.claude_client import analyze_with_claude


def process_dossier(input_path: str, rules_dir: str, verbose: bool = False) -> dict:
    """
    Pipeline complet d'analyse d'un dossier CEE.

    Args:
        input_path: Chemin vers le ZIP ou dossier contenant les PDFs
        rules_dir: Chemin vers le dossier contenant les règles (grimoires, CSV)
        verbose: Afficher les détails de traitement

    Returns:
        dict avec les clés: fiche, statut, details, tokens_used
    """
    input_path = Path(input_path)
    rules_dir = Path(rules_dir)

    # --- 1. Extraction des fichiers ---
    if verbose:
        print(f"[1/4] Extraction de : {input_path.name}")

    with tempfile.TemporaryDirectory() as tmpdir:
        if input_path.suffix.lower() == ".zip":
            pdf_files = extract_zip(input_path, tmpdir)
        elif input_path.is_dir():
            pdf_files = list(input_path.glob("*.pdf"))
        else:
            raise ValueError(f"Format non supporté : {input_path.suffix}")

        if not pdf_files:
            raise ValueError("Aucun PDF trouvé dans le dossier/ZIP fourni.")

        # --- 2. Extraction du texte de chaque PDF ---
        if verbose:
            print(f"[2/4] Lecture de {len(pdf_files)} PDF(s)...")

        docs = {}
        for pdf_path in pdf_files:
            pdf_path = Path(pdf_path)
            name = pdf_path.stem.lower()

            if is_scanned_pdf(pdf_path):
                # OCR sur la première page pour les docs scannés
                text = ocr_pdf_page(pdf_path, page=1)
                docs[name] = {"text": text, "scanned": True, "path": str(pdf_path)}
            else:
                text = extract_text_from_pdf(pdf_path)
                docs[name] = {"text": text, "scanned": False, "path": str(pdf_path)}

            if verbose:
                preview = docs[name]["text"][:80].replace("\n", " ")
                scanned_tag = " [SCANNÉ]" if docs[name]["scanned"] else ""
                print(f"   • {pdf_path.name}{scanned_tag}: {preview}...")

        # --- 3. Classification et chargement sélectif des règles ---
        if verbose:
            print("[3/4] Classification du dossier...")

        classification = classify_dossier(docs)

        if verbose:
            print(f"   → Fiche détectée : {classification['fiche']}")
            print(f"   → Type secteur   : {classification['secteur']}")
            print(f"   → Coup de pouce  : {classification['coup_de_pouce']}")

        loader = RuleLoader(rules_dir)
        core_rules = loader.get_core_rules_text()
        variable_rules = loader.get_variable_rules_text(classification)
        if verbose:
            print(f"   → Socle règles : ~{len(core_rules)//4:,} tk (caché) | "
                  f"Variable : ~{len(variable_rules)//4:,} tk")

        # --- 4. Appel API Claude ---
        if verbose:
            print("[4/4] Analyse par Claude...")

        result = analyze_with_claude(
            docs=docs,
            core_rules_text=core_rules,
            variable_rules_text=variable_rules,
            classification=classification,
            verbose=verbose,
        )

        result["classification"] = classification
        return result


def main():
    parser = argparse.ArgumentParser(
        description="Analyseur de dossiers CEE - chargement sélectif des règles"
    )
    parser.add_argument("input", help="Chemin vers le ZIP ou dossier de PDFs")
    parser.add_argument(
        "--rules",
        default="./rules_data",
        help="Dossier contenant les règles (défaut: ./rules_data)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Fichier JSON de sortie (défaut: affichage console)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Mode verbeux")

    args = parser.parse_args()

    try:
        result = process_dossier(args.input, args.rules, verbose=args.verbose)

        output = json.dumps(result, ensure_ascii=False, indent=2)

        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
            print(f"Résultat écrit dans : {args.output}")
        else:
            print("\n" + "=" * 60)
            print("RÉSULTAT D'ANALYSE")
            print("=" * 60)
            print(f"Fiche applicable : {result['classification']['fiche']}")
            print(f"Statut           : {result.get('statut', 'N/A')}")
            print(f"Tokens utilisés  : {result.get('tokens_used', 'N/A')}")
            print("\n--- Analyse détaillée ---")
            print(result.get("analyse", ""))

    except Exception as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
