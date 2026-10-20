"""Regression tests for mutation-specific evidence attribution.

Copy this file into backend/tests/ and run:
    python -m unittest tests.test_mutation_attribution_regressions -v
"""
import unittest
from agents.mutation_matching import assess_paper


PLAN = {
    "intent": ["mutation_variant_information"],
    "entities": {
        "genes": ["vanA"],
        "antibiotics": ["vancomycin"],
        "organisms": ["Enterococcus faecium"],
        "mutations": [],
        "variants": [],
    },
    "mutation_reference_mutations": [],
    "mutation_reference_reported": [],
}


class MutationAttributionRegressionTests(unittest.TestCase):
    def test_ddl_mutation_does_not_count_as_vana_mutation(self):
        paper = {
            "title": "Vancomycin-resistant Enterococcus faecium clinical genomic analysis",
            "abstract": (
                "Enterococcus faecium isolates carried vanA. A unique strain showed "
                "resistance without canonical van genes, harboring a Ddl Ser210Tyr mutation."
            ),
        }
        result = assess_paper(paper, PLAN)
        self.assertFalse(result["eligible"])
        self.assertIn("no_mutation_in_gene_context", result["reasons"])

    def test_linezolid_mutation_does_not_count_as_vana_vancomycin_evidence(self):
        paper = {
            "title": "Vancomycin-resistant Enterococcus faecium genomic surveillance",
            "abstract": (
                "Enterococcus faecium isolates were resistant to vancomycin; 98% carried "
                "vanA, while two isolates were resistant to linezolid (G2576T mutation)."
            ),
        }
        result = assess_paper(paper, PLAN)
        self.assertFalse(result["eligible"])
        self.assertIn("no_mutation_in_gene_context", result["reasons"])


if __name__ == "__main__":
    unittest.main()
