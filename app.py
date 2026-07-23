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




def afficher_resultats(data: dict, rules_path) -> None:
    """
    Affiche le résultat d'une analyse depuis son dict d'export (identique au
    JSON téléchargeable). Appelée HORS du bloc 'run', à partir de
    st.session_state : les résultats survivent aux reruns Streamlit (clic sur
    Télécharger...) et un JSON importé se réaffiche à l'identique.
    """
    import json
    classification = data.get("classification", {})
    elapsed = data.get("temps_secondes", 0.0)
    try:
        from utils.rule_loader import RuleLoader
        loader = RuleLoader(rules_path)
    except Exception:
        loader = None

    col1, col2, col3 = st.columns(3)

    with col1:
        statut = data.get("statut", "INDÉTERMINÉ")
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
        tokens = data.get("tokens_used", {})
        total_tok = tokens.get("total", 0)
        cost_eur = data.get("cout_eur", 0.0)
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

    audit = data.get("audit", {})

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

        if data.get("reponse_tronquee"):
            st.error(
                "🚨 La réponse de l'API a été tronquée (limite de tokens atteinte "
                "malgré une relance) — le résultat ci-dessous est incomplet, "
                "relancer l'analyse.",
                icon="🚨",
            )

        _fiches_manquantes = data.get("fiches_manquantes") or []
        if _fiches_manquantes:
            st.error(
                f"🚨 {len(_fiches_manquantes)} fiche(s) classifiée(s) mais SANS élément "
                f"technique détaillé dans ce résultat (malgré une relance automatique) : "
                f"**{', '.join(_fiches_manquantes)}**. Le tableau des éléments techniques "
                f"ci-dessous ne couvre pas ces fiches — les mentions les concernant dans les "
                f"anomalies/synthèse ne remplacent pas une vérification structurée. "
                f"Relancer l'analyse est recommandé.",
                icon="🚨",
            )

        # =====================================================
        # 1. ÉLÉMENTS CLÉS DU DOSSIER (toujours visibles)
        # =====================================================
        st.subheader("📅 Éléments clés")
        date_eng = audit.get("date_engagement_confirmee") or classification.get("date_engagement")
        date_rea = audit.get("date_realisation")
        type_eng_lbl = {
            "ordre_de_service": "Ordre de service", "bon_de_commande": "Bon de commande",
            "acte_engagement": "Acte d'engagement", "devis": "Devis", "inconnu": "Inconnu",
        }.get(classification.get("type_engagement", "inconnu"), "?")

        delai_str, incoherence_dates = "—", False
        realisation_perimee, age_realisation = False, None
        if date_eng and date_rea:
            try:
                from datetime import datetime as _dt
                _d1 = _dt.strptime(date_eng, "%d/%m/%Y")
                _d2 = _dt.strptime(date_rea, "%d/%m/%Y")
                _delai = (_d2 - _d1).days
                delai_str = f"{_delai} j"
                incoherence_dates = _delai < 0
            except ValueError:
                delai_str = "format ?"
        if date_rea:
            try:
                from datetime import datetime as _dt
                age_realisation = (_dt.today() - _dt.strptime(date_rea, "%d/%m/%Y")).days
                realisation_perimee = age_realisation > 365
            except ValueError:
                pass

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Date d'engagement", date_eng or "❌ Introuvable",
                  help=f"Document d'engagement : {type_eng_lbl}. "
                       "Date confirmée par l'audit dans les documents — elle détermine "
                       "la version de fiche et la validité RGE.")
        c2.metric("Date de réalisation", date_rea or "❌ Introuvable",
                  help="Date d'achèvement des travaux, ou à défaut date de la facture "
                       "finale / du DGD.")
        c3.metric("Délai eng. → réal.", delai_str,
                  help="Vérification déterministe (calculée en Python, pas par l'IA). "
                       "Un délai négatif est une incohérence majeure.")
        c4.metric("Fiche(s)", ", ".join(
            f.get("code", "?") for f in audit.get("fiches", [])) or "?")

        for fiche_obj in audit.get("fiches", []):
            if fiche_obj.get("version_applicable"):
                st.caption(f"**{fiche_obj.get('code', '?')}** — {fiche_obj['version_applicable']}")

        # --- Identité & lieu (preuve de réalisation) — compact ---
        pro = audit.get("professionnel_realisation")
        adresse = audit.get("adresse_travaux")
        sous_t = audit.get("sous_traitant")
        lignes_id = [
            f"**🏗️ Professionnel :** {pro or '❌ Non identifié'}",
            f"**📍 Adresse :** {adresse or '❌ Introuvable'}",
        ]
        if sous_t:
            lignes_id.append(f"**🔩 Sous-traitant :** {sous_t} _(doit porter la RGE si exigée)_")
        if audit.get("montant_ht"):
            lignes_id.append(f"**💶 Montant HT :** {audit['montant_ht']}")
        st.markdown("  \n".join(lignes_id))

        # --- Constat visuel signatures/tampons (passe vision dédiée) ---
        _sig = data.get("verification_signatures")
        if _sig:
            if _sig.get("erreur"):
                st.caption(f"🖋️ Vérification visuelle des signatures indisponible : "
                           f"{_sig['erreur']}")
            else:
                lignes_sig = []
                for pg in _sig.get("pages", []):
                    s_ok = pg.get("signature_manuscrite_presente")
                    t_ok = pg.get("tampon_present")
                    if not s_ok and not t_ok:
                        icone, constat = "❌", "ni signature ni tampon"
                    elif s_ok and t_ok:
                        icone, constat = "✅", "signature + tampon"
                    elif s_ok:
                        icone, constat = "🟡", "signature seule"
                    else:
                        icone, constat = "🟡", "tampon seul"
                    date_s = pg.get("date_manuscrite_ou_tamponnee")
                    lignes_sig.append(
                        f"{icone} {pg.get('document', '?')} p.{pg.get('page', '?')} : {constat}"
                        + (f", daté {date_s}" if date_s else "")
                    )
                if lignes_sig:
                    st.markdown("**🖋️ Signatures (vision) :** " + " · ".join(lignes_sig))

        # --- RGE : affichée seulement si la fiche/version l'exige ---
        # (colonne 'QUALIFICATION DU PROFESSIONNEL' du récap xlsx : 55
        # versions BAR sur 132 n'exigent aucune qualification)
        axes_preview = audit.get("axes", {})
        rge_verdict = (axes_preview.get("rge") or {}).get("verdict", "?")
        rge_icon = {"VALIDE": "✅", "NON VALIDE": "❌", "INCOMPLET": "⚠️"}.get(rge_verdict, "❓")
        for fiche_obj in audit.get("fiches", []):
            code = fiche_obj.get("code", "?")
            try:
                qual = loader.get_qualification_requise(
                    code, classification.get("secteur", "BAR"), date_eng)
            except Exception:
                qual = {"requise": None, "texte": None}
            if qual["requise"] is True:
                texte_court = (qual["texte"] or "").replace("\n", " · ")
                if len(texte_court) > 110:
                    texte_court = texte_court[:110] + "…"
                st.markdown(f"**🎓 RGE requise** ({code}) — {rge_icon} **{rge_verdict}** · {texte_court}")
            elif qual["requise"] is False:
                st.caption(f"🎓 RGE ({code}) : non requise pour cette version")
            else:
                st.caption(f"🎓 RGE ({code}) : exigence non déterminable depuis le "
                           f"référentiel (fiche absente du récap xlsx) — voir l'axe "
                           f"RGE dans le récapitulatif.")

        if incoherence_dates:
            st.error("🚨 La date de réalisation est ANTÉRIEURE à la date "
                     "d'engagement — incohérence majeure (travaux engagés après "
                     "leur réalisation ?). À vérifier en priorité absolue.", icon="🚨")
        if realisation_perimee:
            st.error(f"🚨 DOSSIER NON ÉLIGIBLE — la date de réalisation "
                     f"({date_rea}) date de {age_realisation} jours, soit plus "
                     f"de 12 mois par rapport à la date du jour (règle "
                     f"`regles_realisation.md` : la réalisation doit dater de "
                     f"moins de 12 mois à la date d'analyse). Contrôle "
                     f"déterministe calculé en Python, indépendant de l'IA.",
                     icon="🚨")

        # --- Catégorisation des documents (engagement vs réalisation) ---
        docs_eng = data.get("audit", {}).get("documents_engagement") or []
        docs_rea = data.get("audit", {}).get("documents_realisation") or []
        if docs_eng or docs_rea:
            st.caption(
                f"📄 Engagement : {', '.join(docs_eng) or '❌ aucun'} · "
                f"Réalisation : {', '.join(docs_rea) or '❌ aucune'}"
            )
            if not docs_eng:
                st.warning("Aucun document d'engagement identifié dans le dossier — "
                           "la date d'engagement ne peut pas être établie de façon "
                           "probante.", icon="⚠️")

        # =====================================================
        # 2. ÉLÉMENTS TECHNIQUES DES TRAVAUX (toujours visibles)
        # =====================================================
        st.subheader("🔧 Éléments techniques des travaux")
        for fiche_obj in audit.get("fiches", []):
            v_tech = fiche_obj.get("verdict_technique", "?")
            icon = {"VALIDE": "✅", "NON VALIDE": "❌", "INCOMPLET": "⚠️"}.get(v_tech, "❓")
            if len(audit.get("fiches", [])) > 1:
                st.markdown(f"{icon} **{fiche_obj.get('code', '?')}** — verdict technique : {v_tech}")
            else:
                st.markdown(f"{icon} Verdict technique : **{v_tech}**")
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
                        "Conforme": conforme_str,
                        "Citation vérifiée": verif_str,
                        "Citation exacte (à vérifier)": el.get("citation_verbatim") or "—",
                        "Source": el.get("source") or "—",
                    })
                st.dataframe(rows, use_container_width=True, hide_index=True)
            else:
                st.caption("Aucun élément technique détaillé retourné.")
        st.caption(
            "« Citation vérifiée » confirme que le texte existe dans les documents, "
            "pas qu'il est attribué au bon composant — sur une facture multi-lignes, "
            "vérifiez la colonne « Citation exacte »."
        )

        # =====================================================
        # 3. POINTS À VÉRIFIER (consolidés, toujours visibles)
        # =====================================================
        st.subheader("🔍 Points à vérifier")
        bloquants, avertissements = [], []

        # a) Contrôles en échec, tous axes confondus
        axes = audit.get("axes", {})
        axe_labels = {
            "logique_globale": "Logique globale",
            "engagement": "Engagement",
            "realisation_documentaire": "Réalisation",
            "rge": "RGE",
            "ah": "AH",
            "coherence": "Cohérence eng. ↔ réal.",
            "documents_annexes": "Documents annexes",
        }
        for key, label in axe_labels.items():
            axe = axes.get(key) or {}
            axe_v = axe.get("verdict", "")
            for ctrl in axe.get("controles", []):
                if not ctrl.get("verdict"):
                    txt = f"**[{label}]** {ctrl.get('item', '?')}"
                    if ctrl.get("details"):
                        txt += f" — {ctrl['details']}"
                    (bloquants if axe_v == "NON VALIDE" else avertissements).append(txt)

        # b) Éléments techniques en défaut
        for fiche_obj in audit.get("fiches", []):
            code = fiche_obj.get("code", "?")
            for el in fiche_obj.get("elements_techniques", []):
                champ = el.get("champ", "?")
                if el.get("conforme") is False:
                    bloquants.append(f"**[Technique {code}]** `{champ}` NON CONFORME au seuil "
                                     f"(valeur : {el.get('valeur_trouvee') or '?'})")
                elif not el.get("present") and el.get("hors_extrait_possible"):
                    avertissements.append(f"**[Technique {code}]** `{champ}` introuvable, mais le "
                                          f"document source est PARTIEL — vérifier le document original")
                elif not el.get("present"):
                    avertissements.append(f"**[Technique {code}]** `{champ}` absent de la preuve "
                                          f"de réalisation")
                if el.get("citation_verifiee") is False:
                    bloquants.append(f"**[Technique {code}]** citation de `{champ}` INTROUVABLE "
                                     f"dans les documents — risque d'hallucination, vérifier en priorité")

        # c) Anomalies signalées par l'audit — DÉDUPLIQUÉES contre les
        # contrôles d'axes déjà en échec : le modèle est instruit de ne plus
        # produire ces redites (schéma), mais ce filet d'affichage protège
        # aussi les anciens JSON réimportés. Mesuré sur un dossier réel :
        # 3 anomalies sur 8 recouvraient un contrôle KO à plus de 35%
        # de mots communs — seuil retenu ici.
        import re as _re

        def _mots(s):
            return set(_re.findall(r"[a-zà-ù0-9]{4,}", s.lower()))

        _controles_ko_txt = []
        for _axe in audit.get("axes", {}).values():
            for _c in _axe.get("controles", []):
                if not _c.get("verdict"):
                    _controles_ko_txt.append(
                        _mots((_c.get("item", "") + " " + (_c.get("details") or ""))))
        for a in audit.get("anomalies", []):
            _wa = _mots(a)
            _redondante = any(
                len(_wa & _wc) / max(1, len(_wa)) > 0.35 for _wc in _controles_ko_txt)
            if not _redondante:
                avertissements.append(f"**[Anomalie]** {a}")

        if not bloquants and not avertissements:
            st.success("Aucun point bloquant ni anomalie détecté sur ce dossier.", icon="✅")
        else:
            for b in bloquants:
                st.error(b, icon="❌")
            for w in avertissements:
                st.warning(w, icon="⚠️")

        # =====================================================
        # 4. RÉCAP COMPLET (replié — vue épurée)
        # =====================================================
        st.subheader("📋 Récapitulatif complet des vérifications")

        axe_labels_full = {
            "logique_globale": "1. Logique globale",
            "engagement": "2. Validation engagement",
            "realisation_documentaire": "3. Validation réalisation (documentaire)",
            "rge": "4. Validation RGE",
            "ah": "5. Validation AH",
            "coherence": "6. Cohérence engagement ↔ réalisation",
            "documents_annexes": "7. Documents annexes",
        }
        for key, label in axe_labels_full.items():
            axe = axes.get(key)
            if not axe:
                continue
            v = axe.get("verdict", "?")
            icon = {"VALIDE": "✅", "NON VALIDE": "❌", "INCOMPLET": "⚠️"}.get(v, "❓")
            n_ok = sum(1 for c in axe.get("controles", []) if c.get("verdict"))
            n_tot = len(axe.get("controles", []))
            with st.expander(f"{icon} **{label}** — {v} ({n_ok}/{n_tot} contrôles OK)",
                             expanded=False):
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

        with st.expander("🏷️ Classification du dossier", expanded=False):
            st.json(classification)
    else:
        st.warning("Aucune donnée structurée retournée par l'API — réponse inattendue.", icon="⚠️")
        st.text(data.get("analyse", "(vide)"))

    st.divider()

    # --- Documents annotés (surlignages des éléments de l'audit) ---
    pdfs_annotes = st.session_state.get("pdfs_annotes")
    st.subheader("📑 Documents annotés")
    if pdfs_annotes:
        st.caption(
            "Les citations et valeurs clés ayant servi à l'audit sont surlignées "
            "dans les documents : 🟩 conforme · 🟨 présent (conformité non "
            "applicable) · 🟥 non conforme · 🟦 valeurs clés (dates, montant, "
            "SIRET, adresse). Survolez un surlignage dans votre lecteur PDF "
            "pour voir l'élément correspondant."
        )
        role_labels = {"engagement": "📝 Engagement", "realisation": "🔧 Réalisation",
                        "autre": "📄 Autre"}
        cols = st.columns(min(3, max(1, len(pdfs_annotes))))
        for i, (name, info) in enumerate(pdfs_annotes.items()):
            with cols[i % len(cols)]:
                if info.get("bytes"):
                    st.download_button(
                        label=f"⬇️ {name}",
                        data=info["bytes"],
                        file_name=f"annote_{name}",
                        mime="application/pdf",
                        key=f"dl_annot_{i}",
                    )
                    st.caption(f"{role_labels.get(info.get('role'), '📄')} · "
                               f"{info.get('n_annotations', 0)} surlignage(s)")
                else:
                    st.caption(f"❌ {name} : annotation impossible "
                               f"({info.get('erreur', '?')})")
    else:
        st.caption("Disponibles après une analyse complète. Pour une analyse "
                   "importée en JSON : déposez aussi le ZIP du dossier ci-dessus, "
                   "puis utilisez le bouton d'annotation qui apparaîtra.")

    st.download_button(
        label="⬇️ Télécharger le résultat (JSON)",
        data=json.dumps(data, ensure_ascii=False, indent=2),
        file_name=f"analyse_{data.get('fichier', 'dossier').replace('.zip', '')}.json",
        mime="application/json",
        key="dl_json_resultat",
    )
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

    st.divider()
    with st.expander("📂 Réafficher une analyse (import JSON)", expanded=False):
        st.caption("Importez un JSON exporté précédemment pour retrouver le "
                   "visuel complet des résultats, sans relancer d'analyse.")
        json_import = st.file_uploader("Fichier d'analyse", type=["json"],
                                        key="json_import_uploader")
        if json_import is not None:
            try:
                _imported = json.load(json_import)
                if not isinstance(_imported, dict) or "audit" not in _imported:
                    st.error("Ce JSON ne ressemble pas à un export d'analyse "
                             "(clé 'audit' absente).")
                else:
                    st.session_state["analyse_resultat"] = _imported
                    st.success(f"Analyse « {_imported.get('fichier', '?')} » chargée.")
            except json.JSONDecodeError:
                st.error("Fichier JSON invalide.")

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

                # `pdf_files` pointe dans `tmpdir`, qui sera SUPPRIMÉ du disque
                # dès la sortie de ce bloc `with` -- mais l'annotation des PDF
                # et la vérification visuelle des signatures n'interviennent
                # qu'après (une fois l'audit terminé). Sans copie préalable des
                # octets, ces étapes tombaient sur des chemins déjà supprimés
                # ("no such file"). On garde donc les octets en mémoire ici,
                # pour les réécrire dans un répertoire temporaire dédié juste
                # avant l'annotation (créé sans context manager, donc non
                # supprimé prématurément).
                pdf_bytes_map = {Path(p).name: Path(p).read_bytes() for p in pdf_files}

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
                if not correspondance_table:
                    st.warning(
                        "⚠️ Table de correspondance fiche↔travaux VIDE : ni le xlsx "
                        "récapitulatif ni les CSV n'ont pu être lus dans le dossier de "
                        "règles. La classification perd son garde-fou principal contre "
                        "la confusion de fiches voisines (ex: combles classés BAR-EN-103 "
                        "au lieu de BAR-EN-101) — vérifier le déploiement de "
                        "`rules_data/`.",
                        icon="⚠️",
                    )

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
        # MODE RÉEL : calculer le coût complet, persister le résultat en
        # session_state, puis laisser l'affichage (hors bloc run) le rendre.
        # Ainsi un rerun Streamlit (clic sur Télécharger...) ne perd rien.
        # ══════════════════════════════════════════════════════════════
        else:
            tokens = result.get("tokens_used", {})
            from utils.claude_client import PRICE_INPUT_USD_MTOK, PRICE_OUTPUT_USD_MTOK
            cost_eur = (
                tokens.get("input", 0) * PRICE_INPUT_USD_MTOK
                + tokens.get("output", 0) * PRICE_OUTPUT_USD_MTOK
                + tokens.get("cache_write", 0) * PRICE_INPUT_USD_MTOK * 1.25
                + tokens.get("cache_read", 0) * PRICE_INPUT_USD_MTOK * 0.1
            ) / 1_000_000 * 0.92
            st.session_state["analyse_resultat"] = {
                "fichier": uploaded.name,
                "classification": classification,
                "statut": result.get("statut", "INDÉTERMINÉ"),
                "tokens_used": tokens,
                "cout_eur": round(cost_eur, 4),
                "temps_secondes": round(elapsed, 1),
                "reponse_tronquee": result.get("reponse_tronquee", False),
                "fiches_manquantes": result.get("fiches_manquantes", []),
                "analyse": result.get("analyse", ""),
                "audit": result.get("audit", {}),
            }

            # Réécriture des PDF dans un répertoire dédié NON supprimé
            # automatiquement (créé sans `with`, contrairement à `tmpdir` qui
            # a déjà disparu à ce stade) -- c'est ce chemin, pas `pdf_files`
            # (obsolète), qu'utilisent la vision signatures et l'annotation.
            _persist_dir = Path(tempfile.mkdtemp(prefix="cee_annot_"))
            pdf_files_persist = []
            for name, data_bytes in pdf_bytes_map.items():
                p = _persist_dir / name
                p.write_bytes(data_bytes)
                pdf_files_persist.append(p)

            # --- Passe VISION signatures/tampons sur le(s) document(s)
            # d'engagement : comble la limite structurelle de l'extraction
            # texte (une signature manuscrite n'existe pas dans le texte).
            # Appel API séparé, minuscule (~1 600 tk/page d'image, effort
            # low, ~0,01 €), sans corpus de règles.
            try:
                from utils.vision_signatures import (check_signatures,
                                                      selectionner_docs_engagement)
                _docs_eng = selectionner_docs_engagement(
                    pdf_files_persist, result.get("audit", {}) or {})
                if _docs_eng:
                    with st.status("🖋️ Vérification visuelle des signatures/"
                                    "tampons (vision)…", expanded=False):
                        _sig = check_signatures(_docs_eng)
                    st.session_state["analyse_resultat"]["verification_signatures"] = _sig
                    # Coût de cet appel séparé (vision) -- absent jusqu'ici du
                    # coût total affiché, alors qu'il est systématiquement
                    # facturé dès qu'un document d'engagement est identifié.
                    _sig_tk = (_sig or {}).get("tokens_used")
                    if _sig_tk:
                        from utils.claude_client import PRICE_INPUT_USD_MTOK, PRICE_OUTPUT_USD_MTOK
                        _sig_cost = (
                            _sig_tk.get("input", 0) * PRICE_INPUT_USD_MTOK
                            + _sig_tk.get("output", 0) * PRICE_OUTPUT_USD_MTOK
                        ) / 1_000_000 * 0.92
                        st.session_state["analyse_resultat"]["cout_eur"] = round(
                            st.session_state["analyse_resultat"].get("cout_eur", 0.0) + _sig_cost, 4)
            except Exception as _e:
                st.session_state["analyse_resultat"]["verification_signatures"] = {
                    "erreur": str(_e)}

            # --- PDF annotés : surligner dans les documents les citations et
            # valeurs clés ayant servi à l'audit. Zéro token API (recherche
            # locale des citations verbatim déjà retournées) ; le seul coût
            # est du temps local (OCR de localisation pour les scannés).
            try:
                with st.status("🖍️ Annotation des PDF (surlignage des éléments "
                                "de l'audit)…", expanded=False):
                    from utils.annotator import annotate_dossier
                    st.session_state["pdfs_annotes"] = annotate_dossier(
                        pdf_files_persist, result.get("audit", {}) or {})
            except Exception as _e:
                st.session_state["pdfs_annotes"] = None
                st.warning(f"Annotation des PDF impossible : {_e}", icon="⚠️")

