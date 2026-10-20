"""Tests for mechanism-focused query understanding and PubMed query building.

Verifies that queries like "efflux pump", "porins", "promoter region",
"MexAB-OprM" correctly:
  1. Are classified as AMR-in-scope
  2. Get the resistance_mechanism intent
  3. Produce non-trivial PubMed queries with mechanism terms in them
  4. Pass mechanism terms to supplemental providers
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.query_understanding import understand_query, _fallback_understanding
from agents.literature import build_pubmed_queries, _mechanism_pubmed_clause
from agents.source_adapters import _query_text


class TestMechanismQueryUnderstanding(unittest.TestCase):

    def _understand(self, q):
        return understand_query(q)

    def test_efflux_pump_in_scope(self):
        plan = self._understand("efflux pump")
        self.assertTrue(plan["scope_ok"], "efflux pump should be AMR in-scope")
        self.assertIn("resistance_mechanism", plan["intent"])

    def test_porins_in_scope(self):
        plan = self._understand("porins")
        self.assertTrue(plan["scope_ok"], "porins should be AMR in-scope")
        self.assertIn("resistance_mechanism", plan["intent"])

    def test_promoter_region_in_scope(self):
        plan = self._understand("promoter region")
        self.assertTrue(plan["scope_ok"], "promoter region should be AMR in-scope")
        self.assertIn("resistance_mechanism", plan["intent"])

    def test_mexab_oprm_in_scope(self):
        plan = self._understand("MexAB-OprM")
        self.assertTrue(plan["scope_ok"], "MexAB-OprM should be AMR in-scope")

    def test_efflux_pump_mechanism_extracted(self):
        fb = _fallback_understanding("efflux pump")
        self.assertIn("efflux pump", fb.get("mechanisms", []))

    def test_porins_mechanism_extracted(self):
        fb = _fallback_understanding("porins")
        self.assertIn("porin", fb.get("mechanisms", []))

    def test_promoter_region_mechanism_extracted(self):
        fb = _fallback_understanding("promoter region")
        self.assertIn("promoter mutation", fb.get("mechanisms", []))


class TestMechanismPubMedClause(unittest.TestCase):

    def test_efflux_pump_clause(self):
        clause = _mechanism_pubmed_clause(["efflux pump"])
        self.assertIn("efflux", clause.lower())
        self.assertTrue(len(clause) > 10)

    def test_porin_clause(self):
        clause = _mechanism_pubmed_clause(["porin"])
        self.assertIn("porin", clause.lower())

    def test_promoter_clause(self):
        clause = _mechanism_pubmed_clause(["promoter mutation"])
        self.assertIn("promoter", clause.lower())

    def test_unknown_mechanism_falls_back(self):
        clause = _mechanism_pubmed_clause(["some novel mechanism"])
        self.assertTrue(len(clause) > 0)


class TestMechanismPubMedQueries(unittest.TestCase):

    def _plan_from_fb(self, query):
        fb = _fallback_understanding(query)
        fb["query"] = query
        return fb

    def _build(self, query):
        plan = self._plan_from_fb(query)
        return build_pubmed_queries(plan)

    def test_efflux_pump_query_built(self):
        queries = self._build("efflux pump")
        self.assertTrue(len(queries) >= 1, "Should build at least one query for 'efflux pump'")
        full = " ".join(queries).lower()
        self.assertIn("efflux", full)
        self.assertIn("resistance", full)

    def test_porins_query_built(self):
        queries = self._build("porins")
        self.assertTrue(len(queries) >= 1, "Should build at least one query for 'porins'")
        full = " ".join(queries).lower()
        self.assertIn("porin", full)

    def test_promoter_region_query_built(self):
        queries = self._build("promoter region")
        self.assertTrue(len(queries) >= 1)
        full = " ".join(queries).lower()
        self.assertIn("promoter", full)

    def test_mexab_oprm_query_built(self):
        queries = self._build("MexAB-OprM efflux pump")
        self.assertTrue(len(queries) >= 1)
        full = " ".join(queries).lower()
        self.assertIn("efflux", full)

    def test_no_query_is_trivially_empty(self):
        queries = self._build("efflux pump")
        for q in queries:
            self.assertGreater(len(q.strip()), 10, f"Query is trivially short: {q!r}")


class TestSupplementalQueryText(unittest.TestCase):

    def _plan(self, query, mechanisms=None):
        return {
            "query": query,
            "entities": {
                "genes": [],
                "antibiotics": [],
                "organisms": [],
                "mutations": [],
                "variants": [],
                "mechanisms": mechanisms or [],
            },
        }

    def test_mechanism_included_in_supplemental_query(self):
        plan = self._plan("efflux pump", mechanisms=["efflux pump"])
        text = _query_text(plan)
        self.assertIn("efflux pump", text.lower())

    def test_raw_query_fallback_when_no_entities(self):
        plan = self._plan("porins and resistance")
        text = _query_text(plan)
        self.assertGreater(len(text.strip()), 0)
        # Should fall back to raw query
        self.assertIn("porin", text.lower())

    def test_never_empty_query_text(self):
        plan = self._plan("")
        text = _query_text(plan)
        self.assertGreater(len(text.strip()), 0)


class TestPlannerSupplementalSources(unittest.TestCase):

    def test_planner_includes_doaj_and_arxiv(self):
        from agents.planner import create_plan
        understanding = {
            "domain": "amr",
            "scope_ok": True,
            "intent": ["resistance_mechanism"],
            "entities": {"genes": [], "antibiotics": ["meropenem"], "organisms": [], "mechanisms": ["efflux pump"]},
            "literature_needed": True,
        }
        plan = create_plan("efflux pump meropenem", understanding)
        sources = plan.get("sources", [])
        self.assertIn("DOAJ", sources)
        self.assertIn("arXiv (q-bio)", sources)
        self.assertIn("Europe PMC", sources)
        self.assertIn("OpenAlex", sources)


class TestMultiConceptUnderstanding(unittest.TestCase):

    def test_multi_concept_extraction(self):
        q = "efflux pump and porin mutations in Klebsiella pneumoniae"
        res = understand_query(q)
        concepts = res.get("entities", {}).get("scientific_concepts", [])
        keywords = res.get("entities", {}).get("query_keywords", [])
        self.assertTrue(any("efflux" in c.lower() for c in concepts) or "efflux pump" in res["entities"]["mechanisms"])
        self.assertTrue(any("porin" in c.lower() for c in concepts) or "porin" in res["entities"]["mechanisms"])
        self.assertIn("Klebsiella pneumoniae", res["entities"]["organisms"])


class TestSourceAdaptersAndFiltering(unittest.TestCase):

    def test_doaj_adapter_callable(self):
        from agents.source_adapters import _doaj
        import requests
        plan = {"query": "efflux pump resistance", "entities": {"mechanisms": ["efflux pump"]}}
        try:
            papers = _doaj(plan, limit=2)
            self.assertIsInstance(papers, list)
            if papers:
                self.assertEqual(papers[0]["source"], "DOAJ")
        except requests.RequestException:
            pass  # External network timeout handled

    def test_arxiv_adapter_callable(self):
        from agents.source_adapters import _arxiv
        import requests
        plan = {"query": "antimicrobial resistance genomics", "entities": {}}
        try:
            papers = _arxiv(plan, limit=2)
            self.assertIsInstance(papers, list)
            if papers:
                self.assertEqual(papers[0]["source"], "arXiv (q-bio)")
        except requests.RequestException:
            pass  # External network timeout handled

    def test_biorxiv_and_medrxiv_preprint_adapters(self):
        from agents.source_adapters import _biorxiv, _medrxiv
        import requests
        plan = {"query": "carbapenemase", "entities": {"genes": ["blaKPC"]}}
        try:
            bio_papers = _biorxiv(plan, limit=2)
            self.assertIsInstance(bio_papers, list)
            if bio_papers:
                self.assertEqual(bio_papers[0]["source"], "bioRxiv")
                self.assertIn("preprint", bio_papers[0].get("publication_types", []))
        except requests.RequestException:
            pass

        try:
            med_papers = _medrxiv(plan, limit=2)
            self.assertIsInstance(med_papers, list)
            if med_papers:
                self.assertEqual(med_papers[0]["source"], "medRxiv")
                self.assertIn("preprint", med_papers[0].get("publication_types", []))
        except requests.RequestException:
            pass

    def test_google_scholar_exploration_link(self):
        from agents.source_adapters import _google_scholar
        plan = {"query": "blaNDM-1 Klebsiella pneumoniae", "entities": {}}
        papers = _google_scholar(plan, limit=1)
        self.assertIsInstance(papers, list)
        self.assertEqual(len(papers), 1)
        scholar_record = papers[0]
        self.assertEqual(scholar_record["source"], "Google Scholar")
        self.assertIn("scholar.google.com/scholar?q=", scholar_record["url"])
        self.assertEqual(scholar_record["access_mode"], "direct_query_link (unrestricted access)")

    def test_non_amr_incidental_exclusion(self):
        from agents.evidence_analysis import _paper_alignment
        plan = {
            "entities": {"genes": ["blaNDM-1"], "antibiotics": ["meropenem"]},
            "intent": ["resistance_mechanism"],
        }
        cancer_paper = {
            "title": "Chemotherapy resistant ovarian carcinoma cell line studies",
            "abstract": "We studied carcinoma multidrug resistance pathways in human neoplasm lines without bacterial isolates.",
        }
        alignment, specificity = _paper_alignment(cancer_paper, plan)
        self.assertEqual(alignment, "excluded")
        self.assertEqual(specificity, "none")

    def test_preprints_tagged_in_report_references(self):
        from agents.report_generator import reconcile_citations, _ensure_references_section
        answer = "Initial findings in blaKPC [1] and peer-reviewed confirmation [2]."
        refs = [
            {"pmid": "", "doi": "10.1101/2023.01.01.123456", "title": "Early genomic preprint", "source": "bioRxiv", "publication_types": ["preprint"]},
            {"pmid": "99999", "doi": "10.1001/peer", "title": "Peer-reviewed validation", "source": "PubMed", "publication_types": ["peer-reviewed"]},
        ]
        reconciled_text, cited = reconcile_citations(answer, refs)
        final_text = _ensure_references_section(reconciled_text, cited)

        self.assertIn("[Preprint: bioRxiv]", final_text)
        self.assertIn("[Peer-reviewed]", final_text)
        self.assertEqual(cited[0]["publication_status"], "Preprint")
        self.assertEqual(cited[0]["preprint_server"], "bioRxiv")
        self.assertEqual(cited[1]["publication_status"], "Peer-reviewed")

    def test_stage_e_classification_buckets(self):
        from agents.evidence_analysis import analyze_literature
        plan_understanding = {
            "intent": ["resistance_mechanism"],
            "entities": {"genes": ["blaTEM"], "antibiotics": ["ampicillin"], "mechanisms": ["beta-lactamase"]},
        }
        mock_lit_results = [{
            "literature": {
                "source": "PubMed",
                "papers": [
                    {"pmid": "111", "title": "blaTEM beta-lactamase confers ampicillin resistance", "abstract": "TEM enzymes hydrolyze ampicillin."},
                    {"pmid": "222", "title": "Carcinoma chemotherapy resistance in humans", "abstract": "Human neoplasm cell line analysis."},
                ],
            }
        }]
        analysis = analyze_literature(mock_lit_results, plan_understanding)
        lit_evidence = analysis["literature_evidence"][0]
        self.assertEqual(len(lit_evidence["direct_evidence"]), 1)
        self.assertEqual(lit_evidence["direct_evidence"][0]["pmid"], "111")
        self.assertEqual(len(lit_evidence["excluded_evidence"]), 1)
        self.assertEqual(lit_evidence["excluded_evidence"][0]["pmid"], "222")


if __name__ == "__main__":
    unittest.main()
