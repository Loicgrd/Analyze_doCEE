"""
Évaluation de la fiabilité de l'analyseur CEE.

Usage :
    1. Placer les ZIPs de test dans eval/dossiers/
    2. Remplir eval/expected_results.json avec les verdicts attendus
    3. Lancer : python eval/run_eval.py --rules ./rules_data

Coût typique : ~0,05 € par dossier évalué.
"""

import os
import sys
import json
import argparse
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analyzer import process_dossier  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
DOSSIERS_DIR = EVAL_DIR / "dossiers"
EXPECTED_FILE = EVAL_DIR / "expected_results.json"
REPORT_FILE = EVAL_DIR / "rapport_eval.json"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _point_found(point: str, analyse: str) -> bool:
    analyse_n = _norm(analyse)
    words = [w for w in _norm(point).split() if len(w) > 4]
    if not words:
        return _norm(point) in analyse_n
    hits = sum(1 for w in words if w in analyse_n)
    return hits >= max(1, int(len(words) * 0.6))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules", default="./rules_data")
    parser.add_argument("--only", default=None)
    parser.add_argument("--no-table", action="store_true",
                         help="Désactive la table de correspondance fiche<->travaux (test A/B)")
    parser.add_argument("--compare", action="store_true",
                         help="Lance la campagne AVEC puis SANS la table, affiche un comparatif côte à côte")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY manquante.", file=sys.stderr)
        sys.exit(1)

    expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))["dossiers"]
    if args.only:
        expected = [e for e in expected if e["fichier"] == args.only]

    results = []
    total_cost_eur = 0.0

    for entry in expected:
        zip_path = DOSSIERS_DIR / entry["fichier"]
        att = entry["attendu"]

        if "A_COMPLETER" in str(att.get("statut", "")):
            print(f"⏭  {entry['fichier']} : verdict attendu non renseigné, ignoré.")
            continue
        if not zip_path.exists():
            print(f"⏭  {entry['fichier']} : ZIP absent de eval/dossiers/, ignoré.")
            continue

        print(f"▶  Analyse de {entry['fichier']}...")
        res = process_dossier(str(zip_path), args.rules, use_correspondance_table=not args.no_table)

        fiche_ok = res["classification"]["fiche"] == att["fiche"]
        statut_ok = res["statut"] == att["statut"]

        points = att.get("points_bloquants_attendus", [])
        points_status = {p: _point_found(p, res["analyse"]) for p in points}
        points_ok = all(points_status.values()) if points else True

        tk = res.get("tokens_used", {})
        cost = (tk.get("input", 0) * 3 + tk.get("output", 0) * 15) / 1_000_000 * 0.92
        total_cost_eur += cost

        results.append({
            "fichier": entry["fichier"],
            "fiche": {"attendu": att["fiche"], "obtenu": res["classification"]["fiche"], "ok": fiche_ok},
            "statut": {"attendu": att["statut"], "obtenu": res["statut"], "ok": statut_ok},
            "points_bloquants": points_status,
            "points_ok": points_ok,
            "global_ok": fiche_ok and statut_ok and points_ok,
            "cout_eur": round(cost, 4),
            "analyse": res["analyse"],
        })

    if not results:
        print("\nAucun dossier évaluable. Remplir expected_results.json et eval/dossiers/.")
        return

    n = len(results)
    ok = sum(1 for r in results if r["global_ok"])
    print("\n" + "=" * 62)
    print(f"SYNTHÈSE ÉVALUATION — {ok}/{n} dossiers corrects ({ok/n*100:.0f}%)")
    print("=" * 62)
    print(f"{'Dossier':<16}{'Fiche':<8}{'Statut':<8}{'Points':<8}{'Global'}")
    for r in results:
        def mark(b): return "✅" if b else "❌"
        print(f"{r['fichier']:<16}{mark(r['fiche']['ok']):<8}{mark(r['statut']['ok']):<8}"
              f"{mark(r['points_ok']):<8}{mark(r['global_ok'])}")
        if not r["fiche"]["ok"]:
            print(f"   fiche : attendu {r['fiche']['attendu']}, obtenu {r['fiche']['obtenu']}")
        if not r["statut"]["ok"]:
            print(f"   statut : attendu {r['statut']['attendu']}, obtenu {r['statut']['obtenu']}")
        for p, found in r["points_bloquants"].items():
            if not found:
                print(f"   point manqué : {p}")
    print(f"\nCoût de la campagne : ~{total_cost_eur:.2f} €")

    REPORT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rapport détaillé : {REPORT_FILE}")
    return results


