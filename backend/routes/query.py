from flask import Blueprint, jsonify, request

from services.orchestrator import process_query
from agents.report_generator import public_view
import config

query_bp = Blueprint("query", __name__)


def _debug_requested():
    if config.AMR_DEBUG:
        return True
    return str(request.args.get("debug", "")).strip().lower() in ("1", "true", "yes")


@query_bp.route("/query", methods=["POST"])
def query():
    data = request.get_json(silent=True) or {}
    user_query = str(data.get("query", "")).strip()
    selected_candidate = data.get("selected_candidate")

    if not user_query:
        return jsonify({"status": "error", "message": "Query is required."}), 400

    report = process_query(
        user_query,
        selected_candidate=selected_candidate,
    )

    # Frontend/API consumers only ever see {status, query, title, answer,
    # references} (or the candidate list when disambiguation is needed).
    # Internal LangGraph/agent details are only included when debug mode is
    # explicitly requested (server-wide via config.AMR_DEBUG, or per-request
    # via ?debug=1) -- see agents/report_generator.public_view.
    if _debug_requested():
        return jsonify(report)
    return jsonify(public_view(report))
