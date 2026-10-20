"""Database-backed entity normalization agent.

Organisms -> NCBI Taxonomy
Antibiotics -> ChEBI 2.0 REST (with local ChEBI OBO fallback)

Rules:
* One entity per real-world thing: rifampin/rifampicin, "M. tuberculosis" /
  "Mycobacterium tuberculosis" / "Mtb" collapse to a single canonical name.
  Accepted aliases are kept in understanding["entity_aliases"] for matching and
  PubMed query building; they are NOT repeated in the entity lists.
* A database hit is accepted as the canonical name only when it is an exact
  name/synonym match. A loosely-similar first hit is recorded but never
  replaces what the user (or the lexicon) named.
* A database being unreachable never blocks the pipeline.
"""
from agents.entity_aliases import (
    canonical_drug,
    canonical_organism,
    dedupe_by_key,
    drug_terms,
    entity_key,
    is_known_organism,
    organism_terms,
)
from agents.amr_lexicon import ANTIBIOTIC_ALIASES
from services.taxonomy_client import normalize_organism
from services.chebi_client import best_match

_CLASS_TERMS = {
    "fluoroquinolone", "carbapenem", "cephalosporin", "aminoglycoside",
    "glycopeptide", "macrolide", "beta-lactam", "sulfonamide",
}


def _chebi_exact(result, query):
    """Return the selected ChEBI record only if it is an exact name/synonym match."""
    selected = result.get("selected") or {}
    q = entity_key(query)
    names = [selected.get("name"), *(selected.get("synonyms") or [])]
    return selected if any(entity_key(n) == q for n in names if n) else {}


def normalize_entities(understanding):
    entities = dict(understanding.get("entities") or {})

    organism_resolutions, organisms = [], []
    for organism in dedupe_by_key(entities.get("organisms"), canonical_organism):
        canonical = canonical_organism(organism)
        result = ({"status": "normalized", "canonical_name": canonical, "source": "local AMR lexicon"}
                  if is_known_organism(canonical) else normalize_organism(canonical))
        organism_resolutions.append(result)
        if is_known_organism(canonical):
            organisms.append(canonical)
        elif result.get("status") == "normalized":
            organisms.append(result.get("canonical_name") or canonical)
        else:
            organisms.append(canonical)

    antibiotic_resolutions, antibiotics = [], []
    for antibiotic in dedupe_by_key(entities.get("antibiotics"), canonical_drug):
        canonical = canonical_drug(antibiotic)
        if canonical.lower() in _CLASS_TERMS:
            antibiotic_resolutions.append({
                "status": "class_term",
                "query": antibiotic,
                "canonical_name": canonical.lower(),
                "source": "deterministic AMR lexicon",
            })
            antibiotics.append(canonical.lower())
            continue
        if canonical.lower() in ANTIBIOTIC_ALIASES:
            result = {"status": "normalized", "selected": {"name": canonical, "synonyms": [canonical]}, "source": "local AMR lexicon"}
        else:
            result = best_match(canonical)
        exact = _chebi_exact(result, canonical)
        # Known lexicon drugs keep their lexicon name; ChEBI contributes the ID.
        name = canonical if canonical.lower() in ANTIBIOTIC_ALIASES else (exact.get("name") or canonical)
        antibiotic_resolutions.append({
            **result,
            "query": antibiotic,
            "canonical_name": name,
            "chebi_id": exact.get("chebi_id"),
        })
        antibiotics.append(name)

    entities["organisms"] = dedupe_by_key(organisms, canonical_organism)
    entities["antibiotics"] = dedupe_by_key(antibiotics, canonical_drug)

    understanding = dict(understanding)
    understanding["entities"] = entities
    understanding["organism_resolutions"] = organism_resolutions
    understanding["antibiotic_resolutions"] = antibiotic_resolutions
    understanding["entity_aliases"] = {
        "organisms": {o: organism_terms(o) for o in entities["organisms"]},
        "antibiotics": {a: drug_terms(a, include_abbreviations=True) for a in entities["antibiotics"]},
    }
    return understanding
