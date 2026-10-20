"""Regression tests for mutation gene-attribution and phenotype filtering."""
import unittest

from agents.mutation_matching import MutationIndex, find_mutations_in_text, records_for_matching
from agents.response_synthesis import _mutation_table, _mutation_focused_response
from agents.mutation_evidence import classify_sentence_phenotype, extract_literature_mutations
from agents.mutation_reference import retrieve_mutations
from agents.entity_aliases import drug_hits
from agents.query_understanding import _ground_terms, _explicit_gene_symbol_mentions


class MutationScopeRegressionTests(unittest.TestCase):
    def test_organism_unspecified_reference_rows_do_not_define_species_numbering(self):
        ref = {"all_mutations": [{"mutation": "S83L", "organism_specific": False}], "mutations": []}
        self.assertEqual(records_for_matching(ref), [])
        specific = {"all_mutations": [{"mutation": "S83L", "organism_specific": True}], "mutations": []}
        self.assertEqual(records_for_matching(specific), specific["all_mutations"])

    def test_other_gene_mutation_does_not_leak_into_katg_query(self):
        text = (
            "Mutations in katG and rpoB are associated with resistance. "
            "The rpoB mutation S450L was observed in resistant isolates. "
            "katG variants were also examined."
        )
        index = MutationIndex(["S315T", "S450L"], genes=["katG"], organisms=["Mycobacterium tuberculosis"])
        matches, others = find_mutations_in_text(text, index, ["katG"])
        self.assertNotIn("S450L", [m["mutation"] for m in matches + others])

    def test_single_gene_following_sentence_still_extracts_variant(self):
        text = (
            "The katG gene was sequenced in Mycobacterium tuberculosis. "
            "The S315T substitution was detected in resistant isolates."
        )
        index = MutationIndex(["S315T"], genes=["katG"], organisms=["Mycobacterium tuberculosis"])
        matches, others = find_mutations_in_text(text, index, ["katG"])
        self.assertIn("S315T", [m["mutation"] for m in matches + others])


    def test_literature_extractor_rejects_rrdr_mutations_for_katg_isoniazid_query(self):
        paper = {
            "pmid": "28904673",
            "title": "Mutations in rpoB and katG genes of Mycobacterium tuberculosis",
            "abstract": "In Mycobacterium tuberculosis, katG S315T was associated with isoniazid resistance.",
            "full_text": (
                "The katG gene was examined in Mycobacterium tuberculosis. "
                "I572P, E562Q, P564S, and Q490Y were identified as novel mutations outside the rifampicin resistance determining region. "
                "The katG S315T mutation was associated with isoniazid resistance in Mycobacterium tuberculosis."
            ),
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/28904673/",
        }
        understanding = {"entities": {
            "genes": ["katG"], "antibiotics": ["isoniazid"],
            "organisms": ["Mycobacterium tuberculosis"], "mutations": [], "variants": [],
        }}
        result = extract_literature_mutations(
            [{"literature": {"papers": [paper]}}], understanding,
            mutation_reference={"mutations": [], "all_mutations": []},
        )
        visible = {(r["mutation"], r["phenotype"]) for r in result}
        self.assertNotIn(("I572P", "resistance-associated evidence"), visible)
        self.assertIn(("S315T", "resistance-associated evidence"), visible)

    def test_impossible_katg_residue_position_is_rejected(self):
        paper = {
            "pmid": "40899880",
            "title": "katG mutations and isoniazid resistance in Mycobacterium tuberculosis",
            "abstract": "The katG G944C variant was reported in isoniazid-resistant Mycobacterium tuberculosis.",
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/40899880/",
        }
        understanding = {"entities": {
            "genes": ["katG"], "antibiotics": ["isoniazid"],
            "organisms": ["Mycobacterium tuberculosis"], "mutations": [], "variants": [],
        }}
        result = extract_literature_mutations(
            [{"literature": {"papers": [paper]}}], understanding,
            mutation_reference={"mutations": [], "all_mutations": []},
        )
        self.assertNotIn("G944C", [r["mutation"] for r in result])

    def test_valid_katg_position_remains_eligible(self):
        paper = {
            "pmid": "valid-katg-test",
            "title": "katG mutations and isoniazid resistance in Mycobacterium tuberculosis",
            "abstract": "The katG S315T variant was associated with isoniazid resistance in Mycobacterium tuberculosis.",
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/valid-katg-test/",
        }
        understanding = {"entities": {
            "genes": ["katG"], "antibiotics": ["isoniazid"],
            "organisms": ["Mycobacterium tuberculosis"], "mutations": [], "variants": [],
        }}
        result = extract_literature_mutations(
            [{"literature": {"papers": [paper]}}], understanding,
            mutation_reference={"mutations": [], "all_mutations": []},
        )
        self.assertIn("S315T", [r["mutation"] for r in result])

    def test_resistance_determining_region_is_not_phenotype_evidence(self):
        sentence = "I572P, E562Q, P564S, and Q490Y were identified as novel mutations outside the rifampicin resistance determining region."
        self.assertEqual(
            classify_sentence_phenotype(sentence, ["isoniazid"]),
            "phenotype not explicit in extracted sentence",
        )
        self.assertEqual(
            classify_sentence_phenotype(sentence, ["rifampicin"]),
            "phenotype not explicit in extracted sentence",
        )

    def test_mutation_level_drug_resistance_link_is_accepted(self):
        sentence = "The gyrA S83L mutation was associated with ciprofloxacin resistance in E. coli isolates."
        self.assertEqual(
            classify_sentence_phenotype(sentence, ["ciprofloxacin"]),
            "resistance-associated evidence",
        )

    def test_susceptibility_is_not_resistance(self):
        sentence = "The gyrA S83L variant was associated with ciprofloxacin susceptibility in E. coli."
        self.assertEqual(
            classify_sentence_phenotype(sentence, ["ciprofloxacin"]),
            "susceptibility-associated evidence",
        )

    def test_specific_drug_does_not_match_another_drug(self):
        self.assertFalse(drug_hits("levofloxacin resistance was observed", "ciprofloxacin"))
        self.assertTrue(drug_hits("levofloxacin resistance was observed", "fluoroquinolone"))
        self.assertTrue(drug_hits("rifampin resistance was observed", "rifampicin"))

    def test_entity_grounding_does_not_expand_named_drug_to_class(self):
        self.assertEqual(_ground_terms(["fluoroquinolone"], "ciprofloxacin resistance", "antibiotics"), [])
        self.assertEqual(_ground_terms(["ciprofloxacin"], "fluoroquinolone resistance", "antibiotics"), [])
        self.assertEqual(_ground_terms(["ciprofloxacin"], "ciprofloxacin resistance", "antibiotics"), ["ciprofloxacin"])

    def test_explicit_gene_extraction_does_not_depend_on_reference_database(self):
        self.assertEqual(_explicit_gene_symbol_mentions("Which mutations in katG are associated with isoniazid resistance in Mycobacterium tuberculosis?"), ["katG"])
        self.assertEqual(_explicit_gene_symbol_mentions("Which gyrA variants are associated with ciprofloxacin resistance in Escherichia coli?"), ["gyrA"])


    def test_alternative_numbering_resolves_to_native_reference_variant(self):
        result = retrieve_mutations({
            "query": "Is S531L in rpoB associated with rifampicin resistance in Mycobacterium tuberculosis?",
            "entities": {"genes": ["rpoB"], "organisms": ["Mycobacterium tuberculosis"], "antibiotics": ["rifampicin"], "mutations": ["S531L"], "variants": []},
            "organism_resolutions": [],
        })
        self.assertEqual(len(result["mutations"]), 1)
        self.assertEqual(result["mutations"][0]["mutation"], "S450L")
        self.assertEqual(result["mutations"][0]["query_notation"], "S531L")
        self.assertEqual(result["mutations"][0]["query_notation_scheme"], "E. coli numbering")

    def test_explicit_variant_query_returns_only_that_variant(self):
        evidence = {
            "mutation_reference": {"mutations": [
                {"mutation": "S450L", "phenotype": "resistance-associated", "confidence": "1) Assoc w R", "source": "WHO TB Mutation Catalogue 2023", "organism_specific": True},
                {"mutation": "D435V", "phenotype": "resistance-associated", "confidence": "1) Assoc w R", "source": "WHO TB Mutation Catalogue 2023", "organism_specific": True},
            ], "total_matching_records": 2},
            "literature_mutations": [],
        }
        table = _mutation_table(
            evidence,
            query="Is S450L in rpoB associated with rifampicin resistance in Mycobacterium tuberculosis?",
            entities={"genes": ["rpoB"], "organisms": ["Mycobacterium tuberculosis"]},
        )
        self.assertIn("S450L", table)
        self.assertNotIn("D435V", table)

    def test_no_extra_abstract_sentence_in_focused_response(self):
        understanding = {"entities": {"genes": ["gyrA"], "antibiotics": ["ciprofloxacin"], "organisms": ["Escherichia coli"], "mutations": [], "variants": []}, "intent": ["mutation_variant_information"]}
        evidence = {
            "mutation_reference": {"mutations": [], "total_matching_records": 0},
            "literature_mutations": [{"mutation": "S83L", "as_written": "S83L", "phenotype": "resistance-associated evidence", "pmid": "201", "source": "Primary literature", "evidence_sentence": "The gyrA S83L mutation was associated with ciprofloxacin resistance in Escherichia coli."}],
            "literature_evidence": [],
        }
        refs = [{"pmid": "201", "title": "Ciprofloxacin resistance in E. coli", "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/201/"}]
        answer = _mutation_focused_response("Which gyrA variants are associated with ciprofloxacin resistance in Escherichia coli?", understanding, evidence, refs)
        self.assertIn("S83L", answer)
        self.assertNotIn("mechanism", answer.lower())
        self.assertNotIn("epidemiology", answer.lower())
        self.assertNotIn("The displayed list is a ranked subset", answer)

    def test_response_excludes_unlinked_variants_and_abstract_dump(self):
        understanding = {"entities": {"genes": ["katG"], "antibiotics": ["isoniazid"], "organisms": ["Mycobacterium tuberculosis"], "mutations": [], "variants": []}, "intent": ["mutation_variant_information"]}
        evidence = {
            "mutation_reference": {"mutations": [], "total_matching_records": 0},
            "literature_mutations": [
                {"mutation": "I572P", "phenotype": "phenotype not explicit in extracted sentence", "pmid": "100", "source": "Primary literature", "evidence_sentence": "outside the rifampicin resistance determining region"},
                {"mutation": "S315T", "phenotype": "resistance-associated evidence", "pmid": "101", "source": "Primary literature", "evidence_sentence": "katG S315T was associated with isoniazid resistance"},
            ],
            "literature_evidence": [],
        }
        refs = [
            {"pmid": "100", "title": "Irrelevant rpoB paper", "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/100/"},
            {"pmid": "101", "title": "katG isoniazid paper", "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/101/"},
        ]
        answer = _mutation_focused_response("Which mutations in katG are associated with isoniazid resistance in Mycobacterium tuberculosis?", understanding, evidence, refs)
        self.assertNotIn("I572P", answer)
        self.assertIn("S315T", answer)
        self.assertNotIn("outside the rifampicin resistance determining region", answer)
        self.assertNotIn("Irrelevant rpoB paper", answer)

    def test_literature_support_displays_traceable_quote_not_generic_claim(self):
        understanding = {"entities": {"genes": ["katG"], "antibiotics": ["isoniazid"], "organisms": ["Mycobacterium tuberculosis"], "mutations": [], "variants": []}, "intent": ["mutation_variant_information"]}
        evidence = {
            "mutation_reference": {"mutations": [], "total_matching_records": 0},
            "literature_mutations": [
                {"mutation": "S315T", "as_written": "S315T", "phenotype": "resistance-associated evidence", "pmid": "101", "evidence_sentence": "The katG S315T mutation was associated with isoniazid resistance in Mycobacterium tuberculosis."},
                {"mutation": "A162E", "as_written": "A162E", "phenotype": "resistance-associated evidence", "pmid": "102"},
            ],
        }
        refs = [
            {"pmid": "101", "title": "katG S315T and isoniazid resistance", "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/101/"},
            {"pmid": "102", "title": "A162E paper without extracted quote", "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/102/"},
        ]
        answer = _mutation_focused_response("Which mutations in katG are associated with isoniazid resistance in Mycobacterium tuberculosis?", understanding, evidence, refs)
        self.assertIn("auditable excerpts", answer)
        self.assertIn("The katG S315T mutation was associated with isoniazid resistance", answer)
        self.assertNotIn("A162E paper without extracted quote", answer)
        self.assertNotIn("mutation-specific resistance evidence [2]", answer)

    def test_literature_excerpt_preserves_alternative_numbering_annotation(self):
        understanding = {"entities": {"genes": ["rpoB"], "antibiotics": ["rifampicin"], "organisms": ["Mycobacterium tuberculosis"], "mutations": [], "variants": []}, "intent": ["mutation_variant_information"]}
        evidence = {
            "mutation_reference": {"mutations": [], "total_matching_records": 0},
            "literature_mutations": [{
                "mutation": "S450L", "as_written": "Ser531Leu", "numbering": "E. coli numbering",
                "phenotype": "resistance-associated evidence", "pmid": "501",
                "evidence_sentence": "In M. tuberculosis, the rpoB Ser531Leu substitution was linked to rifampin resistance."
            }],
        }
        refs = [{"pmid": "501", "title": "rpoB variants in M. tuberculosis", "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/501/"}]
        answer = _mutation_focused_response("Which mutations in rpoB are associated with rifampicin resistance in Mycobacterium tuberculosis?", understanding, evidence, refs)
        self.assertIn("S450L (written Ser531Leu; E. coli numbering)", answer)
        self.assertIn("rpoB Ser531Leu substitution was linked to rifampin resistance", answer)

    def test_not_associated_with_resistance_query_filters_to_negative_classification(self):
        from agents.response_synthesis import _phenotype_matches, _requested_phenotype
        query = "Which rpoB mutations are not associated with rifampicin resistance in Mycobacterium tuberculosis?"
        self.assertEqual(_requested_phenotype(query), "non_resistance")
        self.assertTrue(_phenotype_matches({"phenotype": "not associated with resistance"}, "non_resistance"))
        self.assertFalse(_phenotype_matches({"phenotype": "resistance-associated"}, "non_resistance"))

    def test_resistance_table_excludes_susceptibility_and_unclear_rows(self):
        evidence = {
            "mutation_reference": {"mutations": [], "total_matching_records": 0},
            "literature_mutations": [
                {"mutation": "S83L", "as_written": "S83L", "phenotype": "resistance-associated evidence", "source": "Primary literature", "evidence_sentence": "The gyrA S83L mutation was associated with ciprofloxacin resistance in E. coli."},
                {"mutation": "D87E", "as_written": "D87E", "phenotype": "susceptibility-associated evidence", "source": "Primary literature", "evidence_sentence": "The gyrA D87E mutation was associated with ciprofloxacin susceptibility in E. coli."},
                {"mutation": "S83T", "phenotype": "phenotype not explicit in extracted sentence", "source": "Primary literature"},
            ],
        }
        table = _mutation_table(evidence, query="Which gyrA mutations are associated with ciprofloxacin resistance in E. coli?")
        self.assertIn("S83L", table)
        self.assertNotIn("D87E", table)
        self.assertNotIn("S83T", table)


if __name__ == "__main__":
    unittest.main()
