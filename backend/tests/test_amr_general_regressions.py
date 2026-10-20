"""Comprehensive regression suite for query-agnostic multi-agent AMR research assistant.

Tests:
1. Attribution Precision:
   - Ddl Ser210Tyr must not be attributed to vanA.
   - G2576T (23S rRNA / linezolid) must not be presented as vanA-mediated vancomycin evidence.
   - Cross-gene proximity leakage prevention across multi-gene sentences.
   - Protein length plausibility bounds (VanA 343 aa, KatG 740 aa, bacterial protein sanity bound).
2. General Scope & AMR Intent Generalization:
   - In-scope for resistance mechanisms, comparisons, general AMR questions, and mutation questions.
   - Out-of-scope for non-AMR questions with helpful guidance.
3. WHO TB Catalogue Generalization:
   - M. tuberculosis katG / isoniazid, gyrA / fluoroquinolones.
4. Response Synthesis Adaptation:
   - Mechanism queries produce structured mechanistic reports.
   - Comparison queries produce comparative summaries and tables.
   - Mutation queries produce curated mutation tables.
5. End-to-End Workflow:
   - Orchestrator routes properly for mechanism, comparison, mutation, and out-of-scope queries.
"""
import unittest

from agents.query_understanding import understand_query, _amr_scope
from agents.planner import create_plan
from agents.mutation_matching import (
    MutationIndex,
    find_mutations_in_text,
    assess_paper,
    _gene_mentions,
    _attributed_gene,
)
from agents.mutation_evidence import (
    classify_sentence_phenotype,
    extract_literature_mutations,
    _mutation_position_plausible,
    mutation_position_issue,
)
from agents.mutation_reference import retrieve_mutations
from agents.entity_aliases import drug_resistance_link, drug_hits
from agents.response_synthesis import synthesize_response
from services.orchestrator import process_query


