"""Regression tests for PubMed/PMC retrieval, filtering and evidence tiers.

Run (from the backend folder):
    python test_regression_retrieval.py          # plain runner, prints a summary
    python -m pytest test_regression_retrieval.py -q   # if pytest is installed

All network access is replaced by in-process fakes (PubMed, PMC, NCBI Taxonomy,
ChEBI, Ollama), so the tests are deterministic and need no internet connection.
They exercise the FINAL pipeline (services.orchestrator.process_query ->
report), not only individual functions.

NOTE: these tests prove the logic against fake PubMed records. They do not prove
that live PubMed/PMC retrieval works; that must be checked separately.
"""
import contextlib
import json
import sys
import traceback

import requests

import agents.entity_normalization as entity_normalization
import agents.literature as literature
import agents.pmc_evidence as pmc_evidence
import agents.query_understanding as query_understanding
import agents.response_synthesis as response_synthesis
import services.orchestrator as orchestrator
from agents.entity_aliases import canonical_drug, canonical_organism, drug_hits, find_organisms, organism_assessment
from agents.mutation_matching import MutationIndex, parse_substitution
from agents.mutation_reference import retrieve_mutations

QUERY = "Which mutations in rpoB are associated with rifampicin resistance in Mycobacterium tuberculosis?"


# ---------------------------------------------------------------- fixtures --
def paper(pmid, title, abstract, **extra):
    base = {
        "pmid": pmid, "title": title, "abstract": abstract, "authors": ["Tester A"],
        "journal": "J Test", "year": "2022", "pmcid": None, "doi": None,
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "doi_url": None,
        "publication_types": [], "mesh_terms": [], "source": "PubMed",
    }
    base.update(extra)
    return base


RELEVANT_ABBREV = paper(
    "1001", "Rpob mutations and rifampin resistance in M. tuberculosis clinical isolates",
    "We sequenced rpoB in 300 M. tuberculosis isolates. The Ser531Leu substitution (S450L in "
    "M. tuberculosis numbering) and His526Tyr were most frequent among rifampin-resistant isolates, "
    "whereas Leu511Pro conferred low-level rifampin resistance.",
)
RELEVANT_MTB = paper(
    "1002", "Mtb rpoB codon 435 changes and rifampicin MICs",
    "Mtb isolates carrying rpoB D435V showed high-level rifampicin resistance. Mutation analysis by WGS.",
)
SAUREUS = paper(
    "2001", "Rifampicin resistance mutations in Staphylococcus aureus rpoB",
    "Staphylococcus aureus rpoB mutations (H481N, S486L) confer rifampicin resistance. "
    "We compare with Mycobacterium tuberculosis S450L.",
)
LEPRAE = paper(
    "2002", "Mycobacterium leprae rpoB mutations and rifampicin resistance",
    "Mycobacterium leprae isolates carried rpoB S456L substitutions associated with rifampicin resistance.",
)
ECOLI = paper(
    "2003", "rpoB mutations and rifampicin resistance in Escherichia coli",
    "Escherichia coli rpoB mutation S531L confers rifampicin resistance, as in Mycobacterium tuberculosis.",
)
EPIDEMIOLOGY = paper(
    "3001", "Tuberculosis epidemiology in Peru",
    "Mycobacterium tuberculosis rifampicin resistance prevalence and surveillance.",
)
# Abstract is about another organism; the queried organism is only cited in the full text.
FULLTEXT_ONLY_MENTION = paper(
    "2004", "Rifampicin resistance and rpoB mutations in Staphylococcus epidermidis",
    "Staphylococcus epidermidis rpoB S486L mutation was associated with rifampicin resistance.",
    full_text=("Mycobacterium tuberculosis rpoB S450L mutation is the classic rifampicin resistance change. " * 4),
)


