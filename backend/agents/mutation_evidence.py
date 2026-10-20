"""Extract explicitly reported mutation/phenotype statements from relevant literature.

This is a conservative text-extraction layer, not a mutation-effect predictor.
A paper contributes only if it passes the same content-based assessment used by
the PubMed filter (agents.mutation_matching.assess_paper): gene + drug + queried
organism as the subject + a mutation in gene context. Mutations are recognised in
any accepted notation and reported in the reference's own one-letter form, with
the wording used by the paper kept in "as_written".

Source classifications are never overwritten: a literature mention of a mutation
that is graded in the reference keeps the reference grade in
"reference_classification"; the paper's wording is reported separately.
"""
import re

from agents.mutation_matching import AA3, assess_paper, parse_substitution, records_for_matching  # AA3 re-exported for compatibility
from agents.entity_aliases import canonical_organism, drug_hits, drug_resistance_link

_NEGATED_ASSOCIATION = re.compile(
    r"\b(?:not|no|never|neither|without|lack(?:s|ed)?|fail(?:s|ed)?\s+to)\b"
    r"[^.;]{0,80}\b(?:associat\w*|link\w*|correlat\w*|confer\w*|caus\w*|resistan\w*|susceptib\w*)\b",
    re.I,
)

_RESISTANCE_LINK = re.compile(
    r"(?:"
    r"\b(?:strongly\s+|significantly\s+|closely\s+)?(?:associated|linked|correlated|related)\s+(?:with|to)\s+[^.;]{0,70}\bresistan\w*\b"
    r"|\bresistan\w*(?:\s+to\s+[\w-]+)?\s+(?:is|are|was|were|has been|have been)?\s*(?:strongly\s+|significantly\s+)?(?:associated|linked|correlated|related)\s+(?:with|to)\b"
    r"|\b(?:resistance[- ]associated|resistance[- ]conferring|resistance[- ]mediating)\b"
    r"|\bconfer(?:s|red|ring)?\s+(?:high[- ]level\s+|low[- ]level\s+)?[^.;]{0,35}\bresistan\w*\b"
    r"|\bresistan\w*[^.;]{0,80}\bdriven\s+by\b"
    r"|\bcaus(?:e|es|ed|ing)\s+(?:high[- ]level\s+|low[- ]level\s+)?[^.;]{0,25}\bresistan\w*\b"
    r"|\bcontribut(?:e|es|ed|ing)\s+to\s+[^.;]{0,35}\bresistan\w*\b"
    r"|\bimplicated\s+in\s+[^.;]{0,30}\bresistan\w*\b"
    r"|\bresistan\w*\s+(?:isolates?|strains?|variants?|mutants?|patients?|cases?)\b"
    r"|\b(?:isolates?|strains?|variants?|mutants?)\b[^.;]{0,50}\bresistan\w*\b"
    r"|\bresistant\b"
    r")",
    re.I,
)

_SUSCEPTIBILITY_LINK = re.compile(
    r"(?:"
    r"\b(?:associated|linked|correlated|related)\s+(?:with|to)\s+[^.;]{0,70}\bsusceptib\w*\b"
    r"|\bsusceptib\w*[- ]associated\b"
    r"|\bsusceptib\w*\s+(?:isolates?|strains?|variants?|mutants?)\b"
    r"|\b(?:isolates?|strains?|variants?|mutants?)\b[^.;]{0,50}\bsusceptib\w*\b"
    r"|\bsusceptible\b"
    r")",
    re.I,
)


def classify_sentence_phenotype(sentence, antibiotics=None):
    """Classify only an explicit mutation–phenotype relation in its own sentence.

    Merely containing the word 'resistance' is insufficient: phrases such as
    'resistance-determining region' describe a genomic region, not an allele's
    phenotype. If a drug was queried, the sentence must specifically link resistance
    to that drug (or its class/synonyms) rather than citing the drug only as an
    isolate/organism descriptor (e.g. 'vancomycin-resistant enterococci' in a linezolid study).
    """
    text = str(sentence or "")
    low = text.lower()
    if antibiotics and not any(drug_resistance_link(text, drug) for drug in antibiotics):
        return "phenotype not explicit in extracted sentence"
    if _NEGATED_ASSOCIATION.search(low):
        return "phenotype not explicit in extracted sentence"
    has_resistance = bool(_RESISTANCE_LINK.search(low))
    has_susceptibility = bool(_SUSCEPTIBILITY_LINK.search(low))
    if has_resistance == has_susceptibility:
        return "phenotype not explicit in extracted sentence"
    if has_susceptibility:
        return "susceptibility-associated evidence"
    return "resistance-associated evidence"


