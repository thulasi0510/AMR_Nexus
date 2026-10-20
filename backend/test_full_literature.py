from agents.literature import search_literature
import traceback

plan = {
    "query": "Which mutations in mgrB are associated with colistin resistance in Klebsiella pneumoniae?",
    "entities": {
        "genes": ["mgrB"],
        "antibiotics": ["colistin"],
        "organisms": ["Klebsiella pneumoniae"],
        "mutations": [],
    },
    "intent": ["mutation_variant_information"],
}

try:
    result = search_literature(plan)
    print("Queries:", result.get("search_queries"))
    print("Retrieval:", result.get("retrieval"))
    print("Errors:", result.get("errors"))
    print("Retrieved:", result.get("candidate_papers_retrieved"))
except Exception:
    traceback.print_exc()