def compare_with_without_table(rules_dir: str, only: str = None):
    """
    Lance la campagne d'éval deux fois (avec puis sans la table de
    correspondance fiche<->travaux) et affiche un comparatif côte à côte.
    Permet de vérifier empiriquement si la table change les résultats sur
    votre corpus, plutôt que de le supposer.
    """
    print("=" * 62)
    print("CAMPAGNE A/B — AVEC table de correspondance")
    print("=" * 62)
    results_with = _run_all(rules_dir, only, use_table=True)

    print("\n" + "=" * 62)
    print("CAMPAGNE A/B — SANS table de correspondance")
    print("=" * 62)
    results_without = _run_all(rules_dir, only, use_table=False)

    print("\n" + "=" * 62)
    print("COMPARATIF AVEC vs SANS table")
    print("=" * 62)
    print(f"{'Dossier':<16}{'Avec table':<28}{'Sans table':<28}{'Écart ?'}")
    by_fichier_with = {r["fichier"]: r for r in results_with}
    by_fichier_without = {r["fichier"]: r for r in results_without}
    n_diff = 0
    for fichier in by_fichier_with:
        rw = by_fichier_with[fichier]
        rwo = by_fichier_without.get(fichier)
        if not rwo:
            continue
        fw = rw["fiche"]["obtenu"]
        fwo = rwo["fiche"]["obtenu"]
        diff = "⚠️ OUI" if fw != fwo else "—"
        if fw != fwo:
            n_diff += 1
        print(f"{fichier:<16}{fw:<28}{fwo:<28}{diff}")

    print(f"\n{n_diff}/{len(by_fichier_with)} dossier(s) avec un résultat différent selon la table.")
    if n_diff == 0:
        print("→ Sur ce corpus, la table de correspondance ne change aucun résultat : "
              "envisageable de la retirer pour économiser ~1500 tokens/dossier en mode IA.")
    else:
        print("→ La table change le résultat sur au moins un dossier : recommandé de la garder.")


def _run_all(rules_dir: str, only: str, use_table: bool):
    expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))["dossiers"]
    if only:
        expected = [e for e in expected if e["fichier"] == only]

    results = []
    for entry in expected:
        zip_path = DOSSIERS_DIR / entry["fichier"]
        att = entry["attendu"]
        if "A_COMPLETER" in str(att.get("statut", "")) or not zip_path.exists():
            continue
        print(f"▶  {entry['fichier']}...")
        res = process_dossier(str(zip_path), rules_dir, use_correspondance_table=use_table)
        results.append({
            "fichier": entry["fichier"],
            "fiche": {"attendu": att["fiche"], "obtenu": res["classification"]["fiche"]},
            "statut": {"attendu": att["statut"], "obtenu": res["statut"]},
        })
    return results


if __name__ == "__main__":
    import argparse as _ap
    _pre = _ap.ArgumentParser(add_help=False)
    _pre.add_argument("--compare", action="store_true")
    _pre.add_argument("--rules", default="./rules_data")
    _pre.add_argument("--only", default=None)
    _known, _ = _pre.parse_known_args()
    if _known.compare:
        compare_with_without_table(_known.rules, _known.only)
    else:
        main()
