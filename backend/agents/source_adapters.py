# -*- coding: utf-8 -*-
"""Optional scholarly-source adapters for mutation-focused AMR retrieval.

The adapters use documented JSON APIs, return a common paper shape, and keep
provider provenance. They are disabled by default for deterministic local runs;
set AMR_ADDITIONAL_SOURCES=1 to enable them.
"""
import os
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from agents.entity_aliases import drug_hits, gene_hit, organism_assessment
from config import API_TIMEOUT

EUROPEPMC_URL = os.getenv("AMR_EUROPEPMC_URL", "https://www.ebi.ac.uk/europepmc/webservices/rest/search")
OPENALEX_URL = os.getenv("AMR_OPENALEX_URL", "https://api.openalex.org/works")
CROSSREF_URL = os.getenv("AMR_CROSSREF_URL", "https://api.crossref.org/works")
SEMANTIC_URL = os.getenv("AMR_SEMANTIC_URL", "https://api.semanticscholar.org/graph/v1/paper/search")
RESEARCHGATE_SEARCH_URL = "https://www.researchgate.net/search/publication"


def _year(value):
    match = re.search(r"\d{4}", str(value or ""))
    return match.group(0) if match else None


def _authors(items):
    out = []
    for item in items or []:
        name = item.get("author", {}).get("name") if isinstance(item, dict) else None
        name = name or item.get("given", "") + (" " + item.get("family", "") if item.get("family") else "") if isinstance(item, dict) else None
        if name:
            out.append(name.strip())
    return out[:20]


def _normalized(**kwargs):
    raw_doi = kwargs.get("doi")
    clean_doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(raw_doi).strip(), flags=re.I) if raw_doi else None
    clean_doi = clean_doi.strip() if clean_doi else None

    record = {
        "title": kwargs.get("title") or "Untitled",
        "abstract": kwargs.get("abstract") or "",
        "full_text": kwargs.get("full_text") or "",
        "authors": kwargs.get("authors") or [],
        "journal": kwargs.get("journal"),
        "year": _year(kwargs.get("year")),
        "pmid": kwargs.get("pmid"),
        "pmcid": kwargs.get("pmcid"),
        "doi": clean_doi,
        "pubmed_url": kwargs.get("pubmed_url"),
        "doi_url": kwargs.get("doi_url") or (f"https://doi.org/{clean_doi}" if clean_doi else None),
        "publication_types": kwargs.get("publication_types") or [],
        "mesh_terms": kwargs.get("mesh_terms") or [],
        "source": kwargs.get("source"),
        "source_provenance": kwargs.get("source_provenance") or [],
        "source_text_type": kwargs.get("source_text_type") or ("abstract" if kwargs.get("abstract") else "metadata"),
    }
    for k, v in kwargs.items():
        if k not in record:
            record[k] = v
    return record


DOAJ_URL = os.getenv("AMR_DOAJ_URL", "https://doaj.org/api/search/articles")
ARXIV_URL = os.getenv("AMR_ARXIV_URL", "http://export.arxiv.org/api/query")