@contextlib.contextmanager
def pipeline_env(papers=None, count_error=None, pmc_status="not_available", esearch_payload=None):
    """Patch every network dependency of the pipeline."""
    papers = list(papers or [])
    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def fake_count(query):
        if count_error:
            raise count_error
        return len(papers)

    def fake_fetch(query, limit=None, sort="relevance"):
        if count_error:
            raise count_error
        return [dict(p) for p in papers]

    patch(query_understanding, "_llm_understand", lambda _q: {})
    patch(literature, "fetch_pubmed_count", fake_count)
    patch(literature, "fetch_papers", fake_fetch)
    patch(pmc_evidence, "fetch_full_text_with_status", lambda pmid=None, pmcid=None: {"status": pmc_status})
    patch(entity_normalization, "normalize_organism", lambda name: {"status": "unavailable", "query": name})
    patch(entity_normalization, "best_match", lambda name: {"status": "not_found", "query": name, "results": []})
    patch(response_synthesis.requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("no LLM in tests")))
    patch(literature, "retrieve_supplemental", lambda _p: ([], {"enabled": False, "providers": [], "provider_status": {}, "errors": []}))
    try:
        yield
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


def run(query=QUERY, **env):
    with pipeline_env(**env):
        return orchestrator.process_query(query)


def pmids(report):
    return {str(r.get("pmid")) for r in report["references"]}


def table_mutations(answer):
    rows = [ln for ln in answer.splitlines() if ln.startswith("| ") and not ln.startswith("| Mutation") and not ln.startswith("|---")]
    return [r.split("|")[1].strip() for r in rows]


# ------------------------------------------------------------------- tests --
def test_relevant_papers_found_with_abbreviations_aliases_and_notation():
    report = run(papers=[RELEVANT_ABBREV, RELEVANT_MTB, SAUREUS, LEPRAE, ECOLI, EPIDEMIOLOGY])
    assert report["status"] == "success"
    assert pmids(report) == {"1001", "1002"}, pmids(report)
    answer = report["answer"]
    # E. coli numbering and 3-letter notation are mapped onto the reference's own notation
    assert "S450L (written Ser531Leu; E. coli numbering)" in answer
    assert "H445Y (written His526Tyr; E. coli numbering)" in answer
    # source classification is preserved, not rewritten by the paper's wording
    assert "[WHO TB Mutation Catalogue 2023: resistance-associated]" in answer
    for bad in ("Staphylococcus", "leprae", "Escherichia", "Peru", "2001", "2002", "2003"):
        assert bad not in answer, bad
    supporting = report["literature_evidence"][0]["direct_evidence"]
    assert {p["pmid"] for p in supporting} == {"1001", "1002"}
    assert all(p["evidence_alignment"] == "mutation_aligned" and p["evidence_specificity"] == "exact" for p in supporting)
    basis = {p["pmid"]: p["evidence_basis"] for p in supporting}
    assert basis["1002"]["organism_status"] == "queried" and basis["1002"]["drug_in_title_or_abstract"]


def test_unrelated_organism_papers_are_never_direct_evidence():
    report = run(papers=[SAUREUS, LEPRAE, ECOLI, FULLTEXT_ONLY_MENTION, EPIDEMIOLOGY])
    assert report["references"] == []
    lit = report["literature_results"][0]["literature"]
    assert lit["retrieval"]["status"] == "no_matching_evidence"
    assert lit["papers"] == []
    for item in report["literature_evidence"]:
        assert item["direct_evidence"] == []
    reasons = lit["retrieval"]["filter_rejection_reasons"]
    assert reasons.get("organism:other_organism", 0) >= 3, reasons
    assert "Staphylococcus" not in report["answer"] and "leprae" not in report["answer"]


def test_full_text_mention_cannot_make_other_organism_paper_direct():
    from agents.evidence_analysis import _paper_alignment
    plan = {
        "entities": {"genes": ["rpoB"], "antibiotics": ["rifampicin"], "organisms": ["Mycobacterium tuberculosis"]},
        "intent": ["mutation_variant_information"],
        "mutation_reference_mutations": ["S450L"],
    }
    alignment, specificity = _paper_alignment(dict(FULLTEXT_ONLY_MENTION), plan)
    assert alignment != "mutation_aligned" and specificity == "other_organism", (alignment, specificity)
    # Incidental comparison inside a different organism's abstract is rejected too.
    for p in (SAUREUS, ECOLI):
        alignment, specificity = _paper_alignment(dict(p), plan)
        assert alignment != "mutation_aligned" and specificity == "other_organism"


