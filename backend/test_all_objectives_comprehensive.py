"""Comprehensive Verification Suite for All 4 AMR Research Objectives.

Fulfills:
- Aim: Agentic AI-based evidence retrieval and interpretation framework for AMR gene mutations.
- Objective 1: Authoritative literature and biological database integration (CARD, NCBI, WHO TB, UniProt, PDB, AlphaFold).
- Objective 2: 5-facet biological evidence characterization (occurrence, genetic context, sequence, functional effects, resistance).
- Objective 3: Sequence & 3D structural analysis, physicochemical deltas (Delta_V, Delta_Q, Delta_Hydropathy),
               molecular consequences, and evidence-supported hypotheses for poorly characterized/novel variants.
- Objective 4: Dynamic agentic coordination and 5-tier stratified reporting
               (established findings, database-supported, computational evidence, hypotheses, evidence gaps).
"""
import unittest
import os
import re

from agents.query_understanding import understand_query
from agents.card_reference import search_card_candidates
from agents.ncbi_reference import search_ncbi_candidates
from agents.mutation_reference import retrieve_mutations
from agents.structural_analysis import (
    calculate_physicochemical_delta,
    get_structural_profile,
    analyze_mutation_structural_consequences,
    generate_evidence_supported_hypothesis,
    characterize_mutation_5_facets,
    run_structural_analysis,
    STRUCTURAL_REGISTRY,
)
from agents.response_synthesis import (
    synthesize_response,
    _synthesize_5_facet_matrix,
    _synthesize_structural_computational_section,
    _synthesize_hypotheses_section,
    _synthesize_evidence_gaps_section,
)
from services.orchestrator import process_query


