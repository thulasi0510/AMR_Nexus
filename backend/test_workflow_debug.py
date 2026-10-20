from services.orchestrator import research_graph
import traceback

query = "Which mutations in mgrB are associated with colistin resistance in Klebsiella pneumoniae?"

try:
    result = research_graph.invoke({"query": query})
    print("\n--- EXECUTION TRACE ---")
    for item in result.get("execution_trace", []):
        print(item)
    print("\n--- LITERATURE RETRIEVAL ---")
    for item in result.get("literature_results", []):
        literature = item.get("literature", {})
        print("Queries:", literature.get("search_queries"))
        print("Retrieval:", literature.get("retrieval"))
        print("Errors:", literature.get("errors"))
except Exception:
    traceback.print_exc()
