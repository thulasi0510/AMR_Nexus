import os
import agents.query_understanding as q
from agents.mutation_reference import _who_grade, retrieve_mutations


def understand_without_llm(text):
    q._llm_understand = lambda _: {}
    return q.understand_query(text)


def main():
    good = understand_without_llm(
        "Which mutations in rpoB are associated with rifampicin resistance in Mycobacterium tuberculosis?"
    )
    assert good["domain"] == "amr" and good["scope_ok"] is True
    assert "rpoB" in good["entities"]["genes"]
    assert good["entities"]["organisms"]

    specific = understand_without_llm(
        "Is S450L in rpoB associated with rifampicin resistance in Mycobacterium tuberculosis?"
    )
    assert specific["domain"] == "amr" and "S450L" in specific["entities"]["mutations"]

    # Generalized AMR scope: mechanisms, determinants, and general AMR questions are in-scope
    for amr_q in (
        "What is rpoB?",
        "How does rpoB confer rifampicin resistance?",
        "What are the mechanisms of antimicrobial resistance?",
    ):
        res = understand_without_llm(amr_q)
        assert res["domain"] == "amr" and res["scope_ok"] is True, f"Expected {amr_q} to be in-scope AMR"

    # Truly non-AMR queries must be out-of-scope
    for non_amr in (
        "What is the capital of France?",
        "How do I bake chocolate cookies?",
        "What is the weather in Tokyo?",
    ):
        res = understand_without_llm(non_amr)
        assert res["domain"] == "out_of_scope" and res["scope_ok"] is False, f"Expected {non_amr} to be out-of-scope"

    assert _who_grade("1) Assoc w R") == "resistance-associated"
    assert _who_grade("2) Assoc w R - Interim") == "resistance-associated (interim)"
    assert _who_grade("5) Not assoc w R") == "not associated with resistance"

    ref = retrieve_mutations({
        "entities": {
            "genes": ["rpoB"],
            "organisms": ["Mycobacterium tuberculosis"],
            "antibiotics": ["rifampicin"],
            "mutations": ["S450L"],
        },
        "organism_resolutions": [],
    })
    assert ref["status"] == "found"
    assert len(ref["mutations"]) == 1
    assert ref["mutations"][0]["mutation"] == "S450L"
    assert ref["mutations"][0]["phenotype"] == "resistance-associated"

    print("FINAL SCOPE TESTS: PASS")


if __name__ == "__main__":
    main()

# Precision regression: a resistance query must not surface WHO non-resistance
# or nucleotide-level catalogue records in the default mutation answer.
from agents.mutation_reference import retrieve_mutations

u = {
    "query": "Which mutations in rpoB are associated with rifampicin resistance in Mycobacterium tuberculosis?",
    "entities": {
        "genes": ["rpoB"],
        "organisms": ["Mycobacterium tuberculosis"],
        "antibiotics": ["rifampicin"],
        "mutations": [],
    },
    "organism_resolutions": [],
}
r = retrieve_mutations(u)
assert r["mutations"], "WHO mutation reference returned no resistance records"
assert len(r["mutations"]) <= 20, "User-facing mutation set is not capped"
assert all(str(x.get("mutation", "")).startswith("p.") or not str(x.get("mutation", "")).startswith("c.") for x in r["mutations"])
assert not any("not associated" in str(x.get("phenotype", "")).lower() for x in r["mutations"])
assert not any("uncertain" in str(x.get("phenotype", "")).lower() for x in r["mutations"])
assert any(x.get("mutation") == "S450L" for x in r["mutations"]), "Expected S450L missing"
assert any(x.get("mutation") == "H445Y" for x in r["mutations"]), "Expected H445Y missing"
assert not any(x.get("mutation") == "R445Y" for x in r["mutations"]), "Invalid R445Y mutation leaked into structured result"
print("MUTATION PRECISION TESTS: PASS")


# v4 regression: CLI must not advertise unrelated example queries.
cli_text = open(os.path.join(os.path.dirname(__file__), "test_py_file.py"), encoding="utf-8").read()
assert "MCR-1.1" not in cli_text
assert "What mutations cause rifampicin resistance?" not in cli_text
print("V4 MUTATION SCOPE TESTS: PASS")
