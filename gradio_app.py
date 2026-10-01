#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "gradio>=4.0.0",
#   "requests>=2.31.0",
# ]
# ///
"""Small Gradio client for the Humatheque Alma Check API.

Run:
  UV_CACHE_DIR=/root/.cache/uv uv run --script gradio_app.py
"""

from __future__ import annotations

import html
import json
import os
from typing import Any

import gradio as gr
import requests


DEFAULT_API_URL = os.getenv("ALMA_CHECK_API_URL", "https://alma-checker.smartbiblia.fr")
DEFAULT_API_KEY = os.getenv("ALMA_CHECK_API_KEY", "")

PROFILES = ["thesis", "dissertation", "academic"]

EXAMPLE_JSON = """{
  "title": "La question de l'hygiène aux Indes-Néerlandaises",
  "subtitle": "Les enjeux médicaux, culturels et sociaux",
  "author": "Gani Achmad Jae Lani",
  "degree_type": "Thèse de doctorat",
  "discipline": "Histoire et civilisations",
  "volume": null,
  "granting_institution": "École des Hautes Études en Sciences Sociales",
  "co_tutelle_institutions": [],
  "doctoral_school": "École doctorale de l'EHESS",
  "defense_year": "2017",
  "advisor": ["Gérard Jorland"],
  "jury_president": null,
  "reviewers": [],
  "committee_members": ["Romain Bertrand", "Patrice Bourdelais", "Charles Illouz", "Annick Opinel", "Patrick Zylberman", "Gérard Jorland"],
  "language": "fre",
  "confidence": 0.98
}"""

# Champs acceptes par AlmaCheckRequest (app.py) ; le reste du JSON est ignore.
EXTRACTION_FIELDS = [
    "title",
    "subtitle",
    "author",
    "degree_type",
    "discipline",
    "volume",
    "granting_institution",
    "co_tutelle_institutions",
    "doctoral_school",
    "defense_year",
    "advisor",
    "jury_president",
    "reviewers",
    "committee_members",
    "language",
    "confidence",
]

STATUS_COLORS = {
    "academic_record_found": ("#dcfce7", "#166534"),
    "ambiguous_academic_candidate": ("#fef9c3", "#854d0e"),
    "off_profile_match_only": ("#ffedd5", "#9a3412"),
    "no_academic_record_found": ("#e5e7eb", "#374151"),
}

