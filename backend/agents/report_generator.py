"""Final report assembly and citation reconciliation for the LangGraph workflow."""
import html
import re


def _clean_html_text(text):
    if not text:
        return ""
    t = html.unescape(str(text))
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return " ".join(t.split())


def _clean_ref_title_key(title):
    t = _clean_html_text(title)
    return re.sub(r"[\W_]+", "", t).lower()


def _clean_doi(doi):
    if not doi:
        return None
    d = str(doi).strip()
    d = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", d, flags=re.I)
    return d.strip() or None


def _doi_url(doi):
    clean = _clean_doi(doi)
    return f"https://doi.org/{clean}" if clean else None


def _all_references(literature_results):
    references = []
    seen_pmids = set()
    seen_dois = set()
    seen_titles = {}

    for item in literature_results or []:
        for ref in item.get("literature", {}).get("references", []):
            if not isinstance(ref, dict):
                continue
            pmid = str(ref.get("pmid") or "").strip()
            doi = str(ref.get("doi") or "").strip().lower()
            title_key = _clean_ref_title_key(ref.get("title"))

            existing = None
            if pmid and pmid in seen_pmids:
                existing = next((r for r in references if str(r.get("pmid") or "").strip() == pmid), None)
            elif doi and doi in seen_dois:
                existing = next((r for r in references if str(r.get("doi") or "").strip().lower() == doi), None)
            elif title_key and len(title_key) >= 20 and title_key in seen_titles:
                existing = seen_titles[title_key]

            if existing:
                if not existing.get("pmid") and pmid:
                    existing["pmid"] = pmid
                    seen_pmids.add(pmid)
                if not existing.get("doi") and doi:
                    existing["doi"] = doi
                    seen_dois.add(doi)
                if not existing.get("pubmed_url") and ref.get("pubmed_url"):
                    existing["pubmed_url"] = ref.get("pubmed_url")
                if not existing.get("doi_url") and ref.get("doi_url"):
                    existing["doi_url"] = ref.get("doi_url")
                if not existing.get("source_url") and ref.get("source_url"):
                    existing["source_url"] = ref.get("source_url")
                continue

            ref_copy = dict(ref)
            if pmid:
                seen_pmids.add(pmid)
            if doi:
                seen_dois.add(doi)
            if title_key and len(title_key) >= 20:
                seen_titles[title_key] = ref_copy
            references.append(ref_copy)
    return references


