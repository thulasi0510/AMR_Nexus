"""Universal AMR Query Understanding, Partial Gene Matching & Unified References Regression Suite."""
import unittest
import re
from agents.query_understanding import understand_query, _reference_grounded_gene_mentions
from agents.card_reference import search_card_candidates
from agents.report_generator import generate_report, reconcile_citations, _ensure_references_section
from agents.response_synthesis import synthesize_response
from services.orchestrator import process_query


class UniversalAMRQueryTests(unittest.TestCase):

    def test_clickable_example_query_understanding(self):
        """The clickable example query must be fully understood with correct entities and intent."""
        q = "Which mutations in rpoB are associated with rifampicin resistance in Mycobacterium tuberculosis?"
        u = understand_query(q)
        self.assertEqual(u["domain"], "amr")
        self.assertTrue(u["scope_ok"])
        self.assertIn("mutation_variant_information", u["intent"])
        self.assertTrue(any("rpob" in g.lower() for g in u["entities"]["genes"]))
        self.assertTrue(any("rifampicin" in d.lower() or "rifampin" in d.lower() for d in u["entities"]["antibiotics"]))
        self.assertTrue(any("mycobacterium tuberculosis" in o.lower() for o in u["entities"]["organisms"]))

    def test_short_query_gene_only_ompk(self):
        """Short query 'ompK' must be recognized as in-scope AMR and resolve plausible candidate genes (ompK35, ompK36)."""
        u = understand_query("ompK")
        self.assertEqual(u["domain"], "amr")
        self.assertTrue(u["scope_ok"])
        self.assertTrue(any("ompk" in g.lower() for g in u["entities"]["genes"]))
        # Plausible candidate variants must be identified without silent dropping
        cand_names = [c.get("canonical_name", "").lower() for c in u.get("gene_candidates", [])]
        self.assertTrue(any("ompk35" in c for c in cand_names), f"ompK35 missing from candidates: {cand_names}")
        self.assertTrue(any("ompk36" in c for c in cand_names), f"ompK36 missing from candidates: {cand_names}")

    def test_short_query_gene_only_katg(self):
        """Short query 'katG' must be recognized as in-scope AMR with inferred retrieval/mechanism intent."""
        u = understand_query("katG")
        self.assertEqual(u["domain"], "amr")
        self.assertTrue(u["scope_ok"])
        self.assertTrue(any("katg" in g.lower() for g in u["entities"]["genes"]))

    def test_short_query_gene_and_drug(self):
        """Short query 'vanA vancomycin' must extract both gene and drug and be in-scope."""
        u = understand_query("vanA vancomycin")
        self.assertEqual(u["domain"], "amr")
        self.assertTrue(u["scope_ok"])
        self.assertTrue(any("vana" in g.lower() for g in u["entities"]["genes"]))
        self.assertTrue(any("vancomycin" in d.lower() for d in u["entities"]["antibiotics"]))

    def test_short_query_mutation_alone(self):
        """Short mutation notation 'S315T' must be recognized with mutation_variant_information intent."""
        u = understand_query("S315T")
        self.assertEqual(u["domain"], "amr")
        self.assertTrue(u["scope_ok"])
        self.assertIn("mutation_variant_information", u["intent"])
        self.assertIn("S315T", u["entities"]["mutations"])

    def test_short_query_mechanism_alone(self):
        """Mechanism keyword 'efflux pump' must be recognized as in-scope with resistance_mechanism intent."""
        u = understand_query("efflux pump")
        self.assertEqual(u["domain"], "amr")
        self.assertTrue(u["scope_ok"])
        self.assertIn("resistance_mechanism", u["intent"])

    def test_short_query_organism_and_drug(self):
        """Organism + drug query 'Acinetobacter baumannii colistin' must be recognized as in-scope."""
        u = understand_query("Acinetobacter baumannii colistin")
        self.assertEqual(u["domain"], "amr")
        self.assertTrue(u["scope_ok"])
        self.assertTrue(any("acinetobacter baumannii" in o.lower() for o in u["entities"]["organisms"]))
        self.assertTrue(any("colistin" in d.lower() for d in u["entities"]["antibiotics"]))

    def test_partial_gene_candidates_card_lookup(self):
        """CARD search for partial gene 'ompK' must return OmpK35, OmpK36 with high score."""
        res = search_card_candidates("ompK", include_related=True)
        self.assertEqual(res["status"], "found")
        names = [c["canonical_name"].lower() for c in res["candidates"]]
        self.assertTrue(any("ompk35" in n for n in names))
        self.assertTrue(any("ompk36" in n for n in names))

    def test_unified_references_single_section_no_duplicates(self):
        """Ensure report generator replaces any duplicate references with one numbered References section."""
        raw_answer = (
            "Fluoroquinolone resistance is mediated by GyrA mutations [1] and ParC substitutions [2].\n\n"
            "### References\n"
            "[1] Old ref 1\n"
            "[2] Old ref 2\n"
        )
        all_refs = [
            {"pmid": "11111", "doi": "10.1001/gyra", "title": "GyrA mutations in clinical isolates", "authors": ["Smith J", "Doe A"], "year": "2023", "journal": "Antimicrob Agents Chemother"},
            {"pmid": "22222", "doi": "10.1001/parc", "title": "ParC mechanisms in Enterobacteriaceae", "authors": ["Lee K"], "year": "2022", "journal": "J Antimicrob Chemother"},
        ]
        reconciled_text, cited_refs = reconcile_citations(raw_answer, all_refs)
        final_text = _ensure_references_section(reconciled_text, cited_refs)

        # Count occurrences of 'References' headings in the text
        ref_matches = re.findall(r"(?im)^\s*#{0,3}\s*references\b", final_text)
        self.assertEqual(len(ref_matches), 1, f"Expected exactly 1 References section, found {len(ref_matches)}: {final_text}")

        # Check that direct links are generated
        self.assertIn("https://pubmed.ncbi.nlm.nih.gov/11111/", final_text)
        self.assertIn("https://pubmed.ncbi.nlm.nih.gov/22222/", final_text)
        self.assertEqual(len(cited_refs), 2)
        self.assertEqual(cited_refs[0]["reference_number"], 1)
        self.assertEqual(cited_refs[1]["reference_number"], 2)
        self.assertTrue(cited_refs[0]["pubmed_url"].startswith("https://pubmed.ncbi.nlm.nih.gov/"))

    def test_related_evidence_fallback_structure(self):
        """When direct evidence is missing, response synthesis provides related-evidence fallback with clear labels."""
        u = {
            "intent": ["resistance_mechanism"],
            "entities": {
                "genes": ["ompK", "ompK35", "ompK36"],
                "candidate_genes": ["ompK35", "ompK36"],
                "antibiotics": ["meropenem"],
                "organisms": ["Klebsiella pneumoniae"],
                "mutations": [],
                "mechanisms": ["porin loss"],
            },
        }
        evidence = {
            "reference_evidence": [
                {"canonical_name": "ompK35", "aro_id": "ARO:3000552", "source": "CARD", "description": "Porin channel"},
                {"canonical_name": "ompK36", "aro_id": "ARO:3000553", "source": "CARD", "description": "Porin channel"},
            ],
            "literature_evidence": [{
                "direct_evidence": [],
                "related_evidence": [{
                    "title": "Porin deficiency in carbapenem-resistant Klebsiella pneumoniae",
                    "pmid": "33333",
                    "evidence_snippets": [{"text": "OmpK35 downregulation combined with OmpK36 loop 3 insertions causes carbapenem resistance."}],
                }],
            }],
        }
        refs = [{"reference_number": 1, "pmid": "33333", "title": "Porin deficiency in carbapenem-resistant Klebsiella pneumoniae"}]
        answer = synthesize_response("ompK", u, evidence, refs)

        self.assertIn("### Resistance Mechanism Overview", answer)
        self.assertIn("ompk35", answer.lower())
        self.assertIn("ompk36", answer.lower())
        self.assertIn("References", answer)


if __name__ == "__main__":
    unittest.main()