def test_mixed_and_unstated_organism_are_not_direct():
    mixed = paper("4001", "rpoB mutations and rifampicin resistance in two pathogens",
                  "Mycobacterium tuberculosis and Mycobacterium bovis isolates carried rpoB S450L conferring rifampicin resistance.")
    unstated = paper("4002", "rpoB S450L and rifampicin resistance", "The rpoB S450L mutation confers rifampicin resistance.")
    report = run(papers=[mixed, unstated])
    assert report["references"] == []
    status = {o["status"] for o in (organism_assessment(mixed, ["Mycobacterium tuberculosis"]),)}
    assert status == {"mixed_organism"}
    assert organism_assessment(unstated, ["Mycobacterium tuberculosis"])["status"] == "not_stated"


def test_mesh_and_disease_term_establish_organism_only_when_nothing_competes():
    mesh = paper("4003", "rpoB S450L and rifampicin resistance", "The rpoB S450L mutation confers rifampicin resistance.",
                 mesh_terms=["Mycobacterium tuberculosis", "Rifampin"])
    disease = paper("4004", "Rifampicin-resistant tuberculosis and rpoB mutations",
                    "In rifampicin-resistant tuberculosis, the rpoB S450L mutation predominates.")
    competing = paper("4005", "Rifampicin-resistant Staphylococcus aureus rpoB mutations",
                      "Tuberculosis programmes also use rifampicin. Staphylococcus aureus rpoB S486L mutation was found.")
    report = run(papers=[mesh, disease, competing])
    assert pmids(report) == {"4003", "4004"}, pmids(report)


def test_mutation_outside_displayed_top20_is_still_recognised():
    u = {"entities": {"genes": ["rpoB"], "antibiotics": ["rifampicin"], "organisms": ["Mycobacterium tuberculosis"], "mutations": []},
         "query": QUERY, "organism_resolutions": []}
    ref = retrieve_mutations(u)
    shown = {r["mutation"] for r in ref["mutations"]}
    hidden = [r for r in ref["all_mutations"][20:]
              if r.get("organism_specific") and r.get("reported_mutation") and parse_substitution(r["mutation"])]
    assert hidden, "expected organism-specific records beyond the display limit"
    target = hidden[0]
    assert target["mutation"] not in shown
    # Written in E. coli numbering: only the COMPLETE reference index can map this back
    # to the catalogue entry (and its WHO classification); a top-20-only allowlist cannot.
    ref_aa, pos, alt_aa = parse_substitution(target["mutation"])
    inv = {"A": "Ala", "R": "Arg", "N": "Asn", "D": "Asp", "C": "Cys", "Q": "Gln", "E": "Glu", "G": "Gly", "H": "His",
           "I": "Ile", "L": "Leu", "K": "Lys", "M": "Met", "F": "Phe", "P": "Pro", "S": "Ser", "T": "Thr", "W": "Trp",
           "Y": "Tyr", "V": "Val"}
    written = f"{inv[ref_aa]}{pos + 81}{inv[alt_aa]}"
    p = paper("5001", "rpoB variants in M. tuberculosis",
              f"In M. tuberculosis, the rpoB {written} substitution was linked to rifampin resistance.")
    report = run(papers=[p])
    assert pmids(report) == {"5001"}, (target["mutation"], report["answer"][-600:])
    expected = f"{target['mutation']} (written {written}; E. coli numbering) [WHO TB Mutation Catalogue 2023:"
    assert expected in report["answer"], (expected, report["answer"][-700:])


def test_reference_data_not_truncated_but_display_limit_kept():
    report = run(papers=[])
    mr = report["mutation_reference"]
    assert len(mr["mutations"]) == 20 and mr["display_limit"] == 20
    assert len(mr["all_mutations"]) == mr["total_matching_records"] > 20
    assert mr["truncated_records"] == mr["total_matching_records"] - 20
    assert len(table_mutations(report["answer"])) == 20
    first = table_mutations(report["answer"])
    assert "S450L" in first and "H445Y" in first and "R445Y" not in report["answer"]
    assert f"Showing 20 of {mr['total_matching_records']}" in report["answer"]
    # WHO classifications preserved
    classes = {r["mutation"]: r["confidence"] for r in mr["mutations"]}
    assert classes["S450L"] == "1) Assoc w R"
    assert all(r["source"] == "WHO TB Mutation Catalogue 2023" for r in mr["mutations"])
    assert not any("not associated" in str(r["phenotype"]).lower() or "uncertain" in str(r["phenotype"]).lower() for r in mr["all_mutations"] if r.get("organism_specific"))


