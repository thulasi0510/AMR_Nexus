from agents.literature import fetch_pubmed_count, fetch_papers
import traceback

q = "mgrB AND colistin AND Klebsiella pneumoniae"

for name, fn in [
    ("COUNT", lambda: fetch_pubmed_count(q)),
    ("PAPERS", lambda: fetch_papers(q, limit=3)),
]:
    print("\n---", name, "---")
    try:
        result = fn()
        print("SUCCESS:", result if name == "COUNT" else f"{len(result)} papers")
    except Exception:
        traceback.print_exc()
