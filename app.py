"""
Application Streamlit — Analyseur de dossiers CEE
Dépose un ZIP, obtiens l'analyse de conformité en quelques secondes.
"""

import os
import json
import tempfile
import time
from pathlib import Path

import streamlit as st

# ── Configuration de la page ──────────────────────────────────────────────────
st.set_page_config(
    page_title="Analyseur CEE",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS minimal ───────────────────────────────────────────────────────────────
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
</style>
""", unsafe_allow_html=True)


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/thumb/3/3b/Eo_circle_yellow_white_letter-e.svg/240px-Eo_circle_yellow_white_letter-e.svg.png", width=48)
    st.title("Analyseur CEE")
    st.caption("Vérification automatique de conformité des dossiers CEE")

    st.divider()
    st.subheader("⚙️ Configuration")

    # Clé API — priorité : variable d'env > saisie manuelle
    api_key_env = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key_env:
        st.success("Clé API détectée (variable d'env)", icon="✅")
        api_key = api_key_env
    else:
        api_key = st.text_input(
            "Clé API Anthropic",
            type="password",
            placeholder="sk-ant-...",
            help="Disponible sur console.anthropic.com",
        )

    st.divider()

    # Chemin vers les règles
    rules_dir = st.text_input(
        "Dossier des règles (grimoires)",
        value="./rules_data",
        help="Chemin vers le dossier contenant les CSV et PDFs de règles",
    )

    rules_path = Path(rules_dir)
    if rules_path.exists():
        n_files = len(list(rules_path.iterdir()))
        st.caption(f"✅ {n_files} fichier(s) trouvé(s)")
    else:
        st.warning("Dossier introuvable — vérifier le chemin")

    st.divider()
    st.caption("v1.0 · Claude Sonnet 4.6 + Haiku 4.5")
    st.caption("~0,06 € par analyse")


# ── Corps principal ───────────────────────────────────────────────────────────
st.title("⚡ Analyse de dossier CEE")
st.caption("Déposez le ZIP du dossier ODICEE pour vérifier sa conformité réglementaire.")

uploaded = st.file_uploader(
    "Dossier CEE (ZIP)",
    type=["zip"],
    help="Le ZIP doit contenir les PDFs : engagement, réalisation, RGE, et optionnellement l'AH",
)

if uploaded:
    st.divider()

    col_info, col_btn = st.columns([3, 1])
    with col_info:
        st.markdown(f"**Fichier :** `{uploaded.name}` · {uploaded.size / 1024:.0f} Ko")
    with col_btn:
        run = st.button("🔍 Lancer l'analyse", type="primary", use_container_width=True)

    if run:
        # Vérifications préalables
        if not api_key:
            st.error("Clé API Anthropic manquante — renseignez-la dans la barre latérale.")
            st.stop()
        if not rules_path.exists():
            st.error(f"Dossier de règles introuvable : `{rules_dir}`")
            st.stop()

        os.environ["ANTHROPIC_API_KEY"] = api_key

        # Import ici pour bénéficier de la clé positionnée
        from utils.extractor import extract_zip, extract_text_from_pdf, is_scanned_pdf, ocr_pdf_page
        from utils.classifier import classify_dossier
        from utils.rule_loader import RuleLoader
        from utils.claude_client import analyze_with_claude

        t0 = time.time()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Sauvegarder le ZIP uploadé
            zip_path = Path(tmpdir) / uploaded.name
            zip_path.write_bytes(uploaded.getvalue())

            # ── Étape 1 : Extraction ───────────────────────────────────────
            with st.status("📂 Extraction des PDFs…", expanded=True) as status:
                pdf_files = extract_zip(zip_path, tmpdir)
                if not pdf_files:
                    st.error("Aucun PDF trouvé dans le ZIP.")
                    st.stop()
                st.write(f"→ {len(pdf_files)} PDF(s) trouvé(s)")

                # ── Étape 2 : Lecture des textes ──────────────────────────
                status.update(label="📄 Lecture des documents…")
                docs = {}
                for pdf_path in pdf_files:
                    pdf_path = Path(pdf_path)
                    name = pdf_path.stem.lower()
                    scanned = is_scanned_pdf(pdf_path)
                    if scanned:
                        text = ocr_pdf_page(pdf_path, page=1)
                        st.write(f"→ `{pdf_path.name}` — scanné (OCR)")
                    else:
                        text = extract_text_from_pdf(pdf_path)
                        st.write(f"→ `{pdf_path.name}` — texte extrait")
                    docs[name] = {"text": text, "scanned": scanned, "path": str(pdf_path)}

                # ── Étape 3 : Classification ──────────────────────────────
                status.update(label="🔎 Classification du dossier…")
                classification = classify_dossier(docs)
                st.write(f"→ Fiche détectée : **{classification['fiche']}** "
                         f"(confiance : {classification.get('confiance', '?')})")

                # ── Étape 4 : Chargement règles ───────────────────────────
                status.update(label="📚 Chargement des règles…")
                loader = RuleLoader(rules_path)
                rules_bundle = loader.load_for_classification(classification)
                n_rules = len(rules_bundle)
                tokens_rules = sum(len(v) for v in rules_bundle.values()) // 4
                st.write(f"→ {n_rules} fichier(s) de règles chargés (~{tokens_rules:,} tokens)")

                # ── Étape 5 : Analyse Claude ──────────────────────────────
                status.update(label="🤖 Analyse par Claude Sonnet 4.6…")
                result = analyze_with_claude(
                    docs=docs,
                    rules_bundle=rules_bundle,
                    classification=classification,
                )
                status.update(label="✅ Analyse terminée", state="complete", expanded=False)

        elapsed = time.time() - t0

        # ── Affichage des résultats ────────────────────────────────────────
        st.divider()

        # En-tête résumé
        col1, col2, col3 = st.columns(3)

        with col1:
            statut = result.get("statut", "INDÉTERMINÉ")
            css_class = {
                "VALIDE":       "statut-valide",
                "NON VALIDE":   "statut-invalide",
                "INCOMPLET":    "statut-incomplet",
            }.get(statut, "statut-inconnu")
            icons = {"VALIDE": "✅", "NON VALIDE": "❌", "INCOMPLET": "⚠️"}
            icon = icons.get(statut, "❓")
            st.markdown(
                f'<div class="{css_class}">{icon} {statut}</div>',
                unsafe_allow_html=True,
            )

        with col2:
            fiche = classification.get("fiche", "?")
            confiance = classification.get("confiance", "?")
            raisonnement = classification.get("raisonnement", "")
            st.markdown(
                f'<span class="info-pill">📋 {fiche}</span>'
                f'<span class="info-pill">🎯 Confiance : {confiance}</span>',
                unsafe_allow_html=True,
            )
            if raisonnement:
                st.caption(f"_{raisonnement}_")

        with col3:
            tokens = result.get("tokens_used", {})
            total_tok = tokens.get("total", 0)
            cost_eur = (
                tokens.get("input", 0) * 3 / 1_000_000
                + tokens.get("output", 0) * 15 / 1_000_000
            ) * 0.92  # conversion USD → EUR approximative
            st.markdown(
                f'<div class="cost-box">'
                f'🪙 <b>{total_tok:,}</b> tokens utilisés<br>'
                f'💶 <b>~{cost_eur:.4f} €</b> · ⏱ {elapsed:.1f}s'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Contexte détecté
        ctx_pills = []
        if classification.get("coup_de_pouce"):
            ctx_pills.append("🎁 Coup de pouce")
        if classification.get("sous_traitance"):
            ctx_pills.append("🔧 Sous-traitance")
        secteur = classification.get("secteur", "BAR")
        ctx_pills.append(f"🏠 {'Résidentiel (BAR)' if secteur == 'BAR' else 'Tertiaire (BAT)'}")
        type_eng = classification.get("type_engagement", "inconnu").replace("_", " ").title()
        ctx_pills.append(f"📝 {type_eng}")

        st.markdown(
            " ".join(f'<span class="info-pill">{p}</span>' for p in ctx_pills),
            unsafe_allow_html=True,
        )

        st.divider()

        # Analyse complète
        st.subheader("📋 Analyse détaillée")
        analyse = result.get("analyse", "")

        # Affichage section par section si les titres ## sont présents
        if "##" in analyse:
            sections = analyse.split("\n## ")
            st.markdown(sections[0])  # intro éventuelle
            for section in sections[1:]:
                lines = section.split("\n", 1)
                title = lines[0].strip()
                body = lines[1].strip() if len(lines) > 1 else ""
                with st.expander(f"**{title}**", expanded=True):
                    st.markdown(body)
        else:
            st.markdown(analyse)

        # Export JSON
        st.divider()
        export_data = {
            "fichier": uploaded.name,
            "classification": classification,
            "statut": statut,
            "tokens_used": tokens,
            "cout_eur": round(cost_eur, 4),
            "temps_secondes": round(elapsed, 1),
            "analyse": analyse,
        }
        st.download_button(
            label="⬇️ Télécharger le résultat (JSON)",
            data=json.dumps(export_data, ensure_ascii=False, indent=2),
            file_name=f"analyse_{uploaded.name.replace('.zip', '')}.json",
            mime="application/json",
        )

else:
    # État initial — instructions
    st.info(
        "👆 Déposez le ZIP du dossier ODICEE ci-dessus pour démarrer l'analyse.\n\n"
        "Le ZIP doit contenir les PDFs habituels : engagement (OS, bon de commande…), "
        "preuve de réalisation (facture, DGD…), certificat RGE, et idéalement l'attestation sur l'honneur.",
        icon="ℹ️",
    )

    with st.expander("Comment configurer l'application ?"):
        st.markdown("""
**1. Clé API Anthropic**
Renseignez-la dans la barre latérale, ou définissez la variable d'environnement :
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

**2. Dossier des règles**
Copiez vos grimoires et CSV dans `./rules_data/` avec le script fourni :
```bash
python setup_rules.py --source /chemin/vers/grimoires --dest ./rules_data
```

**3. Lancer l'app**
```bash
pip install streamlit anthropic
streamlit run app.py
```
        """)