def test_no_duplicate_rows_and_literature_does_not_duplicate_reference_rows():
    report = run(papers=[RELEVANT_ABBREV, RELEVANT_MTB])
    muts = table_mutations(report["answer"])
    assert len(muts) == len(set(muts)) == 20
    assert report["answer"].count("| S450L |") == 1
    assert report["answer"].count("| Mutation |") == 1


def test_entities_are_normalised_and_deduplicated():
    with pipeline_env():
        u = query_understanding.understand_query(
            "Which mutations in rpoB are associated with rifampin (RIF) resistance in M. tuberculosis (Mtb)?")
        u = entity_normalization.normalize_entities(u)
    e = u["entities"]
    assert e["antibiotics"] == ["rifampicin"], e
    assert e["organisms"] == ["Mycobacterium tuberculosis"], e
    assert canonical_drug("Rifampin") == canonical_drug("rifampicin") == "rifampicin"
    assert canonical_organism("m. tuberculosis") == canonical_organism("MTB") == "Mycobacterium tuberculosis"
    assert "rifampin" in u["entity_aliases"]["antibiotics"]["rifampicin"]
    assert "mtb" in u["entity_aliases"]["organisms"]["Mycobacterium tuberculosis"]


def test_drug_and_organism_matching_is_word_bounded():
    assert drug_hits("RIF-resistant isolates", "rifampin")
    assert not drug_hits("Xpert MTB/RIF assay performance", "rifampicin")
    assert not drug_hits("rifampicinoid", "rifampicin")
    assert find_organisms("Mycobacterium tuberculosis and M. leprae").get("Mycobacterium tuberculosis") == 1
    assert find_organisms("nontuberculous mycobacteria") == {}


def test_pubmed_queries_use_aliases_and_are_well_formed():
    with pipeline_env():
        report = orchestrator.process_query(QUERY)
    queries = report["literature_results"][0]["literature"]["search_queries"]
    joined = " ".join(queries)
    for expected in ('"rifampin"', 'mtb[Title/Abstract]', '"m tuberculosis"[Title/Abstract]', '"Mycobacterium tuberculosis"[MeSH Terms]'):
        assert expected in joined, expected
    assert "m. tuberculosis[Title/Abstract]" not in joined  # old malformed term
    assert joined.count('"rifampicin"') >= 1 and len(queries) == len(set(queries))
    # a named mutation is searched in every accepted notation (incl. other numbering)
    with pipeline_env():
        specific = orchestrator.process_query("Is S450L in rpoB associated with rifampicin resistance in Mycobacterium tuberculosis?")
    sq = " ".join(specific["literature_results"][0]["literature"]["search_queries"])
    for form in ("S450L[Title/Abstract]", "Ser450Leu[Title/Abstract]", "S531L[Title/Abstract]", "Ser531Leu[Title/Abstract]"):
        assert form in sq, form


def test_specific_mutation_query_requires_that_mutation_in_any_notation():
    other = paper("6001", "rpoB D435V in M. tuberculosis", "In M. tuberculosis rpoB D435V mutation conferred rifampin resistance.")
    hit = paper("6002", "rpoB Ser531Leu in M. tuberculosis", "In M. tuberculosis the rpoB Ser531Leu substitution conferred rifampin resistance.")
    report = run("Is S450L in rpoB associated with rifampicin resistance in Mycobacterium tuberculosis?", papers=[other, hit])
    assert pmids(report) == {"6002"}, pmids(report)


