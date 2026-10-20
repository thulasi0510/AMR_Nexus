from app import app
import json

query = "Which mutations in mgrB are associated with colistin resistance in Klebsiella pneumoniae?"

with app.test_client() as client:
    response = client.post(
        "/query?debug=1",
        json={"query": query}
    )

    print("HTTP STATUS:", response.status_code)
    data = response.get_json() or {}

    print("\nREPORT STATUS:", data.get("status"))
    print("\nANSWER:\n", data.get("answer", data.get("message")))

    print("\nRETRIEVAL:")
    for item in data.get("literature_results", []):
        literature = item.get("literature", {})
        print(json.dumps(literature.get("retrieval", {}), indent=2))

    print("\nEXECUTION TRACE:")
    print(json.dumps(data.get("execution_trace", []), indent=2))
