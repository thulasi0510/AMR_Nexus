"""ChEBI 2.0 REST client with a local-OBO fallback.

ChEBI's current public REST API is preferred for antibiotic normalization.
The bundled FULL OBO file remains a deterministic fallback when the API is
temporarily unavailable.
"""
from functools import lru_cache
import re
import requests

from config import CHEBI_API_URL, API_TIMEOUT, CHEBI_OBO_PATH, CHEBI_AMR_INDEX_PATH


def _clean(v):
    return re.sub(r"\s+", " ", str(v or "").strip())


@lru_cache(maxsize=128)
def search_chebi(name, size=8):
    name = _clean(name)
    if not name:
        return {"status": "empty", "query": name, "results": []}

    # Current ChEBI 2.0 public endpoint.
    try:
        r = requests.get(
            f"{CHEBI_API_URL}/es_search/",
            params={"query": name, "size": int(size)},
            timeout=API_TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        results = payload.get("results", payload if isinstance(payload, list) else [])
        normalized = []
        for item in results or []:
            if not isinstance(item, dict):
                continue
            normalized.append({
                "chebi_id": item.get("chebiId") or item.get("chebi_id") or item.get("id"),
                "name": item.get("chebiAsciiName") or item.get("chebiName") or item.get("name"),
                "synonyms": item.get("synonyms") or [],
                "definition": item.get("definition"),
            })
        if normalized:
            return {"status": "normalized", "query": name, "results": normalized, "source": "ChEBI REST API"}
    except (requests.RequestException, ValueError, TypeError):
        pass

    # Small, cached OBO fallback. We scan only once per process and only retain
    # name/synonym/definition/ID fields; no structures are loaded into memory.
    return _search_obo(name, size)


@lru_cache(maxsize=1)
def _obo_index():
    index = {}
    path = CHEBI_OBO_PATH
    if not path:
        return index
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            stanza = {}
            for raw in fh:
                line = raw.rstrip("\n")
                if not line:
                    if stanza.get("id", "").startswith("CHEBI:"):
                        rec = {
                            "chebi_id": stanza.get("id"),
                            "name": stanza.get("name"),
                            "synonyms": stanza.get("synonyms", []),
                            "definition": stanza.get("def"),
                        }
                        for term in [rec["name"], *rec["synonyms"]]:
                            if term:
                                key = _clean(term).lower()
                                index.setdefault(key, []).append(rec)
                    stanza = {}
                    continue
                if line.startswith("id: "):
                    stanza["id"] = line[4:]
                elif line.startswith("name: "):
                    stanza["name"] = line[6:]
                elif line.startswith("def: "):
                    stanza["def"] = line[5:].split(" [", 1)[0].strip('"')
                elif line.startswith("synonym: "):
                    m = re.match(r'synonym:\s+"([^"]+)"', line)
                    if m:
                        stanza.setdefault("synonyms", []).append(m.group(1))
            return index
    except (OSError, UnicodeError):
        return {}


def _search_obo(name, size):
    q = _clean(name).lower()
    if not q:
        return {"status": "not_found", "query": name, "results": []}

    # Prefer the compact project-specific index derived from the bundled FULL
    # ChEBI OBO. This keeps the offline fallback fast without pretending that
    # the entire 270 MB ontology must be reparsed for every query.
    try:
        import json
        if CHEBI_AMR_INDEX_PATH:
            with open(CHEBI_AMR_INDEX_PATH, encoding="utf-8") as fh:
                small = json.load(fh)
            if q in small:
                return {
                    "status": "normalized",
                    "query": name,
                    "results": [small[q]],
                    "source": "ChEBI OBO project index",
                }
            hits = []
            for term, rec in small.items():
                if q in term:
                    hits.append(rec)
                    if len(hits) >= size:
                        break
            if hits:
                return {
                    "status": "normalized",
                    "query": name,
                    "results": hits[:size],
                    "source": "ChEBI OBO project index",
                }
    except (OSError, ValueError, TypeError):
        pass

    # Last-resort full OBO search.
    hits = []
    for rec in _obo_index().get(q, []):
        if rec not in hits:
            hits.append(rec)
    return {
        "status": "normalized" if hits else "not_found",
        "query": name,
        "results": hits[:size],
        "source": "ChEBI OBO fallback",
    }


def best_match(name):
    result = search_chebi(name)
    results = result.get("results", [])
    if not results:
        return result
    q = _clean(name).lower()
    exact = next((x for x in results if _clean(x.get("name")).lower() == q), None)
    return {**result, "selected": exact or results[0]}