def test_retrieval_failure_is_distinct_from_no_matching_evidence():
    failed = run(count_error=requests.ConnectionError("network down"))
    lit = failed["literature_results"][0]["literature"]
    assert lit["retrieval"]["status"] == "failed" and lit["evidence_level"] == "retrieval_failed"
    assert "Literature check incomplete" in failed["answer"]
    assert "not evidence that no supporting papers exist" not in failed["answer"]
    assert "none reported a mutation" not in failed["answer"]
    assert len(table_mutations(failed["answer"])) == 20  # deterministic reference answer still delivered

    nothing = run(papers=[])
    assert nothing["literature_results"][0]["literature"]["retrieval"]["status"] == "no_pubmed_records"
    assert "returned no records" in nothing["answer"] and "retrieval failed" not in nothing["answer"]

    unmatched = run(papers=[SAUREUS, EPIDEMIOLOGY])
    assert unmatched["literature_results"][0]["literature"]["retrieval"]["status"] == "no_matching_evidence"
    assert "No paper met the mutation-specific evidence criteria" in unmatched["answer"] and "retrieval failed" not in unmatched["answer"]


def test_ncbi_error_body_is_a_failure_not_zero_results():
    class FakeResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"error": "API rate limit exceeded"}
    saved_get, saved_sleep = literature.requests.get, literature.time.sleep
    literature.requests.get = lambda *a, **k: FakeResponse()
    literature.time.sleep = lambda *_: None
    try:
        result = literature.search_literature({
            "entities": {"genes": ["rpoB"], "antibiotics": ["rifampicin"], "organisms": ["Mycobacterium tuberculosis"]},
            "intent": ["mutation_variant_information"],
        })
    finally:
        literature.requests.get, literature.time.sleep = saved_get, saved_sleep
    assert result["retrieval"]["status"] == "failed" and result["retrieval"]["queries_failed"] == result["retrieval"]["queries_attempted"] > 0


def test_partial_failure_is_reported_not_hidden():
    calls = {"n": 0}
    def flaky(query):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise requests.ConnectionError("blip")
        return 2
    with pipeline_env(papers=[RELEVANT_MTB, SAUREUS]):
        saved = literature.fetch_pubmed_count
        literature.fetch_pubmed_count = flaky
        try:
            report = orchestrator.process_query(QUERY)
        finally:
            literature.fetch_pubmed_count = saved
    assert report["literature_results"][0]["literature"]["retrieval"]["status"] == "partial_failure"
    assert pmids(report) == {"1002"}
    assert "searches failed" in report["answer"]


def test_pmc_not_available_is_not_counted_as_error():
    with_pmc = dict(RELEVANT_MTB, pmcid="PMC0000001")
    report = run(papers=[with_pmc], pmc_status="not_available")
    pmc = report["literature_evidence"][0]["pmc"]
    assert {k: pmc[k] for k in ("attempted", "retrieved", "not_available", "errors")} == {"attempted": 1, "retrieved": 0, "not_available": 1, "errors": 0}, pmc
    report = run(papers=[with_pmc], pmc_status="error")
    assert report["literature_evidence"][0]["pmc"]["errors"] == 1
    assert "Full-text retrieval failed for 1 article" in report["answer"]


def test_llm_cannot_inject_entities_or_mutations():
    saved = query_understanding._llm_understand
    query_understanding._llm_understand = lambda _q: {
        "domain": "amr", "intent": ["mutation_variant_information"], "genes": ["gyrA"],
        "antibiotics": ["vancomycin"], "organisms": ["Bacillus subtilis"],
        "mutations": ["R445Y", "S450L"], "variants": ["K1000X"], "mechanisms": ["efflux pump"],
    }
    try:
        u = query_understanding.understand_query("Is Ser450Leu in rpoB associated with rifampin resistance in M. tuberculosis?")
    finally:
        query_understanding._llm_understand = saved
    e = u["entities"]
    assert e["genes"] == ["rpoB"] and e["antibiotics"] == ["rifampicin"]
    assert e["organisms"] == ["Mycobacterium tuberculosis"] and e["mutations"] == ["S450L"]
    assert e["variants"] == [] and e["mechanisms"] == []