class ComprehensiveAMRObjectivesTest(unittest.TestCase):

    # ==========================================================================
    # OBJECTIVE 1: MULTI-RESOURCE RETRIEVAL (DATABASES & LITERATURE)
    # ==========================================================================
    def test_obj1_card_database_integration(self):
        """Objective 1: CARD provides curated AMR determinants, ARO IDs, and mechanisms."""
        res = search_card_candidates("blaKPC-2")
        self.assertEqual(res["status"], "found")
        cand = res["candidates"][0]
        self.assertIn("kpc", cand["canonical_name"].lower())
        self.assertTrue(cand["aro_id"].startswith("ARO:"))
        self.assertTrue(bool(cand["description"]))

    def test_obj1_ncbi_amrfinderplus_integration(self):
        """Objective 1: NCBI AMRFinderPlus reference contains verified clinical determinants."""
        res = search_ncbi_candidates("gyrA")
        self.assertEqual(res["status"], "found")
        self.assertTrue(len(res["candidates"]) > 0)

    def test_obj1_who_tb_catalogue_integration(self):
        """Objective 1: WHO TB Mutation Catalogue provides clinical confidence grading."""
        u = {
            "entities": {
                "genes": ["rpoB"],
                "organisms": ["Mycobacterium tuberculosis"],
                "antibiotics": ["rifampicin"],
                "mutations": ["S450L"],
            },
            "organism_resolutions": [],
        }
        res = retrieve_mutations(u)
        self.assertEqual(res["status"], "found")
        self.assertTrue(any(r["mutation"] == "S450L" for r in res["mutations"]))
        s450l = next(r for r in res["mutations"] if r["mutation"] == "S450L")
        self.assertEqual(s450l["confidence"], "1) Assoc w R")

    def test_obj1_macromolecular_structure_registry_integration(self):
        """Objective 1: PDB structures, AlphaFold models, and UniProt accessions are integrated."""
        for gene_key in ["tem", "kpc", "katg", "rpob", "gyra", "mcr-1", "vana", "acrb"]:
            prof = get_structural_profile(gene_key)
            self.assertIn(gene_key, STRUCTURAL_REGISTRY)
            self.assertTrue(len(prof["pdb_structures"]) > 0, f"Missing PDB structures for {gene_key}")
            self.assertTrue(prof["alphafold_id"].startswith("AF-"), f"Missing AlphaFold ID for {gene_key}")
            self.assertTrue(bool(prof["uniprot_id"]), f"Missing UniProt ID for {gene_key}")
            self.assertTrue(len(prof["functional_domains"]) > 0, f"Missing functional domains for {gene_key}")

    # ==========================================================================
    # OBJECTIVE 2: 5-FACET BIOLOGICAL EVIDENCE CHARACTERIZATION
    # ==========================================================================
    def test_obj2_five_facet_characterization_matrix(self):
        """Objective 2: All 5 facets (occurrence, genetic context, sequence, functional effects, resistance) are systematically evaluated."""
        facet = characterize_mutation_5_facets(
            gene_symbol="katG",
            mutation_str="S315T",
            organism="Mycobacterium tuberculosis",
            drug="isoniazid",
            curated_row={"confidence": "1) Assoc w R", "phenotype": "resistance-associated", "source": "WHO TB Catalogue"},
        )
        self.assertTrue(any(w in facet["reported_occurrence"].lower() for w in ["prevalent", "clinical", "occurrence", "isolates"]))
        self.assertIn("chromosomal", facet["genetic_context"].lower())
        self.assertIn("codon 315", facet["sequence_characteristics"].lower())
        self.assertIn("delta_vol", facet["sequence_characteristics"].lower())
        self.assertIn("channel", facet["functional_effects"].lower())
        self.assertIn("isoniazid", facet["resistance_associations"].lower())

    def test_obj2_mobile_element_genetic_context(self):
        """Objective 2: Plasmid/transposon context is correctly distinguished for mobile genes."""
        facet = characterize_mutation_5_facets(
            gene_symbol="blaTEM",
            mutation_str="G238S",
            organism="Klebsiella pneumoniae",
            drug="cefotaxime",
        )
        self.assertIn("mobile genetic element", facet["genetic_context"].lower())
        self.assertIn("plasmid", facet["genetic_context"].lower())

    # ==========================================================================
    # OBJECTIVE 3: SEQUENCE/STRUCTURAL MODELING & EVIDENCE-SUPPORTED HYPOTHESES
    # ==========================================================================
    def test_obj3_physicochemical_deltas_quantitative(self):
        """Objective 3: Quantitative Delta_V, Delta_Charge, Delta_Hydropathy, and H-bond shifts."""
        # Serine (89.0 A^3, -0.8 hydropathy) -> Threonine (116.1 A^3, -0.7 hydropathy)
        d_st = calculate_physicochemical_delta("S", "T")
        self.assertEqual(d_st["delta_volume"], 27.1)
        self.assertEqual(d_st["delta_hydropathy"], 0.1)
        self.assertEqual(d_st["delta_charge"], 0.0)

        # Aspartate (-1 charge) -> Asparagine (0 charge) in blaKPC D179N
        d_dn = calculate_physicochemical_delta("D", "N")
        self.assertEqual(d_dn["delta_charge"], 1.0)
        self.assertIn("Gain of positive charge", d_dn["polarity_transition"])

        # Tyrosine (193.6 A^3) -> Cysteine (108.5 A^3) in novel KatG Y337C
        d_yc = calculate_physicochemical_delta("Y", "C")
        self.assertEqual(d_yc["delta_volume"], -85.1)
        self.assertIn("Severe cavity-creating reduction", d_yc["steric_effect"])

    def test_obj3_novel_uncharacterized_hypothesis_generation(self):
        """Objective 3: Formulate evidence-supported hypotheses for novel/uncharacterized variants where direct evidence is absent."""
        sc = analyze_mutation_structural_consequences("katG", "Y337C", "Mycobacterium tuberculosis", "isoniazid")
        self.assertTrue(sc["valid_notation"])
        self.assertTrue(sc["position_plausible"])
        self.assertIn("Severe cavity-creating reduction", sc["physicochemical"]["steric_effect"])

        hypo = generate_evidence_supported_hypothesis(
            gene_symbol="katG",
            mutation_str="Y337C",
            structural_consequence=sc,
            organism="Mycobacterium tuberculosis",
            drug="isoniazid",
            curated_evidence=None,  # No direct evidence
        )
        self.assertTrue(hypo["is_hypothesis"])
        self.assertEqual(hypo["status"], "computational_hypothesis")
        self.assertIn("Hypothesis: Evidence-Supported Computational & Structural Inference", hypo["hypothesis"])
        self.assertIn("Evidence Gap & Verification Priority", hypo["hypothesis"])
        self.assertIn("broth microdilution", hypo["hypothesis"])

    def test_obj3_active_site_proximity_and_loop_remodeling(self):
        """Objective 3: Accurately model omega-loop remodeling in blaKPC-2 D179N."""
        sc = analyze_mutation_structural_consequences("blaKPC-2", "D179N", "Klebsiella pneumoniae", "ceftazidime-avibactam")
        self.assertTrue(sc["valid_notation"])
        self.assertIn("Omega-loop", sc["domain"])
        self.assertEqual(sc["physicochemical"]["delta_charge"], 1.0)

    def test_obj3_invalid_position_rejected_by_sequence_length(self):
        """Objective 3: Reject biologically impossible coordinates that exceed gene length."""
        sc = analyze_mutation_structural_consequences("katG", "G944C")
        self.assertFalse(sc["position_plausible"])
        self.assertIn("exceeds canonical protein coding capacity", sc["error"])

    # ==========================================================================
    # OBJECTIVE 4: AGENTIC WORKFLOW & 5-TIER STRATIFIED REPORTING
    # ==========================================================================
    def test_obj4_agentic_workflow_execution_and_5_tier_distinction(self):
        """Objective 4: LangGraph workflow coordinates retrieval and produces traceable 5-tier report."""
        report = process_query("What are the structural consequences and hypothesis for KatG Y337C in Mycobacterium tuberculosis?")
        self.assertEqual(report["status"], "success")
        answer = report["answer"]

        # Tier 1: Established Findings / Summary
        self.assertTrue("Executive Scientific Summary" in answer or "Summary" in answer)
        # Tier 2: Database-supported information
        self.assertIn("Database-Supported Information", answer)
        # Tier 3: Computational Evidence & 3D Macromolecular Architecture
        self.assertIn("Computational Evidence & 3D Macromolecular Architecture", answer)
        self.assertIn("PDB", answer)
        self.assertIn("AlphaFold", answer)
        # Tier 4: Evidence-Supported Hypotheses
        self.assertIn("Evidence-Supported Hypotheses", answer)
        # Tier 5: Evidence Gaps & Verification Priorities
        self.assertIn("Evidence Gaps & Verification Priorities", answer)

        # Verify trace in orchestrator output
        trace = report.get("execution_trace", [])
        self.assertTrue(any("Structural" in t.get("agent", "") for t in trace), f"Structural agent missing from trace: {trace}")
        self.assertIn("structural_analysis", report)
        self.assertTrue(bool(report["structural_analysis"].get("gene_structures")))

    def test_obj4_traceable_references_with_urls(self):
        """Objective 4: References are numbered and clickable with URLs."""
        report = process_query("Which mutations in rpoB are associated with rifampicin resistance in Mycobacterium tuberculosis?")
        answer = report["answer"]
        self.assertIn("References", answer)
        self.assertTrue(re.search(r"\[\d+\]", answer))


if __name__ == "__main__":
    unittest.main()
