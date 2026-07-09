"""
Copie les fichiers de règles (CSV Fiches + Grimoires) dans rules_data/.
Les Markdown de règles Doc sont déjà fournis dans rules_data/ du package.
"""

import shutil
import argparse
from pathlib import Path


EXPECTED_FILES = [
    "Fiche__Récapitulatif_des_fiches_BAR.csv",
    "Fiche__Récapitulatif_des_fiches_BAT.csv",
    "Fiche__Récapitulatif_des_COUP_DE_POUCE.csv",
]


def setup(source_dir: str, dest_dir: str = "./rules_data"):
    source = Path(source_dir)
    dest = Path(dest_dir)
    dest.mkdir(exist_ok=True)

    copied, missing = 0, []
    for filename in EXPECTED_FILES:
        src = source / filename
        if src.exists():
            shutil.copy2(src, dest / filename)
            copied += 1
        else:
            missing.append(filename)

    # Copie aussi tout GRIMOIRE__*.md trouvé à la source (optionnel)
    grimoires = list(source.glob("GRIMOIRE__*.md"))
    for g in grimoires:
        shutil.copy2(g, dest / g.name)

    print(f"\n✓ {copied} fichier(s) CSV copié(s) dans {dest}/")
    if grimoires:
        print(f"✓ {len(grimoires)} grimoire(s) Markdown copié(s)")
    if missing:
        print(f"\n⚠ {len(missing)} fichier(s) non trouvé(s) :")
        for f in missing:
            print(f"   • {f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--dest", default="./rules_data")
    args = parser.parse_args()
    setup(args.source, args.dest)
