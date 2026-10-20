"""Evidence-analysis agent for the AMR research assistant.

The layer distinguishes exact-entity evidence from broader family-level evidence
and from merely related AMR papers.  It does not claim causality or scientific
quality from keyword overlap alone.
"""
import html
import re
from agents.entity_aliases import ORGANISM_OK_STATUSES, organism_assessment
from agents.mutation_evidence import extract_literature_mutations
from agents.mutation_matching import assess_paper, records_for_matching


def _parent_gene_terms(genes):
    parents = []
    for gene in genes or []:
        value = str(gene).strip()
        if not value:
            continue
        parent = re.sub(r"\.\d+$", "", value)
        if parent == value:
            parent = re.sub(r"[-_]\d+$", "", value)
        if parent != value and len(parent) >= 3:
            parents.append(parent.lower())
    return list(dict.fromkeys(parents))


def _sentence_snippets(paper, plan, max_snippets=4):
    text = paper.get("full_text") or paper.get("abstract") or ""
    if not text:
        return []
    text_clean = html.unescape(re.sub(r"<[^>]+>", "", text))
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", re.sub(r"\s+", " ", text_clean).strip())
    entities = plan.get("entities", {}) or {}
    terms = []
    for key in ("genes", "antibiotics", "mutations", "variants", "organisms", "mechanisms", "scientific_concepts", "query_keywords"):
        terms.extend(str(x).lower() for x in entities.get(key, []) if x)
    terms.extend(_parent_gene_terms(entities.get("genes", [])))
    intent_terms = ["resistance", "susceptibility", "mechanism", "mutation", "variant", "lipid", "enzyme", "pathway", "modification", "porin", "efflux", "promoter", "permeability"]
    scored = []
    for sentence in sentences:
        low = sentence.lower()
        score = sum(4 for t in terms if t in low) + sum(1 for t in intent_terms if t in low)
        if score and len(sentence.strip()) >= 35:
            scored.append((score, sentence.strip()))
    scored.sort(key=lambda x: (-x[0], -len(x[1])))
    return [{"text": x[1], "score": x[0]} for x in scored[:max_snippets]]


