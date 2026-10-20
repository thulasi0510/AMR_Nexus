"""Dedicated multi-source Literature Research Agent.

This component owns literature discovery orchestration and returns a stable
report contract to the main AMR LangGraph workflow. It deliberately does not
resolve entities or generate the final scientific answer; those remain main
agent responsibilities.
"""
from dataclasses import dataclass, field
from typing import Any

from agents.router import run_literature


@dataclass
class LiteratureResearchRequest:
    query: str
    entities: dict[str, Any] = field(default_factory=dict)
    intent: list[str] = field(default_factory=list)
    mutation_reference_mutations: list[str] = field(default_factory=list)
    mutation_reference_reported: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)

    def as_plan(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "entities": self.entities,
            "intent": self.intent,
            "mutation_reference_mutations": self.mutation_reference_mutations,
            "mutation_reference_reported": self.mutation_reference_reported,
            **self.options,
        }


@dataclass
class LiteratureResearchReport:
    literature: dict[str, Any]
    request: LiteratureResearchRequest
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        report = dict(self.literature)
        report.setdefault("agent", "Literature Research Agent")
        report.setdefault("agent_contract_version", "1.0")
        report.setdefault("request", {"query": self.request.query, "entities": self.request.entities, "intent": self.request.intent})
        if self.errors:
            report["agent_errors"] = list(self.errors)
        return report


def research(request: LiteratureResearchRequest) -> LiteratureResearchReport:
    """Discover, normalize, deduplicate, and preliminarily filter literature.

    Provider-specific failures are retained by the underlying multi-source
    retriever. An unexpected orchestration failure becomes a structured failed
    report, never a false no-evidence answer.
    """
    try:
        literature = run_literature(request.as_plan())
        return LiteratureResearchReport(literature=literature, request=request)
    except Exception as exc:  # defensive boundary for the LangGraph node
        message = f"{type(exc).__name__}: {exc}"
        return LiteratureResearchReport(
            literature={
                "title": "Multi-source Literature Evidence",
                "papers": [], "references": [], "answer_candidates": [], "top_papers": [],
                "candidate_papers_retrieved": 0,
                "retrieval": {
                    "status": "failed", "queries_attempted": 0, "queries_failed": 0,
                    "records_retrieved": 0, "records_passing_filter": 0, "errors": [message],
                },
                "errors": [message], "evidence_level": "retrieval_failed",
                "source_summary": {"primary": "PubMed / NCBI E-utilities", "supplemental": {"enabled": False, "providers": [], "errors": [message]}},
            },
            request=request,
            errors=[message],
        )
