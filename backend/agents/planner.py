"""AMR retrieval-planning agent.

Turns structured query understanding into a source/tool plan. The planner is
source-agnostic: adding Europe PMC, PMC, OpenAlex, etc. later means adding a
retrieval agent and registering it here, without rewriting query understanding.
"""


def _has_gene_resolution(understanding):
    return any(r.get("selected") for r in understanding.get("resolutions", []))


def create_plan(query, understanding):
    domain = understanding.get("domain", "uncertain")
    intents = understanding.get("intent", [])
    entities = understanding.get("entities", {})

    if domain == "out_of_scope" or not understanding.get("scope_ok", True):
        return {
            "query": query,
            "status": "out_of_scope",
            "agents_required": [],
            "sources": [],
            "reason": "The query was not identified as an AMR information request.",
        }

    sources = []
    if entities.get("organisms"):
        sources.append("NCBI Taxonomy")
    if entities.get("antibiotics"):
        sources.append("ChEBI")
    if understanding.get("needs_reference_lookup") and entities.get("genes"):
        sources.extend(["CARD", "NCBI AMRFinderPlus"])
    if entities.get("genes") or entities.get("mutations") or entities.get("variants"):
        sources.append("Mutation Reference")
        if (
            any("mycobacterium tuberculosis" in str(x).lower() for x in entities.get("organisms", []))
            or any("tb" == str(x).strip().lower() or "mtb" in str(x).lower() for x in entities.get("organisms", []))
        ):
            sources.append("WHO TB Mutation Catalogue")
    if understanding.get("literature_needed", True):
        sources.extend(["PubMed", "PMC BioC"])
        try:
            from config import AMR_ADDITIONAL_SOURCES
            if AMR_ADDITIONAL_SOURCES:
                sources.extend([
                    "Europe PMC", "bioRxiv", "medRxiv", "OpenAlex", "Crossref",
                    "Semantic Scholar", "Google Scholar", "DOAJ", "arXiv (q-bio)",
                ])
        except Exception:
            pass

    sources = list(dict.fromkeys(sources))

    agents = ["query_understanding", "entity_normalization"]
    if entities.get("genes") or entities.get("mutations") or entities.get("variants"):
        agents.extend(["gene_verification", "mutation_reference"])
    if "PubMed" in sources:
        agents.append("literature")
    if "PMC BioC" in sources:
        agents.append("pmc_evidence")
    agents.extend(["evidence_analysis", "response_synthesis"])

    if understanding.get("selection_required"):
        status = "pending_selection"
    else:
        status = "planned"

    return {
        "query": query,
        "domain": domain,
        "intent": intents,
        "entities": entities,
        "information_needed": understanding.get("information_needed", []),
        "original_query": understanding.get("original_query", query),
        "retrieval_mode": "query_level",
        "sources": sources,
        "agents_required": agents,
        "gene_resolution_available": _has_gene_resolution(understanding),
        "selection_required": understanding.get("selection_required", False),
        "recency_requested": understanding.get("recency_requested", False),
        "time_window": understanding.get("time_window"),
        "status": status,
    }