def classify_evidence_type(sentence):
    """Classify the strength/type of a mutation claim without inferring causality."""
    low = str(sentence or "").lower()
    if re.search(r"\b(?:predicted|in silico|computational|model(?:led|ing)?|simulat(?:ed|ion))\b", low):
        return "computational prediction"
    if re.search(r"\b(?:detect(?:ed|ion)|assay|diagnostic|screen(?:ing)?|crisp?r|amplification)\b", low):
        return "diagnostic detection / association"
    if re.search(r"\b(?:isolates?|clinical samples?|patients?|surveillance|observed in|identified in)\b", low):
        return "clinical or observational association"
    if re.search(r"\b(?:experiment(?:al|ally)?|functional characterization|enzymatic|biochemical|MIC|minimum inhibitory|complementation|constructed mutant|mutagenesis)\b", low):
        return "direct experimental evidence"
    return "mutation–phenotype association"


# Conservative protein-length sanity checks for well-characterized gene/species
# pairs and universal AMR determinant lengths.
_PROTEIN_LENGTH_MAX = {
    ("katg", "Mycobacterium tuberculosis"): 740,
    ("gyra", "Mycobacterium tuberculosis"): 838,
    ("gyra", "Escherichia coli"): 875,
    ("parc", "Escherichia coli"): 752,
    ("vana", "Enterococcus faecium"): 343,
    ("vana", "Enterococcus faecalis"): 343,
}

_CANONICAL_GENE_LENGTHS = {
    "vana": 343,
    "vanb": 343,
    "katg": 740,
    "gyra": 875,
    "parc": 752,
    "ddl": 360,
    "rpob": 1342,
}


def _mutation_position_plausible(mutation, genes, organisms):
    """Return False for impossible residue positions in known gene/species pairs.

    Only simple amino-acid substitutions are checked. Positions > 2000 are rejected
    for bacterial proteins as they indicate rRNA nucleotide positions or genomic coordinates.
    """
    sub = parse_substitution(mutation)
    if not sub:
        return True
    pos = sub[1]
    gene_keys = {str(g or "").strip().lower() for g in (genes or [])}
    organism_keys = {canonical_organism(o) for o in (organisms or []) if str(o or "").strip()}

    # Species-specific limit
    limits = [limit for (gene, organism), limit in _PROTEIN_LENGTH_MAX.items()
              if gene in gene_keys and organism in organism_keys]
    if limits and pos > min(limits):
        return False

    # Gene-level universal length guards
    for g, limit in _CANONICAL_GENE_LENGTHS.items():
        if g in gene_keys and pos > limit:
            return False

    # Universal bacterial protein sanity bound
    if pos > 2000:
        return False

    return True


def mutation_position_issue(mutation, genes, organisms):
    """Return a precise validation issue, or ``None`` when plausible."""
    sub = parse_substitution(mutation)
    if not sub:
        return None
    pos = sub[1]
    gene_keys = {str(g or "").strip().lower() for g in (genes or [])}
    organism_keys = {canonical_organism(o) for o in organisms or [] if str(o or "").strip()}
    for (gene, organism), limit in _PROTEIN_LENGTH_MAX.items():
        if gene in gene_keys and organism in organism_keys and pos > limit:
            return f"position {pos} exceeds the known {gene} protein length (~{limit} residues) for {organism}"
    for g, limit in _CANONICAL_GENE_LENGTHS.items():
        if g in gene_keys and pos > limit:
            return f"position {pos} exceeds the known {g} protein length (~{limit} residues)"
    if pos > 2000:
        return f"position {pos} is not a plausible bacterial protein residue position (likely an rRNA nucleotide position or genomic coordinate)"
    return None


def _ref_records(mutation_reference):
    return records_for_matching(mutation_reference)


