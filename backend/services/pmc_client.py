"""NCBI PMC Open Access BioC client.

Only retrieves full text for a small number of already-relevant PMCID/PMID
records. This avoids bulk retrieval and follows PMC's supported BioC API.

fetch_full_text_with_status() distinguishes:
  retrieved       full text obtained
  not_available   the article is not in the PMC open-access subset (HTTP 404 / empty)
  error           network/service/parse failure (says nothing about availability)
fetch_full_text() keeps the original "dict or None" contract.
"""
import requests

from config import PMC_BIOC_URL, API_TIMEOUT, NCBI_API_KEY, NCBI_TOOL, NCBI_EMAIL


def fetch_full_text_with_status(pmid=None, pmcid=None):
    identifier = pmcid or pmid
    if not identifier:
        return {"status": "not_available", "reason": "no identifier"}

    url = f"{PMC_BIOC_URL}/BioC_json/{identifier}/unicode"
    params = {}
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    if NCBI_TOOL:
        params["tool"] = NCBI_TOOL
    if NCBI_EMAIL:
        params["email"] = NCBI_EMAIL

    try:
        r = requests.get(url, params=params, timeout=API_TIMEOUT)
        # 4xx (other than 429) means PMC has no open-access copy of this record: not a failure.
        if 400 <= r.status_code < 500 and r.status_code != 429:
            return {"status": "not_available", "reason": f"HTTP {r.status_code}"}
        r.raise_for_status()  # 429 / 5xx -> error
        try:
            payload = r.json()
        except ValueError:
            # PMC answers HTTP 200 with a plain-text "No result can be found" for records
            # outside the open-access subset.
            return {"status": "not_available", "reason": f"non-JSON reply: {r.text[:60].strip()!r}"}
        passages = []
        for doc in payload if isinstance(payload, list) else [payload]:
            for d in doc.get("documents", []) if isinstance(doc, dict) else []:
                for p in d.get("passages", []) or []:
                    text = (p.get("text") or "").strip()
                    if text:
                        passages.append(text)
        full_text = "\n".join(passages)
        if full_text:
            return {
                "status": "retrieved",
                "pmid": str(pmid) if pmid else None,
                "pmcid": pmcid,
                "text": full_text,
                "source": "PMC BioC API",
            }
        return {"status": "not_available", "reason": "empty document"}
    except (requests.RequestException, TypeError) as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}


def fetch_full_text(pmid=None, pmcid=None):
    result = fetch_full_text_with_status(pmid=pmid, pmcid=pmcid)
    return result if result.get("status") == "retrieved" else None