# ══════════════════════════════════════════════════════════════════════
# AFFICHAGE DES RÉSULTATS — hors du bloc 'run' : rendu depuis
# session_state à CHAQUE rerun, donc le clic sur Télécharger (qui
# relance le script) ne fait plus disparaître les résultats, et un JSON
# importé depuis la sidebar s'affiche par le même chemin.
# ══════════════════════════════════════════════════════════════════════
if st.session_state.get("analyse_resultat"):
    st.divider()
    _data = st.session_state["analyse_resultat"]
    st.markdown(f"#### 📄 Résultats — `{_data.get('fichier', '?')}`")

    # Analyse importée (JSON) + ZIP déposé mais pas encore annoté : proposer
    # l'annotation locale des PDF avec les citations de cette analyse.
    if uploaded and not st.session_state.get("pdfs_annotes"):
        if st.button("🖍️ Annoter les documents du ZIP avec cette analyse",
                      key="btn_annoter_import"):
            try:
                with st.status("🖍️ Annotation des PDF…", expanded=False):
                    import tempfile
                    from utils.extractor import extract_zip
                    from utils.annotator import annotate_dossier
                    _tmp = tempfile.mkdtemp(prefix="annot_")
                    _pdfs = extract_zip(uploaded, _tmp)
                    st.session_state["pdfs_annotes"] = annotate_dossier(
                        _pdfs, _data.get("audit", {}) or {})
                st.rerun()
            except Exception as _e:
                st.warning(f"Annotation impossible : {_e}", icon="⚠️")

    afficher_resultats(_data, rules_path)

if not uploaded and not st.session_state.get("analyse_resultat"):
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