def _reference_lookup(mutation_reference):
    out = {}
    for rec in _ref_records(mutation_reference):
        key = str(rec.get("mutation") or "").upper()
        if key and key not in out:
            out[key] = rec
    return out


def extract_literature_mutations(literature_results, understanding, max_records=80, mutation_reference=None):
    entities = understanding.get("entities", {}) or {}
    genes = [str(x) for x in entities.get("genes", []) if x]
    organisms = [str(x) for x in entities.get("organisms", []) if x]
    antibiotics = [str(x) for x in entities.get("antibiotics", []) if x]
    # A gene-less query is valid when it names an explicit variant.
    if not (genes or entities.get("mutations") or entities.get("variants")):
        return []

    ref_records = _ref_records(mutation_reference)
    plan = {
        "entities": entities,
        "intent": ["mutation_variant_information"],
        "mutation_reference_mutations": [r.get("mutation") for r in ref_records if r.get("mutation")],
        "mutation_reference_reported": [r.get("reported_mutation") for r in ref_records if r.get("reported_mutation")],
    }
    ref_lookup = _reference_lookup(mutation_reference)
    query_text = str(understanding.get("original_query") or understanding.get("query") or "").lower()
    if re.search(r"\bnot\s+(?:associated\s+with\s+)?(?:rifampicin\s+|rifampin\s+)?resistan\w*\b", query_text):
        requested_direction = "non_resistance"
    elif re.search(r"\b(susceptib\w*|sensitive\s+to)\b", query_text) and not re.search(r"\bresistan\w*\b", query_text):
        requested_direction = "susceptibility"
    elif re.search(r"\bresistan\w*\b", query_text):
        requested_direction = "resistance"
    else:
        requested_direction = None

    found_by_key = {}
    for item in literature_results or []:
        for paper in item.get("literature", {}).get("papers", []) or []:
            assessment = assess_paper(paper, plan)
            if not assessment["eligible"]:
                continue
            for match in assessment["reference_matches"] + assessment["other_substitutions"]:
                # A context-window match is not enough if the residue position
                # cannot exist in the queried protein. This specifically blocks
                # cross-gene/genomic-coordinate contamination such as G944C being
                # displayed as an M. tuberculosis katG amino-acid substitution.
                if not _mutation_position_plausible(match.get("mutation"), genes, organisms):
                    continue
                key = (match["mutation"], paper.get("pmid"))
                ref = ref_lookup.get(match["mutation"].upper())
                phenotype = classify_sentence_phenotype(match["sentence"], antibiotics)
                record = {
                    "mutation": match["mutation"],
                    "as_written": match["as_written"],
                    "numbering": match["numbering"],
                    "phenotype": phenotype,
                    "gene": match.get("gene"),
                    "source": "Primary literature",
                    "source_url": paper.get("pubmed_url"),
                    "pmid": paper.get("pmid"),
                    "title": paper.get("title"),
                    "organism_specific": True,
                    "in_reference": bool(ref),
                    "evidence_sentence": match["sentence"][:500],
                    "evidence_type": classify_evidence_type(match["sentence"]),
                }
                if ref:
                    record["reference_classification"] = {
                        "phenotype": ref.get("phenotype"),
                        "confidence": ref.get("confidence"),
                        "source": ref.get("source"),
                    }
                old = found_by_key.get(key)
                # Prefer a sentence that explicitly links this mutation to the
                # queried phenotype over a title/mention that contains no such
                # relationship. Never replace a stronger evidence sentence.
                if requested_direction == "resistance":
                    ranks = {"resistance-associated evidence": 3, "susceptibility-associated evidence": 1,
                             "phenotype not explicit in extracted sentence": 0}
                elif requested_direction == "susceptibility":
                    ranks = {"susceptibility-associated evidence": 3, "resistance-associated evidence": 1,
                             "phenotype not explicit in extracted sentence": 0}
                else:
                    ranks = {"resistance-associated evidence": 2, "susceptibility-associated evidence": 2,
                             "phenotype not explicit in extracted sentence": 0}
                rank = ranks.get(phenotype, 0)
                old_rank = ranks.get((old or {}).get("phenotype"), -1)
                if old is None or rank > old_rank:
                    found_by_key[key] = record
    return list(found_by_key.values())[:max_records]
    return found
