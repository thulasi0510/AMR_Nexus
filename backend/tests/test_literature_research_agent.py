from agents.literature_research_agent import LiteratureResearchRequest, research
import agents.literature_research_agent as literature_agent
import agents.source_adapters as adapters


def test_literature_agent_returns_structured_report(monkeypatch):
    captured = {}

    def fake_run(plan):
        captured.update(plan)
        return {
            "title": "Multi-source Literature Evidence",
            "papers": [],
            "retrieval": {"status": "no_pubmed_records"},
            "source_summary": {"primary": "PubMed / NCBI E-utilities"},
        }

    monkeypatch.setattr(literature_agent, "run_literature", fake_run)
    request = LiteratureResearchRequest(
        query="Is rpoB S450L associated with rifampicin resistance in Mycobacterium tuberculosis?",
        entities={"genes": ["rpoB"], "organisms": ["Mycobacterium tuberculosis"]},
        intent=["mutation_variant_information"],
    )
    report = research(request).as_dict()
    assert report["agent"] == "Literature Research Agent"
    assert report["agent_contract_version"] == "1.0"
    assert captured["entities"]["genes"] == ["rpoB"]
    assert report["retrieval"]["status"] == "no_pubmed_records"


def test_supplemental_dedup_preserves_provenance(monkeypatch):
    monkeypatch.setenv("AMR_ADDITIONAL_SOURCES", "1")
    paper_a = {"title": "Same paper", "pmid": "123", "doi": "10.1/x", "source": "Europe PMC", "source_provenance": ["Europe PMC"]}
    paper_b = {"title": "Same paper", "pmid": "123", "doi": "10.1/x", "source": "OpenAlex", "source_provenance": ["OpenAlex"]}
    monkeypatch.setattr(adapters, "_europe_pmc", lambda plan: [paper_a])
    monkeypatch.setattr(adapters, "_openalex", lambda plan: [paper_b])
    monkeypatch.setattr(adapters, "_crossref", lambda plan: [])
    papers, metadata = adapters.retrieve_supplemental({"query": "test", "entities": {}})
    assert len(papers) == 1
    assert papers[0]["source_provenance"] == ["Europe PMC", "OpenAlex"]
    assert metadata["researchgate"]["status"] == "metadata_only"