def test_llm_answer_with_invented_mutation_is_discarded():
    understanding = {"entities": {"genes": ["rpoB"], "organisms": ["Mycobacterium tuberculosis"], "mutations": []},
                     "original_query": "rpoB", "intent": ["resistance_mechanism"], "scope_ok": False}
    evidence = {"mutation_reference": {"all_mutations": [{"mutation": "S450L"}], "mutations": [{"mutation": "S450L"}]}}
    assert response_synthesis._validate_llm_text("S450L and Ser531Leu are reported [1].", understanding, evidence, [{}])
    assert not response_synthesis._validate_llm_text("R445Y confers resistance.", understanding, evidence, [{}])
    assert not response_synthesis._validate_llm_text("See [7].", understanding, evidence, [{}])


def test_non_substitution_and_numbering_index():
    idx = MutationIndex(["S450L", "p.Asp435del"], ["rpoB"], ["Mycobacterium tuberculosis"])
    assert idx.lookup(("S", 531, "L")) == ("S450L", "E. coli numbering")
    assert idx.lookup(("S", 450, "L"))[0] == "S450L"
    assert idx.lookup(("S", 531, "W"))[0] is None
    # No numbering scheme for other gene/organism pairs
    assert MutationIndex(["S83L"], ["gyrA"], ["Escherichia coli"]).lookup(("S", 164, "L"))[0] is None


# ---- cases taken from a real live-PubMed run (E. coli / gyrA / fluoroquinolone) ----------
GYRA_PLAN = {
    "entities": {"genes": ["gyrA"], "antibiotics": ["fluoroquinolone"], "organisms": ["Escherichia coli"]},
    "intent": ["mutation_variant_information"], "mutation_reference_mutations": [],
}


def accepted(p, plan=GYRA_PLAN):
    from agents.mutation_matching import assess_paper
    a = assess_paper(p, plan)
    return a["eligible"], [m["mutation"] for m in a["reference_matches"] + a["other_substitutions"]]


def test_mutations_are_attributed_to_the_queried_gene_only():
    ecoli = "Escherichia coli"
    # gyrA and parC substitutions in one sentence
    p = paper("a1", f"Genomes of {ecoli}", f"Twenty-eight {ecoli} genomes showed a gyrA S83L substitution, and 12 of those 28 harbored "
              "double-serine mutations gyrA S83L and parC S80I, correlating with in vitro quinolone-resistances.")
    assert accepted(p) == (True, ["S83L"])
    # "X/Y in ParC and Z/W in GyrA"
    p = paper("a2", f"Ciprofloxacin-resistant {ecoli}", "Coexisting amino acid mutations of Thr57Ser/Ser80Arg in ParC and "
              f"Ser83Phe/Asp87Gly in GyrA might cause fluoroquinolone resistance in {ecoli}.")
    assert accepted(p) == (True, ["S83F", "D87G"])
    # "GyrA:S83L + D87N; ParC:S80I + E84V"
    p = paper("a3", f"Ciprofloxacin-resistant {ecoli}", f"The ciprofloxacin-resistant {ecoli} harbored a pattern (GyrA:S83L + D87N; ParC:S80I + E84V).")
    assert accepted(p) == (True, ["S83L", "D87N"])
    # gyrA gene (...) and gyrB (...)
    p = paper("a4", f"Quinolone resistance in {ecoli}", "Chromosomal point mutations were found in gyrA gene (Ser83Leu and Asp87Asn) and gyrB "
              f"(Gln434His, Lys444Arg and Gly435Val). Ciprofloxacin MICs were high in {ecoli}.")
    assert accepted(p) == (True, ["S83L", "D87N"])
    # gene-prefixed variant and a gene-less follow-up sentence still count
    p = paper("a5", f"gyrA in {ecoli}", f"In {ecoli}, gyrA mutations confer ciprofloxacin resistance. The Ser83Leu and Asp87Asn substitutions were most frequent.")
    assert accepted(p) == (True, ["S83L", "D87N"])
    p = paper("a6", f"Gyrase resistance in {ecoli}", f"Treatment of {ecoli} with ciprofloxacin enriched GyrA-A119E, a gyrA variant.")
    assert accepted(p) == (True, ["A119E"])
    # only other genes' substitutions -> nothing attributable to gyrA
    p = paper("a7", f"parC in {ecoli}", f"A novel parC mutation Lys154Arg was found in ciprofloxacin-resistant {ecoli}; gyrA was wild type.")
    assert accepted(p) == (False, [])