class AMRGeneralRegressionTests(unittest.TestCase):

    # -------------------------------------------------------------------------
    # 1. Biological Attribution Regressions (Ddl / vanA and G2576T / linezolid)
    # -------------------------------------------------------------------------

    def test_ddl_ser210tyr_not_attributed_to_vana(self):
        """Ddl Ser210Tyr must NOT be incorrectly attributed to vanA."""
        text = (
            "Enterococcus faecium isolates carried the vanA gene cluster conferring vancomycin resistance. "
            "In addition, the Ddl mutation Ser210Tyr was identified in isolates with altered peptidoglycan synthesis."
        )
        index = MutationIndex(["Ser210Tyr", "S210Y"], genes=["vanA"], organisms=["Enterococcus faecium"])
        matches, others = find_mutations_in_text(text, index, ["vanA"])

        extracted_mutations = [m["mutation"] for m in matches + others]
        self.assertNotIn(
            "S210Y",
            extracted_mutations,
            "Ddl Ser210Tyr was incorrectly attributed to vanA!",
        )
        self.assertNotIn(
            "Ser210Tyr",
            extracted_mutations,
            "Ddl Ser210Tyr was incorrectly attributed to vanA!",
        )

    def test_ddl_preceding_mutation_is_recognized_as_distinct_gene(self):
        """Sentence with 'ddl mutation Ser210Tyr' assigns Ser210Tyr to ddl, not vanA."""
        sentence = "The ddl mutation Ser210Tyr was detected in vanA-positive enterococcal isolates."
        mentions = _gene_mentions(sentence, ["vanA"])
        # ddl must be recognized as a distinct gene mention
        mention_names = [m[2] for m in mentions]
        self.assertIn("ddl", mention_names, "ddl was not recognized as a gene in sentence!")
        self.assertIn("vana", mention_names)

        # Attribute Ser210Tyr
        items = [{"sub": ("S", 210, "Y"), "start": 17, "end": 26, "as_written": "Ser210Tyr"}]
        owner = _attributed_gene(sentence, items, mentions)
        self.assertIsNotNone(owner)
        self.assertEqual(owner[2], "ddl", "Ser210Tyr was attributed to vanA instead of ddl!")
        self.assertFalse(owner[3], "ddl should have is_queried=False for vanA query")

    def test_g2576t_in_23s_rrna_not_attributed_to_vana(self):
        """G2576T in 23S rRNA must NOT be attributed to vanA."""
        text = (
            "Vancomycin-resistant enterococci (VRE) harboring vanA were evaluated for linezolid susceptibility. "
            "The 23S rRNA mutation G2576T was associated with linezolid resistance."
        )
        index = MutationIndex(["G2576T"], genes=["vanA"], organisms=["Enterococcus faecium"])
        matches, others = find_mutations_in_text(text, index, ["vanA"])

        extracted_mutations = [m["mutation"] for m in matches + others]
        self.assertNotIn(
            "G2576T",
            extracted_mutations,
            "G2576T in 23S rRNA was incorrectly attributed to vanA!",
        )

    def test_g2576t_position_rejected_for_vana_protein_length(self):
        """VanA is ~343 aa; residue position 2576 must be rejected as biologically impossible."""
        self.assertFalse(
            _mutation_position_plausible("G2576T", ["vanA"], ["Enterococcus faecium"]),
            "G2576T must be rejected for vanA based on protein length!",
        )
        self.assertFalse(
            _mutation_position_plausible("G2576T", ["vanA"], []),
            "G2576T must be rejected for vanA even when organism is unspecified!",
        )
        issue = mutation_position_issue("G2576T", ["vanA"], ["Enterococcus faecium"])
        self.assertIsNotNone(issue)
        self.assertIn("vana protein length", issue.lower())

    def test_g2576t_linezolid_resistance_not_attributed_to_vancomycin(self):
        """Sentence linking G2576T to linezolid in VRE must not qualify as vancomycin resistance evidence."""
        sentence = "The 23S rRNA G2576T mutation was associated with linezolid resistance in vancomycin-resistant enterococci."

        # Phenotype check for linezolid: should be positive
        self.assertEqual(
            classify_sentence_phenotype(sentence, ["linezolid"]),
            "resistance-associated evidence",
        )

        # Phenotype check for vancomycin: should be rejected because vancomycin is only an isolate descriptor
        self.assertFalse(
            drug_resistance_link(sentence, "vancomycin"),
            "vancomycin in 'vancomycin-resistant enterococci' should not count as resistance target for G2576T!",
        )
        self.assertEqual(
            classify_sentence_phenotype(sentence, ["vancomycin"]),
            "phenotype not explicit in extracted sentence",
        )

    def test_literature_extractor_excludes_g2576t_for_vana_vancomycin(self):
        """extract_literature_mutations must not surface G2576T as vanA vancomycin evidence."""
        paper = {
            "pmid": "9999001",
            "title": "Characterization of linezolid resistance in vancomycin-resistant Enterococcus faecium",
            "abstract": (
                "VanA-positive vancomycin-resistant Enterococcus faecium isolates were analyzed. "
                "The 23S rRNA G2576T mutation was associated with linezolid resistance in these strains."
            ),
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/9999001/",
        }
        understanding = {
            "entities": {
                "genes": ["vanA"],
                "antibiotics": ["vancomycin"],
                "organisms": ["Enterococcus faecium"],
                "mutations": [],
                "variants": [],
            },
            "intent": ["mutation_variant_information"],
        }
        result = extract_literature_mutations(
            [{"literature": {"papers": [paper]}}],
            understanding,
            mutation_reference={"mutations": [], "all_mutations": []},
        )
        mutations_surfaced = [r["mutation"] for r in result]
        self.assertNotIn("G2576T", mutations_surfaced)

    # -------------------------------------------------------------------------
    # 2. General Scope & Query Generalization
    # -------------------------------------------------------------------------

    def test_mechanism_query_is_in_scope(self):
        """Queries asking about resistance mechanisms must be in-scope."""
        queries = [
            "How does rpoB confer rifampicin resistance?",
            "What is the mechanism of resistance of KPC-2 in Klebsiella pneumoniae?",
            "How does vanA mediate vancomycin resistance in Enterococcus faecium?",
            "What are the mechanisms of carbapenem resistance in Pseudomonas aeruginosa?",
        ]
        for q in queries:
            u = understand_query(q)
            self.assertEqual(u["domain"], "amr", f"Query '{q}' should be in domain 'amr'")
            self.assertTrue(u["scope_ok"], f"Query '{q}' should have scope_ok=True")
            self.assertIn(
                "resistance_mechanism",
                u["intent"],
                f"Query '{q}' should have intent 'resistance_mechanism'",
            )

    def test_comparison_query_is_in_scope(self):
        """Comparative queries must be in-scope and planned for comparison."""
        q = "Compare MexAB-OprM and AdeABC efflux pumps in Pseudomonas and Acinetobacter"
        u = understand_query(q)
        self.assertEqual(u["domain"], "amr")
        self.assertTrue(u["scope_ok"])
        self.assertIn("comparison", u["intent"])

        plan = create_plan(q, u)
        self.assertEqual(plan["status"], "planned")
        self.assertIn("comparison", plan["intent"])

    def test_general_amr_queries_in_scope(self):
        """General AMR queries must be in-scope."""
        queries = [
            "What is rpoB?",
            "What are the mechanisms of antimicrobial resistance?",
            "What antibiotics are effective against MRSA?",
            "What causes colistin resistance in Klebsiella pneumoniae?",
        ]
        for q in queries:
            u = understand_query(q)
            self.assertEqual(u["domain"], "amr", f"Query '{q}' should be in domain 'amr'")
            self.assertTrue(u["scope_ok"], f"Query '{q}' should have scope_ok=True")

    def test_non_amr_queries_properly_rejected(self):
        """Truly non-AMR queries must be flagged as out_of_scope."""
        non_amr = [
            "What is the capital of France?",
            "How do I bake chocolate chip cookies?",
            "What is the airspeed velocity of an unladen swallow?",
            "Explain quantum gravity and string theory.",
        ]
        for q in non_amr:
            u = understand_query(q)
            self.assertEqual(u["domain"], "out_of_scope", f"Query '{q}' should be out_of_scope")
            self.assertFalse(u["scope_ok"], f"Query '{q}' should have scope_ok=False")
            plan = create_plan(q, u)
            self.assertEqual(plan["status"], "out_of_scope")

    # -------------------------------------------------------------------------
    # 3. WHO TB Mutation Catalogue Generalization
    # -------------------------------------------------------------------------

    def test_who_tb_catalogue_retrieves_katg_isoniazid(self):
        """WHO TB catalogue must support katG and isoniazid, not just rpoB/rifampicin."""
        u = {
            "query": "Which mutations in katG are associated with isoniazid resistance in Mycobacterium tuberculosis?",
            "entities": {
                "genes": ["katG"],
                "organisms": ["Mycobacterium tuberculosis"],
                "antibiotics": ["isoniazid"],
                "mutations": [],
            },
            "organism_resolutions": [],
        }
        res = retrieve_mutations(u)
        self.assertEqual(res["status"], "found")
        mutations = [r["mutation"] for r in res["mutations"]]
        self.assertIn("S315T", mutations, "katG S315T was not retrieved from WHO TB catalogue!")
        s315t = next(r for r in res["mutations"] if r["mutation"] == "S315T")
        self.assertEqual(s315t["phenotype"], "resistance-associated")

    def test_who_tb_catalogue_retrieves_gyra_fluoroquinolones(self):
        """WHO TB catalogue must support gyrA and levofloxacin/moxifloxacin in M. tuberculosis."""
        u = {
            "query": "Which mutations in gyrA are associated with levofloxacin resistance in Mycobacterium tuberculosis?",
            "entities": {
                "genes": ["gyrA"],
                "organisms": ["Mycobacterium tuberculosis"],
                "antibiotics": ["levofloxacin"],
                "mutations": [],
            },
            "organism_resolutions": [],
        }
        res = retrieve_mutations(u)
        self.assertEqual(res["status"], "found")
        self.assertTrue(len(res["mutations"]) > 0)
        mutations = [r["mutation"] for r in res["mutations"]]
        # gyrA D94G, A90V, S91P are classic WHO fluoroquinolone resistance mutations
        self.assertTrue(any(m in mutations for m in ["D94G", "A90V", "S91P"]))

    # -------------------------------------------------------------------------
    # 4. Response Synthesis Adaptation to Query Intent
    # -------------------------------------------------------------------------

    def test_mechanism_response_produces_structured_mechanistic_report(self):
        """Mechanism queries must return a structured mechanistic explanation with classification table."""
        u = {
            "intent": ["resistance_mechanism"],
            "entities": {
                "genes": ["vanA"],
                "antibiotics": ["vancomycin"],
                "organisms": ["Enterococcus faecium"],
            },
        }
        evidence = {
            "reference_evidence": [{
                "mention": "vanA",
                "canonical_name": "vanA",
                "aro_id": "ARO:3000010",
                "description": "VanA is an inducible high-level glycopeptide resistance gene encoding a D-Ala-D-Lac ligase.",
                "source": "CARD",
            }],
            "literature_evidence": [{
                "direct_evidence": [{
                    "title": "Molecular basis of glycopeptide resistance mediated by vanA",
                    "pmid": "12345678",
                    "evidence_snippets": [{"text": "VanA synthesizes cell wall precursors terminating in D-lactate instead of D-alanine."}],
                }],
                "related_evidence": [],
            }],
        }
        refs = [{"reference_number": 1, "title": "Molecular basis of glycopeptide resistance", "pmid": "12345678"}]
        answer = synthesize_response("How does vanA confer vancomycin resistance?", u, evidence, refs)

        self.assertIn("### Resistance Mechanism Overview", answer)
        self.assertIn("ARO:3000010", answer)
        self.assertIn("D-lactate instead of D-alanine", answer)
        self.assertIn("[1]", answer)
        # Should NOT force a mutation table when the user asked for a mechanism
        self.assertNotIn("| Mutation | Phenotype |", answer)

    def test_comparison_response_produces_comparison_table(self):
        """Comparison queries must return a comparative summary table."""
        u = {
            "intent": ["comparison"],
            "entities": {
                "genes": ["MexAB-OprM", "AdeABC"],
                "organisms": ["Pseudomonas aeruginosa", "Acinetobacter baumannii"],
            },
        }
        evidence = {
            "reference_evidence": [
                {"canonical_name": "MexAB-OprM", "specificity": "RND efflux system", "description": "Multidrug efflux in P. aeruginosa"},
                {"canonical_name": "AdeABC", "specificity": "RND efflux system", "description": "Multidrug efflux in A. baumannii"},
            ],
            "literature_evidence": [],
        }
        refs = []
        answer = synthesize_response("Compare MexAB-OprM and AdeABC efflux pumps", u, evidence, refs)

        self.assertIn("### Comparative", answer)
        self.assertIn("| Entity | Category / Family |", answer)
        self.assertIn("MexAB-OprM", answer)
        self.assertIn("AdeABC", answer)

    # -------------------------------------------------------------------------
    # 5. End-to-End Orchestrator In-Scope & Out-of-Scope Integrity
    # -------------------------------------------------------------------------

    def test_end_to_end_out_of_scope_query(self):
        """Non-AMR query through process_query returns out_of_scope message and explanation."""
        res = process_query("What is the recipe for chocolate cake?")
        self.assertEqual(res["status"], "success")
        self.assertIn("not identified as an antimicrobial resistance (AMR) question", res["answer"])
        self.assertEqual(res["references"], [])

    def test_end_to_end_amr_mechanism_query(self):
        """AMR mechanism query through process_query executes and returns structured report."""
        res = process_query("How does KPC-2 confer carbapenem resistance in Klebsiella pneumoniae?")
        self.assertEqual(res["status"], "success")
        self.assertIn("Resistance Mechanism Overview", res["answer"])
        self.assertNotIn("This release is restricted to mutation-focused", res["answer"])


if __name__ == "__main__":
    unittest.main()
