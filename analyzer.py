"""
CEE Dossier Analyzer — CLI
"""

import os
import sys
import json
import tempfile
import argparse
from pathlib import Path

from utils.extractor import extract_zip, extract_text_from_pdf, is_scanned_pdf, ocr_pdf_smart
from utils.classifier import classify_dossier
from utils.rule_loader import RuleLoader
from utils.claude_client import analyze_with_claude, dry_run as run_dry_run


def process_dossier(input_path: str, rules_dir: str, verbose: bool = False, dry_run: bool = False, fiche_override: str = None, use_correspondance_table: bool = True) -> dict:
    input_path = Path(input_path)
    rules_dir = Path(rules_dir)

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

        if verbose:
            print(f"[2/4] Lecture de {len(pdf_files)} PDF(s)...")

        docs = {}
        for pdf_path in pdf_files:
            pdf_path = Path(pdf_path)
            name = pdf_path.stem.lower()

            if is_scanned_pdf(pdf_path):
                text = ocr_pdf_smart(pdf_path)
                docs[name] = {"text": text, "scanned": True, "path": str(pdf_path)}
            else:
                text = extract_text_from_pdf(pdf_path)
                docs[name] = {"text": text, "scanned": False, "path": str(pdf_path)}

            if verbose:
                preview = docs[name]["text"][:80].replace("\n", " ")
                scanned_tag = " [SCANNÉ]" if docs[name]["scanned"] else ""
                print(f"   • {pdf_path.name}{scanned_tag}: {preview}...")

        if verbose:
            print("[3/4] Classification du dossier...")

        loader = RuleLoader(rules_dir)
        correspondance_table = loader.get_fiche_correspondance_table()

        if fiche_override:
            classification = classify_dossier(
                docs, correspondance_table=correspondance_table,
                use_correspondance_table=use_correspondance_table,
            )
            classification["fiche"] = fiche_override
            classification["secteur"] = "BAT" if fiche_override.upper().startswith("BAT") else "BAR"
            classification["confiance"] = "haute"
            classification["raisonnement"] = "Fiche indiquée manuellement par l'utilisateur"
            if verbose:
                print(f"   → Fiche imposée manuellement : {fiche_override}")
        else:
            classification = classify_dossier(
                docs, correspondance_table=correspondance_table,
                use_correspondance_table=use_correspondance_table,
            )
            if verbose:
                print(f"   → Fiche détectée : {classification['fiche']}")
                print(f"   → Confiance      : {classification.get('confiance', '?')}")
                if classification['fiche'] == "INCONNUE":
                    print("   ⚠️  ALERTE : aucune fiche identifiée — vérifier le VISA/document "
                          "listant la fiche, ou indiquer la fiche manuellement (--fiche).")

        core_rules = loader.get_core_rules_text()
        variable_rules = loader.get_variable_rules_text(classification)
        if verbose:
            print(f"   → Socle : ~{len(core_rules)//4:,} tk | Variable : ~{len(variable_rules)//4:,} tk")

        if dry_run:
            if verbose:
                print("[4/4] Mode test — assemblage du prompt (aucun appel API)...")
            result = run_dry_run(docs, core_rules, variable_rules, classification)
        else:
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
    parser = argparse.ArgumentParser(description="Analyseur de dossiers CEE")
    parser.add_argument("input", help="Chemin vers le ZIP ou dossier de PDFs")
    parser.add_argument("--rules", default="./rules_data")
    parser.add_argument("--output", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Assemble le prompt sans appeler l'API (gratuit)")
    parser.add_argument("--fiche", default=None, help="Impose la fiche BAR/BAT (ex: BAR-EN-105), contourne la classification")
    parser.add_argument("--no-table", action="store_true", help="Désactive la table de correspondance fiche<->travaux en classification IA (test A/B)")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    try:
        result = process_dossier(args.input, args.rules, verbose=args.verbose, dry_run=args.dry_run, fiche_override=args.fiche, use_correspondance_table=not args.no_table)
        output = json.dumps(result, ensure_ascii=False, indent=2)

        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
            print(f"Résultat écrit dans : {args.output}")
        else:
            print("\n" + "=" * 60)
            if args.dry_run:
                print("MODE TEST — AUCUN APPEL API")
                print("=" * 60)
                tk = result["tokens_estimation"]
                print(f"Fiche détectée   : {result['classification']['fiche']}")
                print(f"Tokens estimés   : {tk['total_input']:,} input + {tk['output_estime']:,} output")
                print(f"Coût si réel     : ~{result['cout_estime_eur']['premier_appel']:.4f} € (1er appel)")
                print(f"                   ~{result['cout_estime_eur']['appels_suivants_avec_cache']:.4f} € (avec cache)")
            else:
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