def reconcile_citations(answer, references):
    """Keep only cited references and renumber them in first-citation order."""
    if not answer or not references:
        return answer, []

    pattern = re.compile(r"\[(\d+(?:\s*[-,]\s*\d+)*)\](?:\(([^)]+)\))?")
    cited_old = []

    for match in pattern.finditer(answer):
        raw = match.group(1).replace(" ", "")
        numbers = []
        for part in raw.split(","):
            if "-" in part:
                a, b = part.split("-", 1)
                try:
                    numbers.extend(range(int(a), int(b) + 1))
                except ValueError:
                    continue
            else:
                try:
                    numbers.append(int(part))
                except ValueError:
                    continue
        for n in numbers:
            if 1 <= n <= len(references) and n not in cited_old:
                cited_old.append(n)

    if not cited_old:
        # If no citations were present in the text, do not invent or inject phantom references
        return answer, []

    mapping = {old: new for new, old in enumerate(cited_old, start=1)}

    def replace(match):
        raw = match.group(1).replace(" ", "")
        url = match.group(2)
        parts = []
        for part in raw.split(","):
            if "-" in part:
                a, b = part.split("-", 1)
                try:
                    nums = list(range(int(a), int(b) + 1))
                except ValueError:
                    nums = []
            else:
                try:
                    nums = [int(part)]
                except ValueError:
                    nums = []
            for n in nums:
                if n in mapping:
                    parts.append(str(mapping[n]))
        if not parts:
            return match.group(0)
        # Preserve a supplied URL for single citations. For grouped citations,
        # leave them as plain [1, 2] because each paper has its own URL.
        if len(parts) == 1 and url:
            return f"[{parts[0]}]({url})"
        return "[" + ", ".join(parts) + "]"

    reconciled = pattern.sub(replace, answer)
    selected = []
    for new, old in enumerate(cited_old, start=1):
        item = dict(references[old - 1])
        item["reference_number"] = new
        pmid = item.get("pmid")
        doi = _clean_doi(item.get("doi"))
        item["doi"] = doi
        if pmid and not item.get("pubmed_url"):
            item["pubmed_url"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        if doi:
            item["doi_url"] = f"https://doi.org/{doi}"
        elif item.get("doi_url"):
            item["doi_url"] = _doi_url(item.get("doi_url"))
        if not item.get("source_url"):
            item["source_url"] = item.get("pubmed_url") or item.get("doi_url")

        pub_types = [str(x).lower() for x in item.get("publication_types", [])]
        source_name = item.get("source", "")
        journal_lower = str(item.get("journal", "")).lower()
        if (
            "preprint" in pub_types
            or source_name.lower() in ("biorxiv", "medrxiv", "arxiv")
            or any(p in journal_lower for p in ("biorxiv", "medrxiv", "arxiv"))
        ):
            server = "bioRxiv" if "biorxiv" in (source_name.lower() + journal_lower) else ("medRxiv" if "medrxiv" in (source_name.lower() + journal_lower) else ("arXiv" if "arxiv" in (source_name.lower() + journal_lower) else "Preprint"))
            item["publication_status"] = "Preprint"
            item["preprint_server"] = server
        else:
            item["publication_status"] = "Peer-reviewed"
            item["preprint_server"] = None

        selected.append(item)
    return reconciled, selected


def _ensure_references_section(answer, cited_refs):
    if not cited_refs:
        return answer
    # Cleanly remove any existing preliminary references section to avoid duplicates
    clean_answer = re.sub(
        r"(?im)\n{1,3}#{0,3}\s*(?:references(?:\s+found)?|published\s+articles|supporting\s+publications)\b[\s\S]*$",
        "",
        answer or "",
    ).rstrip()

    lines = ["\n\n### References"]
    for ref in cited_refs:
        n = ref["reference_number"]
        raw_authors = ref.get("authors", [])
        cleaned_authors = [_clean_html_text(a) for a in raw_authors]
        authors = ", ".join(cleaned_authors[:6])
        raw_title = ref.get("title") or "Untitled publication"
        title = _clean_html_text(raw_title)
        title = re.sub(r"\.+$", "", title).strip()
        raw_journal = ref.get("journal") or ""
        journal = _clean_html_text(raw_journal)
        year = ref.get("year") or ""
        pmid = ref.get("pmid")
        doi = _clean_doi(ref.get("doi"))
        url = ref.get("pubmed_url") or (f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None) or _doi_url(doi or ref.get("doi_url")) or ref.get("source_url")
        author_text = f"{authors}. " if authors else ""
        meta_parts = []
        if journal:
            meta_parts.append(journal)
        if year:
            meta_parts.append(str(year))
        meta_str = f" *{', '.join(meta_parts)}*" if meta_parts else ""
        ids = []
        if pmid:
            ids.append(f"PMID: {pmid}")
        if doi:
            ids.append(f"DOI: {doi}")
        id_str = f" | {' · '.join(ids)}" if ids else ""

        # Publication status badge
        pub_status = ref.get("publication_status", "Peer-reviewed")
        server = ref.get("preprint_server")
        status_tag = f" [{pub_status}: {server}]" if (pub_status == "Preprint" and server) else f" [{pub_status}]"

        citation = f"[{n}] {author_text}**{title}**.{status_tag}{meta_str}{id_str}"
        if url:
            citation += f"\n    Direct link: {url}"
        lines.append(citation)
    return clean_answer + "\n" + "\n".join(lines)


def generate_report(query, understanding, plan, literature_results=None, evidence=None, trace=None, answer=""):
    literature_results = literature_results or []
    evidence = evidence or {"literature_evidence": [], "reference_evidence": []}
    trace = trace or []

    all_refs = _all_references(literature_results)
    reconciled_answer, cited_refs = reconcile_citations(answer, all_refs)
    if cited_refs:
        reconciled_answer = _ensure_references_section(reconciled_answer, cited_refs)
    else:
        # If the model omitted citations, keep the answer but do not pretend that
        # uncited papers were used as references.
        cited_refs = []

    return {
        "status": (
            "pending_selection"
            if understanding.get("selection_required") and not literature_results
            else "success"
        ),
        "query": query,
        "title": "AMR Nexus",
        "answer": reconciled_answer,
        "understanding": understanding,
        "plan": plan,
        "gene_candidates": understanding.get("all_genes", []),
        "candidate_groups": understanding.get("gene_candidate_groups", []),
        "resolutions": understanding.get("resolutions", []),
        "literature_results": literature_results,
        "literature_evidence": evidence.get("literature_evidence", []),
        "reference_evidence": evidence.get("reference_evidence", []),
        "evidence_policy": evidence.get("evidence_policy", {}),
        "mutation_reference": evidence.get("mutation_reference", {}),
        "entity_normalization": evidence.get("entity_normalization", {}),
        "references": cited_refs,
        "all_retrieved_references": all_refs,
        "execution_trace": trace,
    }


def public_view(report):
    """Frontend/API-facing view: only the scientific answer and its cited
    references (or, when a gene mention is ambiguous, the candidate list
    needed to disambiguate -- that is user-facing data, not debugging
    output). Internal LangGraph/agent details (execution_trace, raw
    understanding/plan, literature_results, all_retrieved_references, etc.)
    are left out here and are only returned when debug mode is requested
    (config.AMR_DEBUG or a per-request override) -- see routes/query.py.
    """
    view = {
        "status": report.get("status"),
        "query": report.get("query"),
        "title": report.get("title"),
    }
    if report.get("status") == "pending_selection":
        view["message"] = report.get("answer")
        view["candidate_groups"] = report.get("candidate_groups", [])
        return view

    view["answer"] = report.get("answer", "")
    view["references"] = report.get("references", [])
    if report.get("latency"):
        view["latency"] = report["latency"]
    literature_items = report.get("literature_results", []) or []
    if literature_items:
        literature = literature_items[0].get("literature", {}) or {}
        view["retrieval"] = literature.get("retrieval", {})
        view["source_summary"] = literature.get("source_summary", {})
    return view
