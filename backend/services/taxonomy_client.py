"""NCBI Taxonomy API client used for organism normalization.

Uses NCBI E-utilities (Taxonomy database). The service is deliberately small:
it validates a user-supplied organism name and returns a canonical scientific
name, TaxID, and common aliases when available. Failures are non-fatal so the
local deterministic organism lexicon remains available offline.
"""
from functools import lru_cache
import requests

from config import NCBI_EUTILS_URL, NCBI_API_KEY, NCBI_TOOL, NCBI_EMAIL, API_TIMEOUT


def _params(extra):
    params = dict(extra)
    if NCBI_TOOL:
        params["tool"] = NCBI_TOOL
    if NCBI_EMAIL:
        params["email"] = NCBI_EMAIL
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    return params


@lru_cache(maxsize=128)
def normalize_organism(name):
    name = str(name or "").strip()
    if not name:
        return {"status": "empty", "query": name}

    try:
        r = requests.get(
            f"{NCBI_EUTILS_URL}/esearch.fcgi",
            params=_params({"db": "taxonomy", "term": name, "retmode": "json", "retmax": 5}),
            timeout=API_TIMEOUT,
        )
        r.raise_for_status()
        ids = r.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return {"status": "not_found", "query": name, "candidates": []}

        s = requests.get(
            f"{NCBI_EUTILS_URL}/esummary.fcgi",
            params=_params({"db": "taxonomy", "id": ",".join(ids), "retmode": "json"}),
            timeout=API_TIMEOUT,
        )
        s.raise_for_status()
        result = s.json().get("result", {})
        records = []
        for taxid in ids:
            rec = result.get(str(taxid), {})
            if not rec:
                continue
            records.append({
                "taxid": str(taxid),
                "scientific_name": rec.get("scientificname") or rec.get("scientificName"),
                "common_name": rec.get("commonname") or rec.get("commonName"),
                "rank": rec.get("rank"),
                "division": rec.get("division"),
            })

        if not records:
            return {"status": "not_found", "query": name, "candidates": []}

        chosen = records[0]
        return {
            "status": "normalized",
            "query": name,
            "canonical_name": chosen.get("scientific_name") or name,
            "taxid": chosen.get("taxid"),
            "rank": chosen.get("rank"),
            "common_name": chosen.get("common_name"),
            "candidates": records,
            "source": "NCBI Taxonomy",
        }
    except (requests.RequestException, ValueError, TypeError) as exc:
        return {
            "status": "unavailable",
            "query": name,
            "error": f"{type(exc).__name__}: {exc}",
            "source": "NCBI Taxonomy",
        }
