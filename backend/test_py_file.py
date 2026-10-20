"""Clean interactive demonstration for the modular LangGraph AMR assistant.

Set AMR_DEBUG=1 when demonstrating the internal agent/retrieval workflow.
"""
import os
import re
from services.orchestrator import process_query

SEP = "=" * 100
DEBUG = os.getenv("AMR_DEBUG", "0") == "1"


def print_report(report):
    u = report.get("understanding", {})
    p = report.get("plan", {})

    print(f"\n{SEP}\nAMR RESEARCH ASSISTANT\n{SEP}")
    print(f"Query: {report.get('query')}")

    if report.get("status") == "pending_selection":
        print("\nA reference candidate needs confirmation before literature retrieval.")
        for group in report.get("candidate_groups", []):
            print(f"\nMention: {group.get('mention')}")
            for candidate in group.get("candidates", [])[:15]:
                print(
                    f"  [{candidate.get('candidate_label')}] "
                    f"{candidate.get('canonical_name')} | "
                    f"{candidate.get('source')} | "
                    f"{candidate.get('match_type')} | "
                    f"score={candidate.get('match_score', 'N/A')}"
                )
        return

    # User-facing mode: answer first, followed by only the references.
    print(f"\n{SEP}\nRESEARCH ANSWER\n{SEP}")
    print(report.get("answer", "").strip() or "No answer was generated.")

    refs = report.get("references", [])
    # Mutation-focused answers may already contain their own References section.
    # Avoid printing the same references a second time in the terminal.
    answer_has_references = bool(
        re.search(r"(?im)^\s*#{1,4}\s*references\s*$", report.get("answer", ""))
    )
    if refs and not answer_has_references:
        print(f"\n{SEP}\nREFERENCES\n{SEP}")
        for ref in refs:
            n = ref.get("reference_number")
            authors = ", ".join(ref.get("authors", [])[:6])
            if authors:
                authors += ". "
            print(f"[{n}] {authors}{ref.get('title')}")
            print(
                f"    {ref.get('journal') or ''}"
                f"{', ' + str(ref.get('year')) if ref.get('year') else ''}"
                f" | PMID: {ref.get('pmid') or 'N/A'}"
            )
            if ref.get("pubmed_url"):
                print(f"    {ref.get('pubmed_url')}")

    if DEBUG:
        print(f"\n{SEP}\nRETRIEVAL / AGENT DEBUG\n{SEP}")
        print(f"Domain: {u.get('domain')}")
        print(f"Intent: {', '.join(u.get('intent', []))}")
        print(f"Genes: {u.get('entities', {}).get('genes', [])}")
        print(f"Antibiotics: {u.get('entities', {}).get('antibiotics', [])}")
        print(
            f"Mutations/variants: "
            f"{u.get('entities', {}).get('mutations', []) + u.get('entities', {}).get('variants', [])}"
        )
        print(f"Sources selected: {', '.join(p.get('sources', [])) or 'none'}")
        for item in report.get("literature_results", []):
            lit = item.get("literature", {})
            print(f"\nSearch queries:")
            for query in lit.get("search_queries", []):
                print(f"  - {query}")
            print(
                f"PubMed main-query records: {lit.get('total_pubmed_records', 0)} | "
                f"Unique records retrieved across searches: {lit.get('candidate_papers_retrieved', 0)} | "
                f"Evidence candidates passed to synthesis: {len(lit.get('answer_candidates', []))} | "
                f"Concise displayed set: {len(lit.get('top_papers', []))}"
            )
        trace = report.get("execution_trace", [])
        if trace:
            print("\nLangGraph agents: " + " -> ".join(
                step.get("agent", "") for step in trace
            ))


def main():
    print("Enter a mutation-focused AMR research query.")
    print("Type 'exit' to stop.")

    while True:
        query = input("\nYour query: ").strip()
        if query.lower() == "exit":
            break
        if not query:
            continue

        try:
            report = process_query(query)
            print_report(report)

            if report.get("status") == "pending_selection":
                choice = input(
                    "\nSelect a candidate label (e.g. 1A or 2B), "
                    "or press Enter to skip: "
                ).strip()
                if choice:
                    print_report(process_query(query, selected_candidate=choice))
        except Exception as exc:
            print(f"\nERROR: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
