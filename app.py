"""
Application Streamlit — Analyseur de dossiers CEE
Dépose un ZIP, obtiens l'analyse de conformité en quelques secondes.

Sécurité : la clé API est lue en priorité depuis st.secrets (Streamlit Cloud),
avec repli sur la variable d'environnement, puis sur une saisie manuelle
(déconseillée en partage — visible seulement par la session de l'utilisateur,
mais à éviter si l'app est publique).
"""

import os
import json
import tempfile
import time
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="Analyseur CEE",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.statut-valide    { background:#d1fae5; color:#065f46; padding:10px 16px;
                    border-radius:8px; font-weight:600; font-size:1.1rem; }
.statut-invalide  { background:#fee2e2; color:#991b1b; padding:10px 16px;
                    border-radius:8px; font-weight:600; font-size:1.1rem; }
.statut-incomplet { background:#fef3c7; color:#92400e; padding:10px 16px;
                    border-radius:8px; font-weight:600; font-size:1.1rem; }
.statut-inconnu   { background:#f3f4f6; color:#374151; padding:10px 16px;
                    border-radius:8px; font-weight:600; font-size:1.1rem; }
.info-pill { background:#eff6ff; color:#1e40af; padding:4px 10px;
             border-radius:20px; font-size:0.85rem; display:inline-block;
             margin:2px; }
