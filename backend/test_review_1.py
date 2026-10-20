from agents.query_understanding import understand_query

query = "blaSHV-1 and blaVIM-2 were detected in the isolate"

result = understand_query(query)

print("\n" + "=" * 80)
print("QUERY")
print("=" * 80)
print(query)

print("\n" + "=" * 80)
print("GENE CANDIDATES")
print("=" * 80)

genes = result.get("all_genes", [])

print(f"Total candidates: {len(genes)}")

for i, gene in enumerate(genes, 1):
    print(f"\n[{i}]")
    print(f"  Mention    : {gene.get('mention')}")
    print(f"  Canonical  : {gene.get('canonical_name')}")
    print(f"  ARO ID     : {gene.get('aro_id')}")
    print(f"  Match type : {gene.get('match_type')}")
    print(f"  Source     : {gene.get('source')}")
    print(f"  Synonyms   : {gene.get('synonyms', [])}")

print("\n" + "=" * 80)
print("LITERATURE SUMMARY")
print("=" * 80)

for item in result.get("literature_results", []):
    literature = item.get("literature", {})

    print(f"\nGene: {item.get('gene')}")
    print(f"Search query : {literature.get('search_query')}")
    print(f"Evidence    : {literature.get('evidence_level')}")
    print(f"Papers      : {len(literature.get('papers', []))}")
    print(f"References  : {len(literature.get('references', []))}")