"""
Script de configuration : copie les fichiers de règles du projet
dans le dossier rules_data/ attendu par l'analyseur.

Usage : python setup_rules.py --source /chemin/vers/grimoires
"""

import shutil
import argparse
from pathlib import Path


EXPECTED_FILES = [
    # CSV règles
    "Règles_validation.csv",
    "Doc__Récapitulatif_règles_d_engagement.csv",
    "Doc__Récapitulatif_règles_de_réalisation.csv",
    "Doc__Récapitulatif_règles_de_qualification_RGE.csv",
    "Doc__Récapitulatif_règles_des_attestations_sur_lhonneur.csv",
    "Doc__Récapitulatif_règles_des_autres_documents.csv",
    "Fiche__Récapitulatif_des_fiches_BAR.csv",
    "Fiche__Récapitulatif_des_fiches_BAT.csv",
    "Fiche__Récapitulatif_des_COUP_DE_POUCE.csv",
    # Grimoires PDF
    "GRIMOIRE__Généralités_sur_les_preuves.pdf",
    "GRIMOIRE__Preuves_dengagement.pdf",
    "GRIMOIRE__Preuves_de_réalisation.pdf",
    "GRIMOIRE__RGE.pdf",
    "GRIMOIRE__Attestation_sur_lhonneur.pdf",
    "GRIMOIRE__Ordre_de_service.pdf",
    "GRIMOIRE__Bon_de_commande.pdf",
    "GRIMOIRE__Acte_dengagement__DPGF.pdf",
    "GRIMOIRE__Devis.pdf",
    "GRIMOIRE__Facture.pdf",
    "GRIMOIRE__Décompte_Général_et_Définitif.pdf",
    "GRIMOIRE__Soustraitance.pdf",
    "GRIMOIRE__Surface_disolation.pdf",
    "GRIMOIRE__BARTH107_et_107SE.pdf",
    "GRIMOIRE__BARTH127.pdf",
    "GRIMOIRE__BARTH130.pdf",
    "GRIMOIRE__BARTH137_RCU.pdf",
    "GRIMOIRE__BARTH159.pdf",
    "GRIMOIRE__BARTH169.pdf",
    "GRIMOIRE__BARTH174.pdf",
    "GRIMOIRE__BARTH175.pdf",
    "GRIMOIRE__BARTH176_ELAX.pdf",
    "GRIMOIRE__BARTH177_Rénovation_globale.pdf",
    "GRIMOIRE__BAREN104.pdf",
    "GRIMOIRE__Notes_de_dimensionnement_PAC.pdf",
    "GRIMOIRE__Bâtiments.pdf",
    "GRIMOIRE__Tertiaires.pdf",
    "GRIMOIRE__Copropriété.pdf",
    "GRIMOIRE__Coup_de_pouce_chauffage_individuel.pdf",
    "GRIMOIRE__Coup_de_pouce_chauffage_collectif_et_tertiaire.pdf",
    "GRIMOIRE__Coup_de_pouce_rénovation_globale.pdf",
    "GRIMOIRE___Coup_de_pouce_Chauffage_batiment_tertaire_et_collectif_.pdf",
    "GRIMOIRE__Maîtrise_dœuvre.pdf",
    "GRIMOIRE__Domaines_RGE.pdf",
    "GRIMOIRE__Signatures_et_eSignatures.pdf",
    "GRIMOIRE__Annexes.pdf",
    "GRIMOIRE__Doublons.pdf",
    "GRIMOIRE___Fonds_chaleur.pdf",
]


def setup(source_dir: str, dest_dir: str = "./rules_data"):
    source = Path(source_dir)
    dest = Path(dest_dir)
    dest.mkdir(exist_ok=True)

    copied = 0
    missing = []

    for filename in EXPECTED_FILES:
        src = source / filename
        if src.exists():
            shutil.copy2(src, dest / filename)
            copied += 1
        else:
            missing.append(filename)

    print(f"\n✓ {copied} fichier(s) copié(s) dans {dest}/")
    if missing:
        print(f"\n⚠ {len(missing)} fichier(s) non trouvé(s) dans la source :")
        for f in missing:
            print(f"   • {f}")
    else:
        print("✓ Tous les fichiers attendus sont présents.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Configure le dossier rules_data depuis le répertoire de grimoires"
    )
    parser.add_argument("--source", required=True, help="Dossier source contenant les grimoires")
    parser.add_argument("--dest", default="./rules_data", help="Dossier de destination")
    args = parser.parse_args()
    setup(args.source, args.dest)