.cost-box  { background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px;
             padding:12px 16px; font-size:0.85rem; color:#64748b; }
.dryrun-box { background:#f5f3ff; border:1px solid #ddd6fe; border-radius:8px;
              padding:12px 16px; font-size:0.85rem; color:#5b21b6; }
</style>
""", unsafe_allow_html=True)


def get_api_key() -> str:
    """
    Récupère la clé API par ordre de priorité :
    1. st.secrets (Streamlit Cloud — recommandé en production)
    2. Variable d'environnement (usage local/serveur interne)
    3. Saisie manuelle (tests ponctuels uniquement)
    """
    try:
        if "ANTHROPIC_API_KEY" in st.secrets:
            return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY", "")


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚡ Analyseur CEE")
    st.caption("Vérification automatique de conformité des dossiers CEE")

    st.divider()
    st.subheader("⚙️ Configuration")

    api_key_found = get_api_key()
    api_key_source = None
    if api_key_found:
        try:
            if "ANTHROPIC_API_KEY" in st.secrets:
                api_key_source = "secrets"
        except Exception:
            api_key_source = "env"
        st.success(
            f"Clé API détectée ({'st.secrets' if api_key_source == 'secrets' else 'variable environnement'})",
            icon="✅",
        )
        api_key = api_key_found
    else:
        st.warning("Aucune clé API configurée", icon="⚠️")
        api_key = st.text_input(
            "Clé API Anthropic (test ponctuel uniquement)",
            type="password",
            placeholder="sk-ant-...",
            help="⚠️ Déconseillé si l'app est partagée — préférer st.secrets côté Streamlit Cloud.",
        )

    st.divider()

    rules_dir = st.text_input("Dossier des règles", value="./rules_data")
    rules_path = Path(rules_dir)
    if rules_path.exists():
        n_files = len(list(rules_path.iterdir()))
        st.caption(f"✅ {n_files} fichier(s) trouvé(s)")
    else:
        st.warning("Dossier introuvable — vérifier le chemin")

    st.divider()

    dry_run_mode = st.toggle(
        "🧪 Mode test (dry-run)",
        value=False,
        help="Assemble le prompt complet SANS appeler l'API — gratuit. "
             "Permet de vérifier l'extraction, la classification et le "
             "chargement des règles avant de payer un vrai appel.",
    )
    if dry_run_mode:
        st.caption("Aucun appel API ne sera facturé dans ce mode.")

    st.divider()
    st.caption("v1.1 · Claude Sonnet 4.6 + Haiku 4.5")
    st.caption("~0,06 € par analyse réelle · 0 € en mode test")


# ── Corps principal ───────────────────────────────────────────────────────────
st.title("⚡ Analyse de dossier CEE")
st.caption("Déposez le ZIP du dossier ODICEE pour vérifier sa conformité réglementaire.")

col_up, col_fiche = st.columns([2, 1])
with col_up:
    uploaded = st.file_uploader("Dossier CEE (ZIP)", type=["zip"])
with col_fiche:
    fiche_mode = st.radio(
        "Détection de la fiche",
        ["Automatique (IA)", "Manuelle"],
        help="En automatique, une IA identifie la fiche à partir de la nature des "
             "travaux décrits (matériaux, équipements) si aucun code n'est écrit "
             "explicitement. En manuel, vous imposez directement le code — utile "
             "si vous connaissez déjà le dossier ou si la détection automatique "
             "hésite.",
    )
    fiche_manuelle = None
    if fiche_mode == "Manuelle":
        fiche_manuelle_raw = st.text_input(
            "Code(s) fiche",
            placeholder="ex: BAR-EN-105 ou BAR-TH-106,BAR-TH-127 pour un dossier multi-fiches",
            help="Un code, ou plusieurs séparés par des virgules si le dossier couvre "
                 "plusieurs types de travaux (ex: un lot chauffage+ventilation).",
        ).strip().upper()
        fiche_manuelle = (
            [f.strip() for f in fiche_manuelle_raw.split(",") if f.strip()]
            if fiche_manuelle_raw else None
        )

if uploaded:
    st.divider()
    col_info, col_btn = st.columns([3, 1])
    with col_info:
        st.markdown(f"**Fichier :** `{uploaded.name}` · {uploaded.size / 1024:.0f} Ko")
    with col_btn:
        label = "🧪 Tester (gratuit)" if dry_run_mode else "🔍 Lancer l'analyse"
        run = st.button(label, type="primary", use_container_width=True)

    if run:
        if not dry_run_mode and not api_key:
            st.error("Clé API Anthropic manquante — renseignez-la dans la barre latérale, "
                      "ou activez le mode test pour vérifier le pipeline sans clé.")
            st.stop()
        if not rules_path.exists():
            st.error(f"Dossier de règles introuvable : `{rules_dir}`")
            st.stop()

        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key

        from utils.extractor import extract_zip, extract_document
        from utils.classifier import classify_dossier, classify_dossier_regex
        from utils.rule_loader import RuleLoader
        from utils.claude_client import analyze_with_claude, dry_run as run_dry_run

        t0 = time.time()

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / uploaded.name
            zip_path.write_bytes(uploaded.getvalue())

            with st.status("📂 Extraction des PDFs…", expanded=True) as status:
                pdf_files = extract_zip(zip_path, tmpdir)
                if not pdf_files:
                    st.error("Aucun PDF trouvé dans le ZIP.")
                    st.stop()
                st.write(f"→ {len(pdf_files)} PDF(s) trouvé(s)")

                status.update(label="📄 Lecture des documents…")
                docs = {}
                for pdf_path in pdf_files:
                    pdf_path = Path(pdf_path)
                    name = pdf_path.stem.lower()
                    doc = extract_document(pdf_path)
                    docs[name] = doc
                    if doc["scanned"]:
                        st.write(f"→ `{pdf_path.name}` — scanné, {doc.get('pages_total', '?')} page(s), OCR intelligent (début + fin)")
                    else:
                        st.write(f"→ `{pdf_path.name}` — texte extrait")
                    if doc.get("couverture"):
                        st.warning(f"`{pdf_path.name}` — couverture partielle : {doc['couverture']}. "
                                   "Une absence d'élément dans ce document sera signalée comme "
                                   "'possiblement hors extrait' (vérification manuelle recommandée), "
                                   "pas comme une non-conformité définitive.", icon="⚠️")

                status.update(label="📚 Chargement de la nomenclature…")
                loader = RuleLoader(rules_path)
                correspondance_table = loader.get_fiche_correspondance_table()

                status.update(label="🔎 Classification du dossier…")
                if fiche_manuelle:
                    # Contournement total : on ne fait tourner le classifier que pour
                    # récupérer secteur/type d'engagement, la ou les fiches sont imposées.
                    if dry_run_mode and not api_key:
                        classification = classify_dossier_regex(docs)
                    else:
                        classification = classify_dossier(docs, correspondance_table=correspondance_table)
                    classification["fiches"] = fiche_manuelle
                    classification["secteur"] = "BAT" if fiche_manuelle[0].startswith("BAT") else "BAR"
                    classification["confiance"] = "haute"
                    classification["raisonnement"] = "Fiche(s) indiquée(s) manuellement par l'utilisateur"
                    st.write(f"→ Fiche(s) imposée(s) manuellement : **{', '.join(fiche_manuelle)}**")
                elif dry_run_mode and not api_key:
                    classification = classify_dossier_regex(docs)
                    fiches_d = classification.get("fiches", ["INCONNUE"])
                    st.write(f"→ Fiche(s) détectée(s) (regex, sans IA) : **{', '.join(fiches_d)}** "
                             f"(confiance : {classification.get('confiance', '?')})")
                else:
                    classification = classify_dossier(docs, correspondance_table=correspondance_table)
                    fiches_d = classification.get("fiches", ["INCONNUE"])
                    st.write(f"→ Fiche(s) détectée(s) : **{', '.join(fiches_d)}** "
                             f"(confiance : {classification.get('confiance', '?')})")
                    if len(fiches_d) > 1:
                        st.info(f"ℹ️ Dossier multi-fiches détecté ({len(fiches_d)} fiches)", icon="ℹ️")
                    if classification.get("raisonnement"):
                        st.caption(f"_{classification['raisonnement']}_")

                if classification.get("fiches", ["INCONNUE"]) == ["INCONNUE"]:
                    st.warning(
                        "⚠️ Aucune fiche BAR/BAT identifiée. Vérifiez si le VISA ou un "
                        "document listant la fiche est bien inclus dans le ZIP, ou "
                        "utilisez le mode **Manuelle** ci-dessus pour l'indiquer vous-même.",
                        icon="⚠️",
                    )

                status.update(label="📚 Chargement des règles…")
                core_rules = loader.get_core_rules_text()
                variable_rules = loader.get_variable_rules_text(classification)
                st.write(f"→ Socle : ~{len(core_rules)//4:,} tk (caché) · "
                         f"Variable : ~{len(variable_rules)//4:,} tk")

                if dry_run_mode:
                    status.update(label="🧪 Assemblage du prompt (mode test)…")
                    result = run_dry_run(docs, core_rules, variable_rules, classification)
                    status.update(label="✅ Prompt assemblé (aucun appel API)", state="complete", expanded=False)
                else:
                    status.update(label="🤖 Analyse par Claude Sonnet 4.6…")
                    result = analyze_with_claude(
                        docs=docs,
                        core_rules_text=core_rules,
                        variable_rules_text=variable_rules,
                        classification=classification,
                    )
                    status.update(label="✅ Analyse terminée", state="complete", expanded=False)

        elapsed = time.time() - t0

        st.divider()

        # ══════════════════════════════════════════════════════════════
        # AFFICHAGE MODE DRY-RUN
        # ══════════════════════════════════════════════════════════════
        if dry_run_mode:
            st.markdown(
                '<div class="dryrun-box">🧪 <b>Mode test</b> — aucun appel API n\'a été '
                'effectué, aucun coût engagé.</div>',
                unsafe_allow_html=True,
            )
            st.divider()

            tk = result["tokens_estimation"]
            cout = result["cout_estime_eur"]

            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Tokens socle (cacheable)", f"{tk['core_socle']:,}")
            with col2:
                st.metric("Tokens variables", f"{tk['variable']:,}")
            with col3:
                st.metric("Total input estimé", f"{tk['total_input']:,}")

            st.markdown(
                f'<div class="cost-box">💶 Coût estimé si réel : '
                f'<b>~{cout["premier_appel"]:.4f} €</b> (1er appel) · '
                f'<b>~{cout["appels_suivants_avec_cache"]:.4f} €</b> (appels suivants, avec cache)'
                f'</div>',
                unsafe_allow_html=True,
            )

            st.divider()
            st.subheader("📋 Prompt qui serait envoyé à Claude")

            with st.expander("**Instructions système**", expanded=False):
                st.text(result["prompt_system"])
            with st.expander(f"**Socle de règles** (~{tk['core_socle']:,} tk, caché)", expanded=True):
                st.text(result["prompt_core"])
            with st.expander(f"**Bloc variable** (fiche + documents, ~{tk['variable']:,} tk)", expanded=True):
                st.text(result["prompt_variable"])

            st.info(
                "Vérifiez ici que les bons documents sont extraits, que la fiche détectée "
                "est cohérente, et que les règles chargées sont pertinentes — avant de "
                "désactiver le mode test pour lancer une vraie analyse.",
                icon="ℹ️",
            )

        # ══════════════════════════════════════════════════════════════
        # AFFICHAGE MODE RÉEL
        # ══════════════════════════════════════════════════════════════
        else:
            col1, col2, col3 = st.columns(3)

            with col1:
                statut = result.get("statut", "INDÉTERMINÉ")
                css_class = {
                    "VALIDE": "statut-valide",
                    "NON VALIDE": "statut-invalide",
                    "INCOMPLET": "statut-incomplet",
                }.get(statut, "statut-inconnu")
                icons = {"VALIDE": "✅", "NON VALIDE": "❌", "INCOMPLET": "⚠️"}
                icon = icons.get(statut, "❓")
                st.markdown(f'<div class="{css_class}">{icon} {statut}</div>', unsafe_allow_html=True)

            with col2:
                fiche = ", ".join(classification.get("fiches", [classification.get("fiche", "?")]))
                confiance = classification.get("confiance", "?")
                st.markdown(
                    f'<span class="info-pill">📋 {fiche}</span>'
                    f'<span class="info-pill">🎯 Confiance : {confiance}</span>',
                    unsafe_allow_html=True,
                )
                raisonnement = classification.get("raisonnement", "")
                if raisonnement:
                    st.caption(f"_{raisonnement}_")

            with col3:
                tokens = result.get("tokens_used", {})
                total_tok = tokens.get("total", 0)
                cost_eur = (
                    tokens.get("input", 0) * 3 / 1_000_000
                    + tokens.get("output", 0) * 15 / 1_000_000
                ) * 0.92
                st.markdown(
                    f'<div class="cost-box">🪙 <b>{total_tok:,}</b> tokens utilisés<br>'
                    f'💶 <b>~{cost_eur:.4f} €</b> · ⏱ {elapsed:.1f}s</div>',
                    unsafe_allow_html=True,
                )

            ctx_pills = []
            if classification.get("coup_de_pouce"):
                ctx_pills.append("🎁 Coup de pouce")
            if classification.get("sous_traitance"):
                ctx_pills.append("🔧 Sous-traitance")
            secteur = classification.get("secteur", "BAR")
            ctx_pills.append(f"🏠 {'Résidentiel (BAR)' if secteur == 'BAR' else 'Tertiaire (BAT)'}")
            type_eng = classification.get("type_engagement", "inconnu").replace("_", " ").title()
            ctx_pills.append(f"📝 {type_eng}")
            st.markdown(" ".join(f'<span class="info-pill">{p}</span>' for p in ctx_pills), unsafe_allow_html=True)

            st.divider()

            audit = result.get("audit", {})

            if audit:
                # --- Recoupement date d'engagement : classifier vs audit ---
                # Si la date confirmée par Claude pendant l'audit diffère de
                # celle du classifier (qui a servi à filtrer la VERSION de
                # fiche chargée), les seuils vérifiés proviennent peut-être de
                # la mauvaise version -> alerte bloquante à vérifier.
                date_classif = classification.get("date_engagement")
                date_audit = audit.get("date_engagement_confirmee")
                if date_audit and date_classif and date_audit != date_classif:
                    st.error(
                        f"🚨 Divergence de date d'engagement : le classifier a détecté "
                        f"**{date_classif}** (date utilisée pour sélectionner la version de "
                        f"fiche et ses seuils), mais l'audit a confirmé **{date_audit}** dans "
                        f"les documents. La version de fiche vérifiée est peut-être la "
                        f"mauvaise — relancer l'analyse en imposant la fiche/date, ou "
                        f"vérifier manuellement.",
                        icon="🚨",
                    )
                elif date_audit and not date_classif:
                    st.warning(
                        f"ℹ️ L'audit a identifié la date d'engagement **{date_audit}** alors "
                        f"que le classifier n'en avait trouvé aucune (toutes les versions de "
                        f"fiche ont été envoyées) : vérifier dans le détail que la bonne "
                        f"version a été retenue (champ « version_applicable »).",
                        icon="⚠️",
                    )

                if result.get("reponse_tronquee"):
                    st.error(
                        "🚨 La réponse de l'API a été tronquée (limite de tokens atteinte "
                        "malgré une relance) — le résultat ci-dessous est incomplet, "
                        "relancer l'analyse.",
                        icon="🚨",
                    )

                # --- Synthèse narrative (lecture humaine rapide) ---
                if audit.get("synthese_narrative"):
                    st.info(audit["synthese_narrative"], icon="📝")

                # --- Anomalies signalées (non bloquantes) ---
                anomalies = audit.get("anomalies", [])
                if anomalies:
                    with st.expander(f"⚠️ Anomalies signalées ({len(anomalies)})", expanded=True):
                        for a in anomalies:
                            st.markdown(f"- {a}")

                st.subheader("🔧 Éligibilité technique par fiche")
                for fiche_obj in audit.get("fiches", []):
                    v_tech = fiche_obj.get("verdict_technique", "?")
                    icon = {"VALIDE": "✅", "NON VALIDE": "❌", "INCOMPLET": "⚠️"}.get(v_tech, "❓")
                    code = fiche_obj.get("code", "?")
                    version = fiche_obj.get("version_applicable", "")
                    with st.expander(f"{icon} **{code}** ({version}) — {v_tech}", expanded=(v_tech != "VALIDE")):
                        elements = fiche_obj.get("elements_techniques", [])
                        if elements:
                            rows = []
                            for el in elements:
                                conforme = el.get("conforme")
                                conforme_str = "✅" if conforme is True else ("❌" if conforme is False else "—")
                                if el.get("present"):
                                    present_str = "✅"
                                elif el.get("hors_extrait_possible"):
                                    present_str = "❓ hors extrait ?"
                                else:
                                    present_str = "❌"
                                verif = el.get("citation_verifiee")
                                verif_str = "✅" if verif is True else ("⚠️ NON TROUVÉE" if verif is False else "—")
                                rows.append({
                                    "Élément": el.get("champ", "?"),
                                    "Présent": present_str,
                                    "Valeur trouvée": el.get("valeur_trouvee") or "—",
                                    "Citation exacte (à vérifier)": el.get("citation_verbatim") or "—",
                                    "Citation vérifiée": verif_str,
                                    "Conforme": conforme_str,
                                    "Source": el.get("source") or "—",
                                })
                            st.dataframe(rows, use_container_width=True, hide_index=True)
                            n_hors_extrait = sum(1 for el in elements if el.get("hors_extrait_possible"))
                            if n_hors_extrait:
                                st.warning(
                                    f"❓ {n_hors_extrait} élément(s) introuvable(s) dans un document à "
                                    f"couverture PARTIELLE (pages non OCRisées ou texte tronqué) : leur "
                                    f"absence n'est pas certaine — vérifier le document original avant "
                                    f"de conclure à une non-conformité.",
                                    icon="⚠️",
                                )
                            n_non_verifiees = sum(1 for el in elements if el.get("citation_verifiee") is False)
                            if n_non_verifiees:
                                st.error(
                                    f"⚠️ {n_non_verifiees} citation(s) introuvable(s) telle(s) quelle(s) "
                                    f"dans les documents fournis — risque de citation fabriquée, à vérifier "
                                    f"manuellement en priorité.",
                                    icon="🚨",
                                )
                            st.caption(
                                "La colonne « Citation exacte » reproduit mot pour mot la ligne "
                                "source — vérifiez qu'elle concerne bien le composant attendu "
                                "(une facture multi-lignes peut mentionner plusieurs marques/"
                                "références pour des éléments différents). « Citation vérifiée » "
                                "confirme seulement que le texte existe dans les documents, pas "
                                "qu'il est correctement attribué au bon élément."
                            )
                        else:
                            st.caption("Aucun élément technique détaillé retourné.")

                st.subheader("📋 Validation globale du dossier")
                axes = audit.get("axes", {})
                axe_labels = {
                    "logique_globale": "1. Logique globale",
                    "engagement": "2. Validation engagement",
                    "realisation_documentaire": "3. Validation réalisation (documentaire)",
                    "rge": "4. Validation RGE",
                    "ah": "5. Validation AH",
                    "coherence": "6. Cohérence engagement ↔ réalisation",
                    "documents_annexes": "7. Documents annexes",
                }
                for key, label in axe_labels.items():
                    axe = axes.get(key)
                    if not axe:
                        continue
                    v = axe.get("verdict", "?")
                    icon = {"VALIDE": "✅", "NON VALIDE": "❌", "INCOMPLET": "⚠️"}.get(v, "❓")
                    with st.expander(f"{icon} **{label}** — {v}", expanded=(v != "VALIDE")):
                        controles = axe.get("controles", [])
                        if controles:
                            for c in controles:
                                mark = "✅" if c.get("verdict") else "❌"
                                line = f"{mark} {c.get('item', '?')}"
                                if c.get("details"):
                                    line += f" — _{c['details']}_"
                                st.markdown(line)
                                if c.get("source"):
                                    st.caption(f"Source : {c['source']}")
                        else:
                            st.caption("Aucun contrôle détaillé retourné pour cet axe.")
            else:
                st.warning("Aucune donnée structurée retournée par l'API — réponse inattendue.", icon="⚠️")
                st.text(result.get("analyse", "(vide)"))

            st.divider()
            export_data = {
                "fichier": uploaded.name,
                "classification": classification,
                "statut": statut,
                "tokens_used": tokens,
                "cout_eur": round(cost_eur, 4),
                "temps_secondes": round(elapsed, 1),
                "audit": audit,
            }
            st.download_button(
                label="⬇️ Télécharger le résultat (JSON)",
                data=json.dumps(export_data, ensure_ascii=False, indent=2),
                file_name=f"analyse_{uploaded.name.replace('.zip', '')}.json",
                mime="application/json",
            )

else:
    st.info(
        "👆 Déposez le ZIP du dossier ODICEE ci-dessus pour démarrer l'analyse.\n\n"
        "💡 Activez le **mode test** dans la barre latérale pour vérifier gratuitement "
        "l'extraction et la classification avant de lancer une vraie analyse payante.",
        icon="ℹ️",
    )

    with st.expander("Comment configurer l'application ?"):
        st.markdown("""
**1. Clé API Anthropic — configuration recommandée**

Sur Streamlit Community Cloud, allez dans les paramètres de l'app → onglet **Secrets** et ajoutez :
```toml
ANTHROPIC_API_KEY = "sk-ant-..."
```
La clé n'apparaît alors jamais dans le code ni dans le repo Git.

En local, utilisez plutôt la variable d'environnement :
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

**2. Dossier des règles**
```bash
python setup_rules.py --source /chemin/vers/grimoires --dest ./rules_data
```

**3. Tester sans payer**
Activez le mode test (dry-run) dans la barre latérale — le pipeline complet
(extraction, OCR, classification, chargement des règles) s'exécute normalement,
seul l'appel final à Claude est remplacé par un aperçu du prompt qui serait envoyé.

**4. Lancer l'app**
```bash
pip install -r requirements.txt
streamlit run app.py
```
        """)