def _paper_alignment(paper, plan):
    text = f"{paper.get('title', '')} {paper.get('abstract', '')} {paper.get('full_text', '')}".lower()
    entities = plan.get("entities", {}) or {}
    genes = [str(x).lower() for x in entities.get("genes", []) if x]
    parent_genes = _parent_gene_terms(entities.get("genes", []))
    antibiotics = [str(x).lower() for x in entities.get("antibiotics", []) if x]
    mutations = [str(x).lower() for x in [*(entities.get("mutations") or []), *(entities.get("variants") or [])] if x]
    organisms = [str(x).lower() for x in entities.get("organisms", []) if x]
    mechanisms = [str(x).lower() for x in entities.get("mechanisms", []) if x]
    scientific_concepts = [str(x).lower() for x in entities.get("scientific_concepts", []) if x]
    concept_hit = any(c in text for c in scientific_concepts) if scientific_concepts else False
    intents = set(plan.get("intent") or [])

    # Mutation-focused queries use the shared, content-based assessment (same
    # logic as the PubMed filter) so the two stages cannot disagree.
    if "mutation_variant_information" in intents:
        assessment = assess_paper(paper, plan)
        return assessment["alignment"], assessment["specificity"]

    gene_hit = any(x in text for x in genes)
    parent_gene_hit = any(x in text for x in parent_genes)
    drug_hit = any(x in text for x in antibiotics)
    mutation_hit = any(x in text for x in mutations)
    org_assessment = organism_assessment(paper, organisms) if organisms else {"status": "unspecified"}
    organism_hit = org_assessment["status"] in ORGANISM_OK_STATUSES if organisms else False
    organism_specified = bool(organisms)

    # Specific mechanism matching vs generic AMR keywords
    specific_mech_hit = (
        any(m in text for m in mechanisms) or concept_hit
        if (mechanisms or scientific_concepts)
        else any(x in text for x in [
            "mechanism", "resistance", "susceptibility", "lipid", "enzyme",
            "pathway", "modify", "modification", "phosphoethanolamine",
            "efflux", "porin", "porins", "promoter",
        ])
    )
    generic_amr_hit = any(x in text for x in [
        "antimicrobial", "antibiotic", "resistance", "resistant", "susceptibility", "susceptible",
        "bacteria", "bacterial", "isolate", "pathogen", "clinical strain",
    ])
    mutation_text_hit = any(x in text for x in ("mutation", "variant", "substitution", "amino acid"))

    # Stage E: Multi-tier relevance classification
    alignment, specificity = None, None

    # Check for exclusion: non-AMR incidental match (cancer chemotherapy, neoplasm, viral)
    is_non_amr = any(x in text for x in [
        "carcinoma", "neoplasm", "chemotherapy resistant", "cancer cell line", "cancer cells",
        "tumor cell", "tumour cell", "melanoma", "leukemia", "hiv-1", "influenza virus", "hepatitis b", "hepatitis c"
    ])
    if is_non_amr and not (gene_hit or drug_hit):
        return "excluded", "none"

    query_has_specific_entities = bool(genes or antibiotics or mutations)
    if query_has_specific_entities and not (gene_hit or parent_gene_hit or drug_hit or mutation_hit or (organism_hit and generic_amr_hit)):
        return "excluded", "none"

    if "resistance_mechanism" in intents:
        if gene_hit and drug_hit and specific_mech_hit:
            alignment, specificity = "mechanism_aligned", "exact"
        elif parent_gene_hit and drug_hit and specific_mech_hit:
            alignment, specificity = "mechanism_family_context", "family"
        elif (gene_hit or drug_hit) and specific_mech_hit:
            alignment, specificity = "mechanism_aligned" if specific_mech_hit else "mechanism_related", "exact" if specific_mech_hit else "partial"
        elif specific_mech_hit and not query_has_specific_entities:
            alignment, specificity = "mechanism_aligned", "exact"
        elif gene_hit or drug_hit:
            alignment, specificity = "mechanism_related", "partial"
    if alignment is None and "mutation_variant_information" in intents:
        if gene_hit and mutation_text_hit and (mutation_hit or parent_gene_hit):
            alignment, specificity = "mutation_aligned", ("exact" if gene_hit else "family")
    if alignment is None and "comparison" in intents and (gene_hit or drug_hit) and ("compare" in text or "susceptibility" in text):
        alignment, specificity = "comparison_aligned", ("exact" if gene_hit else "partial")
    if alignment is None and gene_hit and drug_hit:
        alignment, specificity = "entity_relationship_aligned", "exact"
    if alignment is None and parent_gene_hit and drug_hit:
        alignment, specificity = "entity_relationship_family_context", "family"
    if alignment is None and (gene_hit or drug_hit or mutation_hit or organism_hit or (specific_mech_hit and not query_has_specific_entities)):
        alignment, specificity = "entity_aligned", ("exact" if (gene_hit or specific_mech_hit) else "partial")
    if alignment is None:
        alignment, specificity = "excluded", "none"

    # Organism-specificity downgrade: if the user named a specific organism and
    # this paper is about a different species, downgrade to related (or exclude if completely unrelated)
    if organism_specified and not organism_hit:
        if org_assessment["status"] == "other_organism" and not (gene_hit or specific_mech_hit):
            return "excluded", "other_organism"
        if alignment in {
            "mechanism_aligned", "mutation_aligned", "comparison_aligned", "entity_relationship_aligned",
        }:
            alignment = {
                "mechanism_aligned": "mechanism_related",
                "mutation_aligned": "entity_aligned",
                "comparison_aligned": "entity_aligned",
                "entity_relationship_aligned": "entity_aligned",
            }[alignment]
            specificity = "other_organism"

    return alignment, specificity