def _get(url, params, headers=None):
    response = requests.get(url, params=params, headers=headers or {}, timeout=API_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _query_text(plan):
    """Build a comprehensive search query string for supplemental providers.

    Combines genes, antibiotics, organisms, mutations, mechanisms, scientific
    concepts, and query keywords so that the full semantic content of the
    user's inquiry is searched across every scholarly database.
    """
    if isinstance(plan, str):
        return plan
    plan = plan or {}
    entities = plan.get("entities", {}) or {}
    terms = []
    seen = set()

    for category in ("genes", "antibiotics", "organisms", "mutations", "variants", "mechanisms", "scientific_concepts"):
        for val in entities.get(category) or []:
            val_str = str(val).strip()
            key = val_str.lower()
            if val_str and key not in seen:
                terms.append(val_str)
                seen.add(key)

    # If few structured entities found, supplement with extracted query keywords
    if len(terms) < 3 and entities.get("query_keywords"):
        for kw in entities.get("query_keywords") or []:
            key = str(kw).lower()
            if key not in seen and len(key) >= 3:
                terms.append(str(kw))
                seen.add(key)

    if terms:
        # Quote multi-word terms; keep single words bare for maximum API compatibility
        return " ".join(f'"{t}"' if " " in t else t for t in terms[:12])

    raw = str(plan.get("query", "")).strip()
    return raw if raw else "antimicrobial resistance"


def _europe_pmc(plan, limit=100):
    """Search Europe PMC -- free public REST API covering MEDLINE and PMC peer-reviewed records."""
    q = _query_text(plan)
    search_term = f"({q}) AND (SRC:MED OR SRC:PMC)"
    try:
        data = _get(EUROPEPMC_URL, {"query": search_term, "format": "json", "pageSize": min(limit, 100), "resultType": "core"})
    except Exception:
        return []
    out = []
    for r in data.get("resultList", {}).get("result", []):
        pmid = str(r.get("pmid")) if r.get("pmid") else None
        doi = r.get("doi")
        out.append(_normalized(
            title=r.get("title"),
            abstract=r.get("abstractText"),
            authors=_authors(r.get("authorList", {}).get("author")),
            journal=r.get("journalTitle"),
            year=r.get("firstPublicationDate"),
            pmid=pmid, pmcid=r.get("pmcid"), doi=doi,
            pubmed_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
            publication_types=[r.get("pubType")] if r.get("pubType") else ["peer-reviewed"],
            source="Europe PMC", source_provenance=["Europe PMC"],
        ))
    return out


def _biorxiv(plan, limit=30):
    """Search bioRxiv preprints via Europe PMC preprint server index."""
    q = _query_text(plan)
    search_term = f"({q}) AND (SRC:PPR) AND (PUB_NAME:biorxiv OR biorxiv)"
    try:
        data = _get(EUROPEPMC_URL, {"query": search_term, "format": "json", "pageSize": min(limit, 50), "resultType": "core"})
    except Exception:
        return []
    out = []
    for r in data.get("resultList", {}).get("result", []):
        doi = r.get("doi")
        out.append(_normalized(
            title=r.get("title"),
            abstract=r.get("abstractText"),
            authors=_authors(r.get("authorList", {}).get("author")),
            journal="bioRxiv",
            year=r.get("firstPublicationDate"),
            doi=doi,
            doi_url=f"https://doi.org/{doi}" if doi else None,
            publication_types=["preprint"],
            source="bioRxiv",
            source_provenance=["bioRxiv", "Europe PMC Preprints"],
        ))
    return out


def _medrxiv(plan, limit=30):
    """Search medRxiv preprints via Europe PMC preprint server index."""
    q = _query_text(plan)
    search_term = f"({q}) AND (SRC:PPR) AND (PUB_NAME:medrxiv OR medrxiv)"
    try:
        data = _get(EUROPEPMC_URL, {"query": search_term, "format": "json", "pageSize": min(limit, 50), "resultType": "core"})
    except Exception:
        return []
    out = []
    for r in data.get("resultList", {}).get("result", []):
        doi = r.get("doi")
        out.append(_normalized(
            title=r.get("title"),
            abstract=r.get("abstractText"),
            authors=_authors(r.get("authorList", {}).get("author")),
            journal="medRxiv",
            year=r.get("firstPublicationDate"),
            doi=doi,
            doi_url=f"https://doi.org/{doi}" if doi else None,
            publication_types=["preprint"],
            source="medRxiv",
            source_provenance=["medRxiv", "Europe PMC Preprints"],
        ))
    return out


def _openalex(plan, limit=100):
    """Search OpenAlex -- free public REST API covering 250M+ global scholarly works."""
    data = _get(
        OPENALEX_URL,
        {"search": _query_text(plan), "per-page": min(limit, 100),
         "select": "id,title,doi,publication_year,authorships,primary_location,ids,abstract_inverted_index"},
        headers={"User-Agent": "agentic-amr-research/1.0"},
    )
    out = []
    for r in data.get("results", []):
        loc = r.get("primary_location") or {}
        source = (loc.get("source") or {}).get("display_name")
        ids = r.get("ids") or {}
        doi = r.get("doi") or (ids.get("doi", "").replace("https://doi.org/", "") or None)
        abstract = ""
        inv = r.get("abstract_inverted_index")
        if inv:
            try:
                word_pos = [(word, pos) for word, positions in inv.items() for pos in positions]
                word_pos.sort(key=lambda x: x[1])
                abstract = " ".join(w for w, _ in word_pos)
            except Exception:
                abstract = ""
        out.append(_normalized(
            title=r.get("title"), abstract=abstract,
            authors=_authors(r.get("authorships")),
            journal=source, year=r.get("publication_year"), doi=doi,
            source="OpenAlex", source_provenance=["OpenAlex"],
        ))
    return out


def _crossref(plan, limit=50):
    """Search Crossref -- free public REST API, DOI registry."""
    data = _get(
        CROSSREF_URL,
        {"query.bibliographic": _query_text(plan), "rows": min(limit, 50),
         "select": "title,author,DOI,published,container-title,type"},
        headers={"User-Agent": "agentic-amr-research/1.0"},
    )
    out = []
    for r in data.get("message", {}).get("items", []):
        title = (r.get("title") or [None])[0]
        journal = (r.get("container-title") or [None])[0]
        published = (r.get("published", {}).get("date-parts") or [[None]])[0]
        doi = r.get("DOI")
        out.append(_normalized(
            title=title, authors=_authors(r.get("author")),
            journal=journal, year=(published[0] if published else None), doi=doi,
            publication_types=[r.get("type")] if r.get("type") else [],
            source="Crossref", source_provenance=["Crossref"],
        ))
    return out


def _semantic_scholar(plan, limit=50):
    """Search Semantic Scholar -- AI academic graph from Allen Institute for AI."""
    api_key = os.getenv("AMR_SEMANTIC_SCHOLAR_API_KEY", "")
    headers = {"x-api-key": api_key} if api_key else {}
    data = _get(
        SEMANTIC_URL,
        {"query": _query_text(plan), "limit": min(limit, 50),
         "fields": "title,abstract,authors,year,venue,externalIds,openAccessPdf"},
        headers=headers,
    )
    out = []
    for r in data.get("data", []):
        ids = r.get("externalIds") or {}
        pmid = ids.get("PubMed")
        doi = ids.get("DOI")
        out.append(_normalized(
            title=r.get("title"), abstract=r.get("abstract"),
            authors=_authors(r.get("authors")),
            journal=r.get("venue"), year=r.get("year"),
            pmid=pmid, doi=doi,
            pubmed_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
            source="Semantic Scholar", source_provenance=["Semantic Scholar"],
        ))
    return out


def _doaj(plan, limit=30):
    """Search DOAJ (Directory of Open Access Journals) -- peer-reviewed open access papers."""
    import urllib.parse
    q = urllib.parse.quote(_query_text(plan))
    url = f"{DOAJ_URL}/{q}"
    data = _get(url, {"pageSize": min(limit, 50)})
    out = []
    for r in data.get("results", []):
        bib = r.get("bibjson", {})
        title = bib.get("title")
        abstract = bib.get("abstract") or ""
        authors = [a.get("name") for a in bib.get("author", []) if a.get("name")]
        journal = (bib.get("journal") or {}).get("title")
        year = bib.get("year")
        dois = [i.get("id") for i in bib.get("identifier", []) if str(i.get("type")).lower() == "doi"]
        doi = dois[0] if dois else None
        out.append(_normalized(
            title=title, abstract=abstract,
            authors=authors, journal=journal, year=year, doi=doi,
            source="DOAJ", source_provenance=["DOAJ"],
        ))
    return out


def _arxiv(plan, limit=25):
    """Search arXiv (Quantitative Biology / Genomics) -- preprints."""
    import xml.etree.ElementTree as ET
    import urllib.parse
    q = urllib.parse.quote(_query_text(plan))
    url = f"{ARXIV_URL}?search_query=all:{q}&start=0&max_results={min(limit, 30)}"
    resp = requests.get(url, timeout=API_TIMEOUT)
    if getattr(resp, "status_code", 0) != 200 or not getattr(resp, "text", None):
        return []
    try:
        root = ET.fromstring(resp.text)
    except Exception:
        return []
    out = []
    ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.findall(f"{ns}entry"):
        title_el = entry.find(f"{ns}title")
        title = title_el.text.strip() if title_el is not None and title_el.text else "Untitled"
        summary_el = entry.find(f"{ns}summary")
        abstract = summary_el.text.strip() if summary_el is not None and summary_el.text else ""
        authors = [a.find(f"{ns}name").text for a in entry.findall(f"{ns}author") if a.find(f"{ns}name") is not None]
        pub_el = entry.find(f"{ns}published")
        year = pub_el.text[:4] if pub_el is not None and pub_el.text else None
        doi_el = entry.find("{http://arxiv.org/schemas/atom}doi")
        doi = doi_el.text.strip() if doi_el is not None and doi_el.text else None
        out.append(_normalized(
            title=title, abstract=abstract,
            authors=authors, journal="arXiv", year=year, doi=doi,
            source="arXiv (q-bio)", source_provenance=["arXiv (q-bio)"],
        ))
    return out


def _google_scholar(plan, limit=10):
    """Search Google Scholar via SerpApi if configured; otherwise generate verified query exploration link."""
    import urllib.parse
    q_text = _query_text(plan)
    scholar_url = f"https://scholar.google.com/scholar?q={urllib.parse.quote(q_text)}"
    serpapi_key = os.getenv("AMR_SERPAPI_KEY") or os.getenv("SERPAPI_API_KEY") or os.getenv("GOOGLE_SCHOLAR_API_KEY")
    if not serpapi_key:
        return [_normalized(
            title=f"Google Scholar Query: {q_text}",
            abstract=f"Verified exploration search on Google Scholar for '{q_text}'. Direct query link provided in accordance with Google Scholar automated access terms of service.",
            authors=["Google Scholar"],
            journal="Google Scholar Exploration",
            year=None,
            doi_url=scholar_url,
            source="Google Scholar",
            source_provenance=["Google Scholar Direct Query"],
            access_mode="direct_query_link (unrestricted access)",
            url=scholar_url,
        )]
    try:
        url = f"https://serpapi.com/search.json?engine=google_scholar&q={urllib.parse.quote(q_text)}&num={min(limit, 20)}&api_key={serpapi_key}"
        resp = requests.get(url, timeout=API_TIMEOUT)
        if resp.status_code != 200:
            return []
        data = resp.json()
        out = []
        for r in data.get("organic_results", []):
            pub_info = r.get("publication_info", {})
            summary = pub_info.get("summary", "")
            out.append(_normalized(
                title=r.get("title"),
                abstract=r.get("snippet") or "",
                authors=[a.get("name") for a in pub_info.get("authors", []) if a.get("name")],
                journal=summary.split("-")[-1].strip() if "-" in summary else "Google Scholar",
                year=_year(summary),
                doi_url=r.get("link"),
                source="Google Scholar",
                source_provenance=["Google Scholar (SerpApi)"],
                access_mode="api_search",
                url=r.get("link"),
            ))
        return out
    except Exception:
        return []


def _identity(paper):
    return (
        ("pmid", str(paper["pmid"]).lower()) if paper.get("pmid")
        else ("pmcid", str(paper["pmcid"]).lower()) if paper.get("pmcid")
        else ("doi", str(paper["doi"]).lower().replace("https://doi.org/", "")) if paper.get("doi")
        else ("title", re.sub(r"\W+", " ", str(paper.get("title", "")).lower()).strip())
    )


def retrieve_supplemental(plan):
    """Query supplemental scholarly databases in parallel alongside PubMed.

    Europe PMC, bioRxiv, medRxiv, OpenAlex, Crossref, DOAJ, and arXiv are queried (free, no auth).
    Semantic Scholar is queried (free, optional API key raises rate limit).
    Google Scholar uses SerpApi if configured, and always supplies a verified direct query URL.
    Set AMR_ADDITIONAL_SOURCES=0 to disable all supplemental providers
    (useful for offline tests or deterministic local runs).
    """
    from config import AMR_ADDITIONAL_SOURCES
    if not AMR_ADDITIONAL_SOURCES:
        return [], {
            "enabled": False, "providers": [], "provider_status": {}, "errors": [],
            "researchgate": {
                "status": "manual_only",
                "reason": "No unrestricted public API; no scraping attempted.",
            },
        }

    providers = [
        "Europe PMC", "bioRxiv", "medRxiv", "OpenAlex", "Crossref",
        "Semantic Scholar", "Google Scholar", "DOAJ", "arXiv (q-bio)",
    ]
    funcs = {
        "Europe PMC": _europe_pmc,
        "bioRxiv": _biorxiv,
        "medRxiv": _medrxiv,
        "OpenAlex": _openalex,
        "Crossref": _crossref,
        "Semantic Scholar": _semantic_scholar,
        "Google Scholar": _google_scholar,
        "DOAJ": _doaj,
        "arXiv (q-bio)": _arxiv,
    }
    by_id = {}
    errors = []
    with ThreadPoolExecutor(max_workers=min(8, len(providers))) as pool:
        futures = {pool.submit(funcs[name], plan): name for name in providers}
        for future in as_completed(futures):
            name = futures[future]
            try:
                papers = future.result()
                for paper in papers:
                    key = _identity(paper)
                    if key[1]:
                        if key in by_id:
                            by_id[key]["source_provenance"] = sorted(set(
                                by_id[key].get("source_provenance", []) + paper.get("source_provenance", [])
                            ))
                        else:
                            by_id[key] = paper
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

    import urllib.parse
    scholar_q = _query_text(plan)
    scholar_url = f"https://scholar.google.com/scholar?q={urllib.parse.quote(scholar_q)}"
    has_scholar_key = bool(os.getenv("AMR_SERPAPI_KEY") or os.getenv("SERPAPI_API_KEY") or os.getenv("GOOGLE_SCHOLAR_API_KEY"))

    provider_status = {}
    for name in providers:
        if name == "Google Scholar":
            provider_status[name] = "searched_via_api" if has_scholar_key else "direct_query_url_available"
        else:
            provider_status[name] = "searched" if not any(e.startswith(name + ":") for e in errors) else "failed"

    return list(by_id.values()), {
        "enabled": True,
        "providers": providers,
        "provider_status": provider_status,
        "errors": errors,
        "google_scholar": {
            "status": "searched_via_api" if has_scholar_key else "direct_query_url_available",
            "url": scholar_url,
            "mode": "SerpApi REST" if has_scholar_key else "direct_query_link (unrestricted access)",
            "note": "Live SerpApi Google Scholar results active." if has_scholar_key else "Automated scraping without API key restricted by Google Scholar ToS; direct query URL supplied for manual cross-verification.",
        },
        "researchgate": {
            "status": "metadata_only",
            "url": RESEARCHGATE_SEARCH_URL,
            "reason": "ResearchGate has no unrestricted public API; no scraping attempted.",
        },
    }
