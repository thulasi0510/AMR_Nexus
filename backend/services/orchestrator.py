"""LangGraph orchestration for the modular AMR research assistant."""
from typing import Any, TypedDict
import time
from config import AMR_PMC_ENRICHMENT_ENABLED
from langgraph.graph import END, START, StateGraph

from agents.query_understanding import understand_query
from agents.planner import create_plan
from agents.entity_normalization import normalize_entities
from agents.mutation_reference import retrieve_mutations
from agents.mutation_matching import records_for_matching
from agents.pmc_evidence import enrich_with_pmc
from agents.literature_research_agent import LiteratureResearchRequest, research as run_literature_research
from agents.evidence_analysis import analyze_literature
from agents.response_synthesis import synthesize_response
from agents.report_generator import generate_report


class ResearchState(TypedDict, total=False):
    query: str
    selected_candidate: str | None
    understanding: dict[str, Any]
    plan: dict[str, Any]
    literature_results: list[dict[str, Any]]
    evidence: dict[str, Any]
    mutation_reference: dict[str, Any]
    answer: str
    report: dict[str, Any]
    execution_trace: list[dict[str, Any]]


def _trace(state, agent, details=None):
    trace = list(state.get("execution_trace", []))
    trace.append({"agent": agent, "status": "completed", "details": details or {}})
    return trace


def query_understanding_node(state):
    u = understand_query(state["query"], state.get("selected_candidate"))
    return {"understanding": u, "execution_trace": _trace(state, "Query Understanding Agent", {"intent": u.get("intent"), "entities": u.get("entities")})}


def entity_normalization_node(state):
    u = normalize_entities(state["understanding"])
    return {
        "understanding": u,
        "execution_trace": _trace(
            state,
            "Entity Normalization Agent",
            {
                "organism_sources": [x.get("source") for x in u.get("organism_resolutions", [])],
                "antibiotic_sources": [x.get("source") for x in u.get("antibiotic_resolutions", [])],
            },
        ),
    }


def planner_node(state):
    p = create_plan(state["query"], state["understanding"])
    return {"plan": p, "execution_trace": _trace(state, "AMR Retrieval Planner Agent", {"sources": p.get("sources"), "agents": p.get("agents_required")})}

def route_after_planning(state):
    if state["plan"].get("status") == "out_of_scope":
        return "out_of_scope"

    entities = state["understanding"].get("entities", {})
    if entities.get("genes") or entities.get("mutations") or entities.get("variants"):
        return "mutation_reference"

    return "literature"


def mutation_reference_node(state):
    mutation_understanding = dict(state["understanding"])
    mutation_understanding["query"] = state.get("query", "")
    result = retrieve_mutations(mutation_understanding)
    return {
        "mutation_reference": result,
        "execution_trace": _trace(
            state,
            "Mutation Reference Agent",
            {"status": result.get("status"), "records": len(result.get("mutations", []))},
        ),
    }


def literature_node(state):
    understanding = state["understanding"]
    plan = state["plan"]
    # If a gene was resolved, constrain the literature plan to the resolved
    # entity while retaining the user's other AMR entities.
    entities = dict(plan.get("entities") or {})
    resolved_genes = [r.get("selected", {}).get("canonical_name") for r in understanding.get("resolutions", []) if r.get("selected")]
    if resolved_genes:
        entities["genes"] = list(dict.fromkeys(resolved_genes))
    retrieval_plan = dict(plan)
    retrieval_plan["entities"] = entities
    mutation_ref = state.get("mutation_reference") or {}
    # Use the COMPLETE reference set (not the 20 displayed rows) to recognise
    # relevant papers.
    ref_records = records_for_matching(mutation_ref)
    if ref_records:
        retrieval_plan["mutation_reference_mutations"] = [
            r.get("mutation") for r in ref_records if r.get("mutation")
        ]
        retrieval_plan["mutation_reference_reported"] = [
            r.get("reported_mutation") for r in ref_records if r.get("reported_mutation")
        ]
    request = LiteratureResearchRequest(
        query=state["query"],
        entities=entities,
        intent=list(retrieval_plan.get("intent") or []),
        mutation_reference_mutations=retrieval_plan.get("mutation_reference_mutations", []),
        mutation_reference_reported=retrieval_plan.get("mutation_reference_reported", []),
        options={k: v for k, v in retrieval_plan.items() if k not in {"query", "entities", "intent", "mutation_reference_mutations", "mutation_reference_reported"}},
    )
    literature = run_literature_research(request).as_dict()
    results = [{"literature": literature, "genes": entities.get("genes", []), "query": state["query"]}]
    return {
        "literature_results": results,
        "execution_trace": _trace(state, "Literature Research Agent", {
            "sources": (literature.get("source_summary") or {}).get("supplemental", {}).get("providers", []) + ["PubMed / NCBI E-utilities"],
            "records": literature.get("candidate_papers_retrieved", 0),
            "retrieval_status": (literature.get("retrieval") or {}).get("status"),
        }),
    }


