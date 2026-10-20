"""Bounded PMC full-text enrichment agent."""
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.pmc_client import fetch_full_text_with_status


def enrich_with_pmc(literature_results, max_articles=8):
    out = []
    for item in literature_results or []:
        literature = dict(item.get("literature", {}))
        papers = [dict(p) for p in (literature.get("papers") or literature.get("answer_candidates") or [])]
        fetched = attempted = unavailable = errors = 0
        error_samples = []
        candidates = [paper for paper in papers if paper.get("pmcid")][:max_articles]
        unavailable += sum(1 for paper in papers if not paper.get("pmcid"))
        attempted += len(candidates)
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(candidates)))) as pool:
            futures = {
                pool.submit(fetch_full_text_with_status, pmid=paper.get("pmid"), pmcid=paper.get("pmcid")): paper
                for paper in candidates
            }
            for future in as_completed(futures):
                paper = futures[future]
                try:
                    data = future.result()
                except Exception as exc:  # provider failure is structured, never fatal
                    data = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
                status = data.get("status")
                if status == "retrieved" and data.get("text"):
                    paper["full_text"] = data["text"]
                    paper["full_text_source"] = data.get("source")
                    fetched += 1
                elif status == "error":
                    errors += 1
                    if len(error_samples) < 3:
                        error_samples.append(f"{paper.get('pmcid')}: {data.get('reason')}")
                else:
                    unavailable += 1
        literature["papers"] = papers
        literature["pmc_fulltext_records"] = fetched
        literature["pmc_attempted"] = attempted
        literature["pmc_not_available"] = unavailable
        literature["pmc_errors"] = errors
        literature["pmc_error_samples"] = error_samples
        literature["pmc_source"] = "NCBI PMC BioC API"
        out.append({**item, "literature": literature})
    return out