def analyze_literature(literature_results, understanding, mutation_reference=None):
    analyzed = []
    ref = mutation_reference or {}
    # Match against the COMPLETE reference set, not the 20 displayed rows.
    ref_records = records_for_matching(ref)
    plan = {
        "entities": understanding.get("entities", {}),
        "intent": understanding.get("intent", []),
        "mutation_reference_mutations": [r.get("mutation") for r in ref_records if r.get("mutation")],
        "mutation_reference_reported": [r.get("reported_mutation") for r in ref_records if r.get("reported_mutation")],
    }

    for item in literature_results or []:
        literature = item.get("literature", {})
        direct = []
        relevant_supporting = []
        related = []
        verify = []
        excluded = []
        for paper in literature.get("papers", []) or literature.get("top_papers", []):
            paper = dict(paper)
            alignment, specificity = _paper_alignment(paper, plan)
            paper["evidence_alignment"] = alignment
            paper["evidence_specificity"] = specificity

            if alignment == "excluded":
                excluded.append(paper)
                continue

            if "mutation_variant_information" in set(plan.get("intent") or []):
                a = assess_paper(paper, plan)
                paper["evidence_basis"] = {
                    "organism_status": a["organism"]["status"],
                    "organism_basis": a["organism"].get("basis"),
                    "other_organisms_in_text": a["organism"].get("others", {}),
                    "gene_in_title_or_abstract": a["gene_ok"],
                    "drug_in_title_or_abstract": a["drug_ok"],
                    "mutations_matched": [
                        {k: m[k] for k in ("mutation", "as_written", "numbering")} for m in a["reference_matches"]
                    ],
                    "other_substitutions": [m["as_written"] for m in a["other_substitutions"]],
                    "rejection_reasons": a["reasons"],
                    "_matches": a["reference_matches"] + a["other_substitutions"],
                }
                if a["eligible"]:
                    paper["evidence_snippets"] = [
                        {"text": m["sentence"][:400], "score": 10} for m in (a["reference_matches"] + a["other_substitutions"])[:3]
                    ]
            paper["joint_entity_support"] = specificity == "exact" and alignment in {
                "mechanism_aligned", "mutation_aligned", "comparison_aligned", "entity_relationship_aligned"
            }
            if not paper.get("evidence_snippets"):
                paper["evidence_snippets"] = _sentence_snippets(paper, plan)

            mutation_intent = "mutation_variant_information" in set(plan.get("intent") or [])
            mutation_direct_ok = (
                not mutation_intent
                or bool((paper.get("evidence_basis") or {}).get("_matches"))
                and not bool((paper.get("evidence_basis") or {}).get("rejection_reasons"))
                and any(
                    str(m.get("gene") or "").strip().lower() in {
                        str(g).strip().lower() for g in (plan.get("entities", {}) or {}).get("genes", [])
                    }
                    for m in (paper.get("evidence_basis") or {}).get("_matches", [])
                )
            )
            if alignment in {
                "mechanism_aligned", "mutation_aligned", "comparison_aligned", "entity_relationship_aligned",
            } and mutation_direct_ok and specificity != "other_organism":
                direct.append(paper)
            elif alignment in {
                "mechanism_family_context", "entity_relationship_family_context",
            } or (alignment in {"mechanism_aligned", "comparison_aligned", "entity_relationship_aligned"} and not mutation_direct_ok):
                relevant_supporting.append(paper)
                related.append(paper)
            elif alignment in {
                "mechanism_related", "entity_aligned", "mutation_aligned", "entity_relationship_aligned",
            } or specificity in {"partial", "other_organism"}:
                related.append(paper)
            else:
                verify.append(paper)

        analyzed.append({
            "source": literature.get("source", "PubMed"),
            "search_query": literature.get("search_query"),
            "sort_basis": literature.get("sort_basis"),
            "search_queries": literature.get("search_queries", []),
            "candidate_papers_retrieved": literature.get("candidate_papers_retrieved", 0),
            "papers_passing_filter": literature.get("papers_passing_filter"),
            "retrieval": literature.get("retrieval") or {},
            "pmc": {
                "attempted": literature.get("pmc_attempted", 0),
                "retrieved": literature.get("pmc_fulltext_records", 0),
                "not_available": literature.get("pmc_not_available", 0),
                "errors": literature.get("pmc_errors", 0),
                "error_samples": literature.get("pmc_error_samples", []),
            },
            "total_records": literature.get("total_pubmed_records", 0),
            "direct_evidence": direct,
            "relevant_supporting_evidence": relevant_supporting,
            "related_evidence": related,
            "requires_verification": verify,
            "excluded_evidence": excluded,
            "evidence_note": (
                "Exact evidence requires the requested entity/relationship to be represented in the retrieved title/abstract. "
                "Family-context evidence may support a broader mechanism but is not treated as allele-specific proof. "
                "When the query names a specific organism, evidence from a different organism is downgraded to related "
                "and marked 'other_organism' rather than presented as direct evidence for the organism asked about."
            ),
            "references": literature.get("references", []),
        })

    reference_evidence = []
    for resolution in understanding.get("resolutions", []):
        selected = resolution.get("selected")
        if selected:
            reference_evidence.append({
                "mention_group": resolution.get("mention_group"),
                "mention": resolution.get("mention"),
                "status": resolution.get("status"),
                "canonical_name": selected.get("canonical_name"),
                "aro_id": selected.get("aro_id"),
                "source": selected.get("source"),
                "match_type": selected.get("match_type"),
                "match_score": selected.get("match_score"),
                "synonyms": selected.get("synonyms", []),
                "description": selected.get("description"),
                "specificity": selected.get("specificity"),
            })

    literature_mutations = extract_literature_mutations(literature_results, understanding, mutation_reference=ref)
    return {
        "reference_evidence": reference_evidence,
        "literature_evidence": analyzed,
        "literature_mutations": literature_mutations,
        "evidence_policy": {
            "reference": "CARD/NCBI identity and annotation evidence; it is not itself a published experimental result.",
            "direct": "PubMed and federated literature evidence that directly represents the requested entity/relationship in the retrieved title/abstract.",
            "relevant_supporting": "Federated literature evidence addressing a parent family, closely related mechanism, or comparative context that genuinely informs the answer.",
            "family_context": "PubMed evidence about a parent AMR family or broader determinant that can explain context but is not allele-specific proof.",
            "related": "AMR literature that overlaps with part of the query but does not establish the complete requested relationship.",
            "excluded": "Papers that fail relevance criteria, match incidental keywords only, discuss unrelated organisms/phenotypes, or lack connection to the query.",
            "hypothesis": "Allowed only when direct evidence is absent and related evidence supports a clearly labelled inference.",
            "mutation_reference": "Specialized database records identify mutation candidates and database-level phenotype grading; they must not be presented as primary literature unless a cited paper supports the claim.",
        },
    }