def test_title_naming_queried_and_another_organism_is_mixed_not_direct():
    p = paper("b1", "Mutation-based fluoroquinolone resistance in Acinetobacter baumannii and Escherichia coli isolates",
              "The specific substitutions observed in GyrA were Cys173Arg and Cys174Gly. Fluoroquinolone resistance in E. coli.")
    assert organism_assessment(p, ["Escherichia coli"])["status"] == "mixed_organism"
    assert accepted(p)[0] is False
    report = run("Which mutations in gyrA are associated with fluoroquinolone resistance in Escherichia coli?", papers=[p])
    assert report["references"] == [] and "Cys173Arg" not in report["answer"] and "C173R" not in report["answer"]


def test_pmc_classification_not_available_vs_real_error():
    import services.pmc_client as pc

    class R:
        def __init__(self, code, body=None, text=""):
            self.status_code, self._body, self.text = code, body, text
        def json(self):
            if self._body is None:
                raise ValueError("not json")
            return self._body
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}")

    saved = pc.requests.get
    try:
        pc.requests.get = lambda *a, **k: R(200, text="No result can be found.")
        assert pc.fetch_full_text_with_status(pmcid="PMC1")["status"] == "not_available"
        pc.requests.get = lambda *a, **k: R(404)
        assert pc.fetch_full_text_with_status(pmcid="PMC1")["status"] == "not_available"
        pc.requests.get = lambda *a, **k: R(503)
        assert pc.fetch_full_text_with_status(pmcid="PMC1")["status"] == "error"
        pc.requests.get = lambda *a, **k: R(429)
        assert pc.fetch_full_text_with_status(pmcid="PMC1")["status"] == "error"
        def boom(*a, **k): raise requests.ConnectionError("down")
        pc.requests.get = boom
        assert pc.fetch_full_text_with_status(pmcid="PMC1")["status"] == "error"
        body = [{"documents": [{"passages": [{"text": "rpoB S450L full text."}]}]}]
        pc.requests.get = lambda *a, **k: R(200, body=body)
        got = pc.fetch_full_text_with_status(pmcid="PMC1")
        assert got["status"] == "retrieved" and "S450L" in got["text"]
    finally:
        pc.requests.get = saved


def test_pmc_is_not_called_for_records_without_a_pmcid():
    calls = []
    saved = pmc_evidence.fetch_full_text_with_status
    pmc_evidence.fetch_full_text_with_status = lambda pmid=None, pmcid=None: calls.append(pmcid) or {"status": "not_available"}
    try:
        out = pmc_evidence.enrich_with_pmc([{"literature": {"papers": [dict(RELEVANT_MTB), dict(RELEVANT_ABBREV, pmcid="PMC9")]}}])
    finally:
        pmc_evidence.fetch_full_text_with_status = saved
    lit = out[0]["literature"]
    assert calls == ["PMC9"] and lit["pmc_attempted"] == 1 and lit["pmc_not_available"] == 2 and lit["pmc_errors"] == 0


def test_specific_mutation_question_gets_a_direct_answer_and_no_variant_as_gene():
    report = run("Does rpoB D435V confer rifampicin resistance in Mycobacterium tuberculosis?", papers=[RELEVANT_MTB])
    head = report["answer"].split("\n")[0]
    assert head.startswith("WHO TB Mutation Catalogue 2023 classifications for rpoB")
    assert "D435V" in report["answer"] and "resistance-associated" in report["answer"]
    assert "Several mutations in" not in report["answer"]
    # a resolver returning the variant as a "gene" must not be displayed as one
    import agents.response_synthesis as rs
    understanding = {"entities": {"genes": ["D435V"], "antibiotics": ["rifampicin"], "organisms": ["Mycobacterium tuberculosis"], "mutations": []}}
    evidence = {"mutation_reference": {"mutations": [{"mutation": "S450L", "phenotype": "resistance-associated", "confidence": "1) Assoc w R",
                "source": "WHO", "reported_variant": "rpoB_p.Ser450Leu", "organism_specific": True}], "total_matching_records": 1}}
    text = rs._mutation_focused_response("q", understanding, evidence, [])
    assert "rpoB" in text and "in D435V" not in text


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    failures = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} regression tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
