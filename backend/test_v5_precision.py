import os
from agents.query_understanding import understand_query
from agents.mutation_reference import retrieve_mutations
from agents.response_synthesis import synthesize_response
from agents.literature import _mutation_query_strict_filter

q = "Which mutations in rpoB are associated with rifampicin resistance in Mycobacterium tuberculosis?"
u = understand_query(q)
u["query"] = q
r = retrieve_mutations(u)
assert len(r["mutations"]) <= 20
assert all("not associated" not in str(x.get("phenotype", "")).lower() for x in r["mutations"])
assert all(not str(x.get("mutation", "")).startswith("c.") for x in r["mutations"])

plan = {
    "intent": ["mutation_variant_information"],
    "entities": {"genes": ["rpoB"], "antibiotics": ["rifampicin"], "organisms": ["mycobacterium tuberculosis"]},
    "mutation_reference_mutations": ["S450L", "D435V"],
    "mutation_reference_reported": ["p.Ser450Leu", "p.Asp435Val"],
}
assert not _mutation_query_strict_filter({"title": "TB epidemiology", "abstract": "Mycobacterium tuberculosis rifampicin resistance prevalence and surveillance."}, plan)
assert _mutation_query_strict_filter({"title": "rpoB mutations", "abstract": "Mycobacterium tuberculosis rpoB S450L mutation was associated with rifampicin resistance."}, plan)
assert not _mutation_query_strict_filter({"title": "TB diagnostics", "abstract": "Mycobacterium tuberculosis rifampicin resistance detected by MALDI-TOF; rpoB mutations were discussed."}, plan)

e = {"mutation_reference": r, "reference_evidence": [], "literature_evidence": [], "literature_mutations": []}
out = synthesize_response(q, u, e, [])
assert "Peru" not in out
assert "MALDI" not in out
assert "macrophage" not in out.lower()
assert "R445Y" not in out
assert out.count("| Mutation |") == 1
assert out.count("| S450L |") == 1
print("V5 MUTATION PRECISION + ANTI-CONTAMINATION TESTS: PASS")