SCORE_KEYS = ["final", "bibliographic", "title", "author", "academic", "year", "language", "context", "advisor"]


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def parse_extraction_json(json_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise gr.Error(f"JSON invalide: {exc}") from exc
    if not isinstance(payload, dict):
        raise gr.Error("Le JSON colle doit etre un objet.")
    if not str(payload.get("title") or "").strip():
        raise gr.Error("Le champ `title` est obligatoire.")
    return payload


def build_check_payload(
    extraction: dict[str, Any],
    max_records_per_query: int,
    max_candidates: int,
    match_threshold: float,
    ambiguous_threshold: float,
    include_unimarc_xml: bool,
) -> dict[str, Any]:
    payload = {key: extraction[key] for key in EXTRACTION_FIELDS if extraction.get(key) is not None}
    payload.update(
        {
            "max_records_per_query": int(max_records_per_query),
            "max_candidates": int(max_candidates),
            "match_threshold": float(match_threshold),
            "ambiguous_threshold": float(ambiguous_threshold),
            "include_unimarc_xml": bool(include_unimarc_xml),
        }
    )
    return payload


def status_badge(status: str) -> str:
    bg, fg = STATUS_COLORS.get(status, ("#e5e7eb", "#374151"))
    return (
        f'<span style="display:inline-block;padding:4px 9px;border-radius:999px;'
        f'background:{bg};color:{fg};font-weight:700;font-size:13px">{esc(status)}</span>'
    )


def ppn_link(candidate: dict[str, Any]) -> str:
    if not candidate.get("ppn"):
        return '<span class="muted">sans PPN</span>'
    return f"<a href='{esc(candidate.get('sudoc_url'))}' target='_blank'>{esc(candidate['ppn'])}</a>"


def score_table(candidate: dict[str, Any]) -> str:
    score = candidate.get("score") or {}
    rows = "".join(
        f"<tr><td>{esc(key)}</td><td><strong>{float(score[key]):.4f}</strong></td></tr>"
        for key in SCORE_KEYS
        if score.get(key) is not None
    )
    return (
        '<table class="score-table"><thead><tr><th>Composante</th><th>Score</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def items_table(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<p class="muted">Aucun exemplaire (930 / 995 / AVA).</p>'
    rows = "".join(
        "<tr>"
        f"<td>{esc(item.get('field'))}</td>"
        f"<td>{esc(item.get('library'))}</td>"
        f"<td>{esc(item.get('location'))}</td>"
        f"<td>{esc(item.get('call_number'))}</td>"
        f"<td>{esc(item.get('availability') or item.get('status'))}</td>"
        "</tr>"
        for item in items
    )
    return (
        '<table class="candidates-table"><thead><tr><th>Champ</th><th>Bibliotheque</th>'
        "<th>Localisation</th><th>Cote</th><th>Disponibilite</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def candidate_panel(title: str, candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return ""
    academic = candidate.get("academic") or {}
    evidence = academic.get("evidence") or []
    evidence_html = (
        "<ul>" + "".join(f"<li>{esc(item)}</li>" for item in evidence) + "</ul>"
        if evidence
        else '<p class="muted">Aucun indice 328 / 608.</p>'
    )
    return f"""
    <section class="panel">
      <h3>{esc(title)}</h3>
      <p><strong>Titre :</strong> {esc(candidate.get('title'))}</p>
      <p><strong>Auteurs :</strong> {esc(' | '.join(candidate.get('authors') or []))}</p>
      <p><strong>MMS ID :</strong> {esc(candidate.get('mms_id'))}
         &nbsp;<strong>PPN :</strong> {ppn_link(candidate)}
         &nbsp;<strong>Annee :</strong> {esc(candidate.get('year'))}</p>
      <p><strong>Nature deduite :</strong> {esc(academic.get('kind'))}
         ({'compte' if candidate.get('counts_for_profile') else 'hors profil'})</p>
      <div class="evidence-grid">
        <div class="evidence-item"><h4>Indices universitaires</h4>{evidence_html}</div>
        <div class="evidence-item"><h4>Scores</h4>{score_table(candidate)}</div>
      </div>
      <h4>Exemplaires</h4>
      {items_table(candidate.get('items') or [])}
    </section>
    """


def candidates_table(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return '<p class="muted">Aucun candidat.</p>'
    rows = "".join(
        "<tr>"
        f"<td>{esc(candidate.get('mms_id'))}</td>"
        f"<td>{ppn_link(candidate)}</td>"
        f"<td>{esc(candidate.get('title'))}</td>"
        f"<td>{esc(' | '.join(candidate.get('authors') or []))}</td>"
        f"<td>{esc(candidate.get('year'))}</td>"
        f"<td>{esc((candidate.get('academic') or {}).get('kind'))}</td>"
        f"<td>{float((candidate.get('score') or {}).get('final') or 0):.4f}</td>"
        f"<td>{int(candidate.get('items_count') or 0)}</td>"
        "</tr>"
        for candidate in candidates
    )
    return (
        '<table class="candidates-table"><thead><tr><th>MMS ID</th><th>PPN</th><th>Titre</th>'
        "<th>Auteurs</th><th>Annee</th><th>Nature</th><th>Final</th><th>Ex.</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def queries_table(queries: list[dict[str, Any]]) -> str:
    if not queries:
        return '<p class="muted">Aucune requete.</p>'
    rows = "".join(
        "<tr>"
        f"<td><code>{esc(query.get('query'))}</code></td>"
        f"<td>{esc(query.get('total_found'))}</td>"
        f"<td>{esc(query.get('error') or '')}</td>"
        "</tr>"
        for query in queries
    )
    return (
        '<table class="candidates-table"><thead><tr><th>Requete CQL</th><th>Trouves</th>'
        f"<th>Erreur</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def render_result(result: dict[str, Any]) -> str:
    profile = result.get("profile") or {}
    support = result.get("decision_support") or {}
    best = result.get("best_academic_candidate")
    off_profile = result.get("best_off_profile_candidate")
    return f"""
    <div class="result">
      <section class="panel">
        <h3>Decision</h3>
        <p>{status_badge(str(result.get('status') or ''))}</p>
        <p><strong>Profil :</strong> {esc(profile.get('name'))}
           <span class="muted">({esc(', '.join(profile.get('accepted_kinds') or []))})</span></p>
        <p><strong>Score :</strong> {float(result.get('match_score') or 0):.4f}</p>
        <p><strong>PPN du meilleur candidat :</strong>
           {ppn_link(best) if best else '<span class="muted">aucun</span>'}</p>
        <p><strong>Candidats universitaires avec / sans PPN :</strong>
           {int(support.get('academic_candidates_with_ppn') or 0)} /
           {int(support.get('academic_candidates_without_ppn') or 0)}</p>
      </section>
      {candidate_panel("Meilleur candidat universitaire", best)}
      {candidate_panel("Meilleur candidat hors profil", off_profile) if not best else ""}
      <section class="panel">
        <h3>Tous les candidats</h3>
        {candidates_table(result.get('candidates') or [])}
      </section>
      <section class="panel">
        <h3>Requetes SRU</h3>
        {queries_table((result.get('sru') or {}).get('queries') or [])}
      </section>
    </div>
    """


def check_ui(
    json_text: str,
    api_url: str,
    api_key: str,
    profile: str,
    max_records_per_query: int,
    max_candidates: int,
    match_threshold: float,
    ambiguous_threshold: float,
    include_unimarc_xml: bool,
) -> tuple[str, str]:
    extraction = parse_extraction_json(json_text)
    payload = build_check_payload(
        extraction,
        max_records_per_query,
        max_candidates,
        match_threshold,
        ambiguous_threshold,
        include_unimarc_xml,
    )
    headers = {"Content-Type": "application/json"}
    if api_key.strip():
        headers["X-API-Key"] = api_key.strip()

    try:
        response = requests.post(
            f"{api_url.rstrip('/')}/check/{profile}", headers=headers, json=payload, timeout=180
        )
        if not response.ok:
            raise gr.Error(f"Erreur API {response.status_code}: {response.text[:1000]}")
        result = response.json()
    except requests.RequestException as exc:
        raise gr.Error(f"Erreur lors de l'appel API: {exc}") from exc
    except ValueError as exc:
        raise gr.Error("La reponse API n'est pas un JSON valide.") from exc

    return render_result(result), json.dumps(result, ensure_ascii=False, indent=2)


CSS = """
.gradio-container { max-width: 1280px !important; }
.muted { color: #6b7280; }
.panel {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 14px 16px;
  margin: 12px 0;
  background: #fff;
}
.panel h3 { margin-top: 0; }
.score-table, .candidates-table { width: 100%; border-collapse: collapse; margin-top: 10px; }
.score-table th, .score-table td, .candidates-table th, .candidates-table td {
  border-bottom: 1px solid #e5e7eb;
  padding: 7px 8px;
  text-align: left;
  vertical-align: top;
}
.score-table th, .candidates-table th { background: #f9fafb; font-weight: 700; }
.evidence-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.evidence-item { border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px; background: #f9fafb; }
.evidence-item h4 { margin: 0 0 6px 0; }
.evidence-item ul { margin: 0; }
@media (max-width: 800px) { .evidence-grid { grid-template-columns: 1fr; } }
"""


with gr.Blocks(css=CSS, title="Humatheque Alma Check") as demo:
    gr.Markdown(
        """
        # Vérification Humathèque Alma SRU
        Collez le JSON produit par l'extraction VLM, choisissez un profil de document,
        puis vérifiez si une notice universitaire existe déja dans Alma (et si elle porte un PPN Sudoc).
        """
    )

    with gr.Row():
        with gr.Column(scale=3):
            json_input = gr.Code(value=EXAMPLE_JSON, language="json", label="JSON d'extraction", lines=22)
        with gr.Column(scale=2):
            api_url = gr.Textbox(value=DEFAULT_API_URL, label="URL de l'API Alma Check")
            api_key = gr.Textbox(value=DEFAULT_API_KEY, label="API key", type="password")
            profile = gr.Radio(PROFILES, value="thesis", label="Profil de document")
            with gr.Accordion("Paramètres avancés", open=False):
                max_records_per_query = gr.Slider(1, 100, value=10, step=1, label="Max notices par requête SRU")
                max_candidates = gr.Slider(1, 100, value=20, step=1, label="Max candidats")
                match_threshold = gr.Slider(0.0, 1.0, value=0.78, step=0.01, label="Seuil de correspondance")
                ambiguous_threshold = gr.Slider(0.0, 1.0, value=0.62, step=0.01, label="Seuil d'ambiguité")
                include_unimarc_xml = gr.Checkbox(value=False, label="Inclure le XML UNIMARC brut")

    check_btn = gr.Button("Vérifier dans Alma", variant="primary")

    result_html = gr.HTML(label="Résultat lisible")
    with gr.Accordion("Réponse API brute", open=False):
        raw_json = gr.Code(language="json", label="Réponse API brute", lines=18)

    check_btn.click(
        check_ui,
        inputs=[
            json_input,
            api_url,
            api_key,
            profile,
            max_records_per_query,
            max_candidates,
            match_threshold,
            ambiguous_threshold,
            include_unimarc_xml,
        ],
        outputs=[result_html, raw_json],
    )

if __name__ == "__main__":
    port = int(os.getenv("PORT", "7862"))
    demo.launch(server_name="0.0.0.0", server_port=port)