def pmc_node(state):
    literature_results = state.get("literature_results", [])
    paper_count = sum(len(item.get("literature", {}).get("papers", [])) for item in literature_results)
    if not AMR_PMC_ENRICHMENT_ENABLED and paper_count > 3:
        return {"literature_results": literature_results}
    results = enrich_with_pmc(literature_results, max_articles=3)
    return {
        "literature_results": results,
        "execution_trace": _trace(
            state,
            "PMC Evidence Agent",
            {"source": "NCBI PMC BioC API"},
        ),
    }


def evidence_node(state):
    e = analyze_literature(
        state.get("literature_results", []), state["understanding"],
        mutation_reference=state.get("mutation_reference"),
    )
    e["mutation_reference"] = state.get("mutation_reference", {})
    e["entity_normalization"] = {
        "organisms": state["understanding"].get("organism_resolutions", []),
        "antibiotics": state["understanding"].get("antibiotic_resolutions", []),
    }
    return {"evidence": e, "execution_trace": _trace(state, "Evidence Analysis Agent", {"literature_sets": len(e.get("literature_evidence", []))})}


def response_node(state):
    refs = []
    for item in state.get("literature_results", []):
        refs.extend(item.get("literature", {}).get("references", []))
    answer = synthesize_response(state["query"], state["understanding"], state.get("evidence", {}), refs)
    return {"answer": answer, "execution_trace": _trace(state, "Response Synthesis Agent", {"grounded": True})}


def report_node(state):
    report = generate_report(
        state["query"], state["understanding"], state["plan"],
        state.get("literature_results", []), state.get("evidence", {}),
        state.get("execution_trace", []), state.get("answer", ""),
    )
    return {"report": report, "execution_trace": report.get("execution_trace", state.get("execution_trace", []))}


def pending_node(state):
    report = generate_report(
        state["query"], state["understanding"], state["plan"], [],
        {"literature_evidence": []},
        _trace(state, "Selection Gate", {"action": "awaiting_user_selection"}),
        "A validated reference match is ambiguous. Select a candidate before literature retrieval continues.",
    )
    return {"report": report, "execution_trace": report.get("execution_trace", [])}


def out_of_scope_node(state):
    report = generate_report(
        state["query"], state["understanding"], state["plan"], [],
        {"literature_evidence": [], "reference_evidence": []},
        _trace(state, "Scope Gate", {"status": "out_of_scope"}),
        "The submitted query was not identified as an antimicrobial resistance (AMR) question. "
        "The AMR Research Assistant supports questions regarding resistance genes, genetic variants "
        "and mutations, antibiotics, resistant organisms/pathogens, resistance mechanisms, and "
        "genotype-phenotype relationships. Please rephrase your query with relevant AMR concepts.",
    )
    return {"report": report, "execution_trace": report.get("execution_trace", [])}


workflow = StateGraph(ResearchState)
workflow.add_node("query_understanding", query_understanding_node)
workflow.add_node("entity_normalization", entity_normalization_node)
workflow.add_node("planner", planner_node)
workflow.add_node("mutation_reference", mutation_reference_node)
workflow.add_node("literature", literature_node)
workflow.add_node("pmc", pmc_node)
workflow.add_node("evidence", evidence_node)
workflow.add_node("response", response_node)
workflow.add_node("report", report_node)
workflow.add_node("pending", pending_node)
workflow.add_node("out_of_scope", out_of_scope_node)
workflow.add_edge(START, "query_understanding")
workflow.add_edge("query_understanding", "entity_normalization")
workflow.add_edge("entity_normalization", "planner")
workflow.add_conditional_edges(
    "planner",
    route_after_planning,
    {
        "pending": "pending",
        "out_of_scope": "out_of_scope",
        "mutation_reference": "mutation_reference",
        "literature": "literature",
    },
)
workflow.add_edge("mutation_reference", "literature")
workflow.add_edge("literature", "pmc")
workflow.add_edge("pmc", "evidence")
workflow.add_edge("evidence", "response")
workflow.add_edge("response", "report")
workflow.add_edge("pending", END)
workflow.add_edge("report", END)
research_graph = workflow.compile()


def process_query(query, selected_candidate=None):
    started = time.perf_counter()
    result = research_graph.invoke({"query": query, "selected_candidate": selected_candidate})
    report = result["report"]
    report["latency"] = {
        "total_seconds": round(time.perf_counter() - started, 3),
        "budget_seconds": 7,
        "within_budget": (time.perf_counter() - started) <= 7,
    }
    return report
