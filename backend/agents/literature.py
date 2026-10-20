"""Query-adaptive PubMed retrieval for the AMR research assistant.

Retrieval is driven by the user's *information need*, not by a gene lookup
alone. CARD/NCBI are used for identity grounding; PubMed supplies the
published literature evidence.

The retriever deliberately separates two ideas:
  1. SEARCH COVERAGE: PubMed can return the complete matching result set.
  2. ANSWER CONTEXT: only the most relevant evidence records are sent to the
     LLM, because asking an LLM to read thousands of abstracts is neither
     efficient nor necessary.
"""
import html
import re
import time
import requests
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from agents.source_adapters import retrieve_supplemental

from agents.entity_aliases import (
    ORGANISM_OK_STATUSES,
    canonical_drug,
    canonical_organism,
    dedupe_by_key,
    drug_hits,
    drug_terms,
    gene_hit,
    organism_assessment,
    organism_terms,
)
from agents.mutation_matching import alternative_notations, assess_paper
from config import (
    NCBI_API_KEY,
    NCBI_EMAIL,
    NCBI_TOOL,
    PUBMED_EUTILS_URL,
    PUBMED_CANDIDATES_PER_QUERY,
    PUBMED_OUTPUT_REFERENCES,
    PUBMED_TIMEOUT,
    PUBMED_RETRIES,
    PUBMED_FETCH_COUNTS,
    PUBMED_QUERY_VARIANTS,
    PUBMED_BATCH_SIZE,
    PUBMED_MAX_RECORDS,
)

TOP_N = PUBMED_OUTPUT_REFERENCES
CANDIDATE_N = PUBMED_CANDIDATES_PER_QUERY


def _ncbi_params(params):
    out = dict(params)
    if NCBI_TOOL:
        out["tool"] = NCBI_TOOL
    if NCBI_EMAIL:
        out["email"] = NCBI_EMAIL
    if NCBI_API_KEY:
        out["api_key"] = NCBI_API_KEY
    return out


def _get(url, params, attempts=PUBMED_RETRIES):
    """GET with a small retry for transient NCBI failures (429 / 5xx / timeouts)."""
    last = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, params=_ncbi_params(params), timeout=PUBMED_TIMEOUT)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise last  # pragma: no cover


def _esearch_result(response):
    """Return esearchresult, raising on NCBI-reported errors.

    NCBI can answer HTTP 200 with an error body (e.g. rate limit). Treating
    that as "0 records" would wrongly look like "no matching evidence".
    """
    payload = response.json()
    if payload.get("error"):
        raise ValueError(f"NCBI error: {payload['error']}")
    result = payload.get("esearchresult")
    if not isinstance(result, dict):
        raise ValueError("NCBI ESearch returned no esearchresult")
    if result.get("ERROR"):
        raise ValueError(f"NCBI ESearch error: {result['ERROR']}")
    return result


def fetch_pubmed_count(search_query):
    response = _get(
        f"{PUBMED_EUTILS_URL}/esearch.fcgi",
        {"db": "pubmed", "term": search_query, "retmax": 0, "retmode": "json"},
    )
    return int(_esearch_result(response).get("count", 0))


def _parse_pubmed_xml(xml_text):
    root = ET.fromstring(xml_text)
    papers = []

    for article in root.findall(".//PubmedArticle"):
        title_element = article.find(".//ArticleTitle")
        title = "".join(title_element.itertext()) if title_element is not None else ""
        abstract = " ".join(
            "".join(e.itertext()) for e in article.findall(".//Abstract/AbstractText")
        )
        authors = []
        for author in article.findall(".//Author"):
            last = author.findtext("LastName")
            initials = author.findtext("Initials")
            if last:
                authors.append(f"{last} {initials}" if initials else last)

        journal = article.findtext(".//Journal/Title")
        year = article.findtext(".//PubDate/Year") or article.findtext(".//PubDate/MedlineDate")
        pmid = article.findtext(".//PMID")
        doi = None
        pmcid = None
        for aid in article.findall(".//ArticleId"):
            typ = aid.attrib.get("IdType")
            if typ == "doi":
                doi = aid.text
            elif typ == "pmc":
                pmcid = aid.text
        if not doi:
            for element in article.findall(".//ELocationID"):
                if element.attrib.get("EIdType") == "doi":
                    doi = element.text

        mesh_terms = [
            (e.text or "").strip()
            for e in article.findall(".//MeshHeadingList/MeshHeading/DescriptorName")
            if (e.text or "").strip()
        ]

        publication_types = [
            (e.text or "").strip()
            for e in article.findall(".//PublicationType")
            if (e.text or "").strip()
        ]

        papers.append({
            "title": title,
            "authors": authors,
            "journal": journal,
            "year": year,
            "pmid": pmid,
            "pmcid": pmcid,
            "doi": doi,
            "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
            "doi_url": f"https://doi.org/{doi}" if doi else None,
            "abstract": abstract,
            "publication_types": publication_types,
            "mesh_terms": mesh_terms,
            "source": "PubMed",
        })

    return papers


def fetch_papers(search_query, limit=None, sort="relevance"):
    """Retrieve PubMed papers in batches.

    When ``limit`` is None, the complete PubMed result set is traversed using
    ESearch history + paginated EFetch.  ``PUBMED_MAX_RECORDS=0`` means no local
    cap. A positive cap is available as an operational safeguard for very large
    searches.
    """
    batch_size = max(1, int(PUBMED_BATCH_SIZE))
    max_records = int(PUBMED_MAX_RECORDS)
    requested_limit = None if limit is None else max(0, int(limit))

    response = _get(
        f"{PUBMED_EUTILS_URL}/esearch.fcgi",
        {
            "db": "pubmed",
            "term": search_query,
            "retmax": 0,
            "sort": sort,
            "usehistory": "y",
            "retmode": "json",
        },
    )
    result = _esearch_result(response)
    count = int(result.get("count", 0))
    webenv = result.get("webenv")
    query_key = result.get("querykey")

    if not count:
        return []
    if not webenv or not query_key:
        raise ValueError("PubMed reported matches but returned no history key")

    target = count
    if requested_limit is not None:
        target = min(target, requested_limit)
    if max_records > 0:
        target = min(target, max_records)

    papers = []
    for start in range(0, target, batch_size):
        size = min(batch_size, target - start)
        fetch_response = _get(
            f"{PUBMED_EUTILS_URL}/efetch.fcgi",
            {
                "db": "pubmed",
                "query_key": query_key,
                "WebEnv": webenv,
                "retstart": start,
                "retmax": size,
                "retmode": "xml",
            },
        )
        papers.extend(_parse_pubmed_xml(fetch_response.text))

    return papers


def _quote(value):
    value = str(value).strip()
    if not value:
        return ""
    return '"' + value.replace('"', "") + '"'


def _pubmed_entity_term(value):
    """Build a PubMed-safe term for biological entity names.

    PubMed treats hyphenated terms specially: a quoted hyphenated phrase can
    fail when it is not present in PubMed's phrase index. AMR allele names such
    as MCR-1.1 are especially vulnerable to this. Field-qualified text-word
    searches are more robust for genes/alleles and still work when punctuation
    is normalized by PubMed.
    """
    value = str(value).strip()
    if not value:
        return ""
    if re.search(r"[-_.]", value):
        return f"{value}[Title/Abstract]"
    return _quote(value)


def _unique(values):
    result = []
    seen = set()
    for value in values or []:
        value = str(value).strip()
        key = value.lower()
        if value and key not in seen:
            result.append(value)
            seen.add(key)
    return result


def _entity_terms(plan):
    entities = plan.get("entities", {}) or {}
    genes = list(entities.get("genes") or [])
    candidate_genes = list(entities.get("candidate_genes") or [])
    search_genes = _unique(genes + candidate_genes) if candidate_genes else _unique(genes)
    return {
        "genes": search_genes,
        "antibiotics": _unique(entities.get("antibiotics")),
        "mutations": _unique([
            *(entities.get("mutations") or []),
            *(entities.get("variants") or []),
        ]),
        "organisms": _unique(entities.get("organisms")),
        "mechanisms": _unique(entities.get("mechanisms")),
        "scientific_concepts": _unique(entities.get("scientific_concepts")),
        "query_keywords": _unique(entities.get("query_keywords")),
    }


def _concepts_for_intent(plan):
    intents = set(plan.get("intent") or [])
    concepts = []
    if "resistance_mechanism" in intents:
        concepts += [
            "resistance", "mechanism", "susceptibility", "lipid A",
            "phosphoethanolamine", "phosphoethanolamine transferase",
            "enzyme", "membrane", "polymyxin",
            "efflux", "porin", "porins", "outer membrane",
            "promoter", "permeability", "target modification",
            "methyltransferase",
        ]
    if "mutation_variant_information" in intents:
        concepts += [
            "mutation", "mutations", "variant", "variants", "substitution", "substitutions",
            "allele", "codon", "polymorphism", "phenotype",
        ]
    if "comparison" in intents:
        concepts += ["resistance", "susceptibility", "comparison"]
    if "amr_information_retrieval" in intents or not concepts:
        concepts += ["antimicrobial resistance", "antibiotic resistance", "resistance"]
    return _unique(concepts)



def _tiab_term(term):
    """Field-qualified Title/Abstract term; periods dropped (PubMed ignores them)."""
    term = re.sub(r"\s+", " ", str(term).replace(".", " ")).strip().replace('"', "")
    if not term:
        return ""
    return f'"{term}"[Title/Abstract]' if " " in term else f"{term}[Title/Abstract]"


def _organism_clause(organisms):
    """Canonical name + every accepted abbreviation, plus the curated MeSH heading."""
    parts, seen = [], set()
    for organism in organisms or []:
        canon = canonical_organism(organism)
        for form in organism_terms(canon):
            term = _tiab_term(form)
            if term and term.lower() not in seen:
                seen.add(term.lower())
                parts.append(term)
        mesh = f'"{canon}"[MeSH Terms]'
        if mesh.lower() not in seen:
            seen.add(mesh.lower())
            parts.append(mesh)
    return " OR ".join(parts)


def _drug_clause(antibiotics):
    """Canonical drug + synonyms (rifampin/rifampicin) + class members."""
    parts, seen = [], set()
    for drug in dedupe_by_key(antibiotics, canonical_drug):
        for term in drug_terms(drug):
            built = _pubmed_entity_term(term)
            if built and built.lower() not in seen:
                seen.add(built.lower())
                parts.append(built)
    return " OR ".join(parts)


def _mutation_clause(mutations, genes, organisms):
    """Each requested mutation in every accepted notation (S450L, Ser450Leu, other numbering)."""
    parts, seen = [], set()
    for mutation in mutations or []:
        for form in alternative_notations(mutation, genes, organisms):
            term = _tiab_term(form)
            if term and term.lower() not in seen:
                seen.add(term.lower())
                parts.append(term)
    return " OR ".join(parts)


def _or_clause(values, entity=False):
    builder = _pubmed_entity_term if entity else _quote
    return " OR ".join(builder(v) for v in _unique(values) if str(v).strip())


def _parent_gene_terms(genes):
    """Generate conservative family-level search terms from versioned alleles.

    Examples: MCR-1.1 -> MCR-1; KPC-2 -> KPC. These are retrieval expansions,
    not assertions that every family-level paper experimentally studied the
    exact allele.
    """
    parents = []
    for gene in genes or []:
        value = str(gene).strip()
        if not value:
            continue
        parent = re.sub(r"\.\d+$", "", value)
        if parent == value:
            parent = re.sub(r"[-_]\d+$", "", value)
        if parent != value and len(parent) >= 3:
            parents.append(parent)
    return _unique(parents)


def _mechanism_pubmed_clause(mechanisms):
    """Build a PubMed clause for mechanism terms with common synonyms.

    Mechanism terms need broader phrase matching since they may appear with
    varied phrasing in abstracts (e.g. "efflux" vs "efflux pump" vs "active efflux").
    """
    if not mechanisms:
        return ""
    parts = []
    seen = set()
    # Expand known mechanism terms to their PubMed-indexed variants
    _mech_expansions = {
        "efflux pump": ['"efflux pump"[Title/Abstract]', '"efflux pumps"[Title/Abstract]',
                        '"efflux"[Title/Abstract]', '"RND efflux"[Title/Abstract]',
                        '"active efflux"[Title/Abstract]'],
        "porin": ['"porin"[Title/Abstract]', '"porins"[Title/Abstract]',
                  '"outer membrane protein"[Title/Abstract]', '"OmpC"[Title/Abstract]',
                  '"OmpF"[Title/Abstract]', '"OprD"[Title/Abstract]',
                  '"OmpK35"[Title/Abstract]', '"OmpK36"[Title/Abstract]'],
        "promoter mutation": ['"promoter"[Title/Abstract]', '"promoter region"[Title/Abstract]',
                              '"upstream region"[Title/Abstract]',
                              '"regulatory mutation"[Title/Abstract]',
                              '"promoter mutation"[Title/Abstract]'],
        "beta-lactamase": ['"beta-lactamase"[Title/Abstract]', '"betalactamase"[Title/Abstract]',
                           '"ESBL"[Title/Abstract]', '"carbapenemase"[Title/Abstract]'],
        "target modification": ['"target modification"[Title/Abstract]',
                                '"target alteration"[Title/Abstract]',
                                '"penicillin-binding protein"[Title/Abstract]',
                                '"PBP"[Title/Abstract]'],
        "methyltransferase": ['"methyltransferase"[Title/Abstract]',
                              '"rRNA methyltransferase"[Title/Abstract]'],
        "phosphoethanolamine transfer": ['"phosphoethanolamine"[Title/Abstract]',
                                         '"MCR"[Title/Abstract]'],
        "lipid A modification": ['"lipid A"[Title/Abstract]'],
    }
    for mech in mechanisms:
        key = str(mech).lower().strip()
        expansion = _mech_expansions.get(key)
        if expansion:
            for term in expansion:
                if term not in seen:
                    parts.append(term)
                    seen.add(term)
        else:
            term = _tiab_term(mech)
            if term and term not in seen:
                parts.append(term)
                seen.add(term)
    return " OR ".join(parts)


def build_pubmed_queries(plan):
    """Build multiple complementary searches from the complete question.

    Exact-variant searches are kept separate from family-level searches so the
    final evidence layer can distinguish allele-specific evidence from broader
    mechanistic evidence.

    Mechanism-only queries (efflux pump, porins, promoter region) get their own
    dedicated query path so they are never reduced to an empty or trivial search.
    """
    terms = _entity_terms(plan)
    concepts = _concepts_for_intent(plan)
    genes = terms["genes"]
    antibiotics = terms["antibiotics"]
    mutations = terms["mutations"]
    organisms = terms["organisms"]
    mechanisms = terms["mechanisms"]
    original = str(plan.get("query", "")).strip()
    queries = []

    gene_clause = _or_clause(genes, entity=True)
    antibiotic_clause = _drug_clause(antibiotics)
    mutation_clause = _mutation_clause(mutations, genes, organisms)
    organism_clause = _organism_clause(organisms)
    mechanism_clause = _or_clause(mechanisms, entity=True)
    mechanism_pubmed_clause = _mechanism_pubmed_clause(mechanisms)
    concept_clause = _or_clause(concepts)
    scientific_concepts = terms.get("scientific_concepts", [])
    query_keywords = terms.get("query_keywords", [])

    # --- 0. Full-query semantic synthesis search ---
    # Combines all biological entities, mechanisms, and scientific concepts from the whole query
    semantic_components = []
    if gene_clause:
        semantic_components.append(f"({gene_clause})")
    if mechanism_pubmed_clause:
        semantic_components.append(f"({mechanism_pubmed_clause})")
    elif mechanism_clause:
        semantic_components.append(f"({mechanism_clause})")
    if antibiotic_clause:
        semantic_components.append(f"({antibiotic_clause})")
    if organism_clause:
        semantic_components.append(f"({organism_clause})")
    # Add scientific concepts not already represented
    for sc in scientific_concepts:
        if not any(sc.lower() in m.lower() for m in mechanisms):
            sc_term = _tiab_term(sc)
            if sc_term and f"({sc_term})" not in semantic_components:
                semantic_components.append(f"({sc_term})")

    if len(semantic_components) >= 2:
        queries.append(" AND ".join(semantic_components))
        amr_anchor = '("antimicrobial resistance"[Title/Abstract] OR "antibiotic resistance"[Title/Abstract] OR "drug resistance"[MeSH Terms])'
        queries.append(" AND ".join(semantic_components) + f" AND {amr_anchor}")

    # --- Core entity-relationship searches ---
    core_parts = [x for x in [f"({gene_clause})" if gene_clause else "",
                               f"({antibiotic_clause})" if antibiotic_clause else "",
                               f"({organism_clause})" if organism_clause else "",
                               f"({concept_clause})" if concept_clause else ""] if x]
    if gene_clause and antibiotic_clause:
        queries.append(" AND ".join(core_parts))
        queries.append(" AND ".join(x for x in core_parts if x != f"({concept_clause})"))
    elif gene_clause:
        queries.append(" AND ".join(core_parts))
        if organism_clause:
            queries.append(f"({gene_clause}) AND ({organism_clause})")
        else:
            queries.append(f"({gene_clause}) AND ({concept_clause})")
    elif antibiotic_clause:
        queries.append(" AND ".join(core_parts))

    # --- Mutation-specific search ---
    if mutation_clause:
        mutation_parts = [x for x in [
            f"({gene_clause})" if gene_clause else "",
            f"({mutation_clause})",
            f"({antibiotic_clause})" if antibiotic_clause else "",
            f"({organism_clause})" if organism_clause else "",
        ] if x]
        base = " AND ".join(mutation_parts)
        if base:
            queries.append(base + f" AND ({concept_clause})")

    # --- Mechanism-focused searches (efflux pump, porins, promoter region, etc.) ---
    # This is the critical path for mechanism-only queries that previously got no results.
    if mechanism_pubmed_clause:
        amr_context = '("antimicrobial resistance"[Title/Abstract] OR "antibiotic resistance"[Title/Abstract] OR "resistance"[MeSH Terms])'
        if gene_clause:
            queries.append(f"({gene_clause}) AND ({mechanism_pubmed_clause})")
            queries.append(f"({gene_clause}) AND ({mechanism_pubmed_clause}) AND {amr_context}")
        elif antibiotic_clause:
            queries.append(f"({mechanism_pubmed_clause}) AND ({antibiotic_clause})")
            queries.append(f"({mechanism_pubmed_clause}) AND {amr_context}")
        elif organism_clause:
            queries.append(f"({mechanism_pubmed_clause}) AND ({organism_clause}) AND {amr_context}")
            queries.append(f"({mechanism_pubmed_clause}) AND {amr_context}")
        else:
            # Pure mechanism query — this is the efflux pump / porins case
            queries.append(f"({mechanism_pubmed_clause}) AND {amr_context}")
            # Also a broader pass without the AMR context constraint to catch review articles
            queries.append(f"({mechanism_pubmed_clause}) AND (review[pt] OR review[tiab])")
    elif mechanism_clause and (gene_clause or antibiotic_clause):
        queries.append(f"({gene_clause or antibiotic_clause}) AND ({mechanism_clause})")

    # --- Family-level broadening for versioned alleles ---
    parent_clause = _or_clause(_parent_gene_terms(genes), entity=True)
    if parent_clause and antibiotic_clause:
        family_parts = [f"({parent_clause})", f"({antibiotic_clause})"]
        if organism_clause:
            family_parts.append(f"({organism_clause})")
        family_parts.append(f"({concept_clause})")
        queries.append(" AND ".join(family_parts))
        queries.append(" AND ".join(family_parts[:-1]))
    elif parent_clause:
        family_parts = [f"({parent_clause})"]
        if organism_clause:
            family_parts.append(f"({organism_clause})")
        family_parts.append(f"({concept_clause})")
        queries.append(" AND ".join(family_parts))

    if organism_clause and (gene_clause or antibiotic_clause):
        entity_clause = gene_clause or antibiotic_clause
        queries.append(f"({entity_clause}) AND ({organism_clause}) AND ({concept_clause})")

    # --- Review + primary study passes ---
    if gene_clause and antibiotic_clause:
        relation_parts = [f"({gene_clause})", f"({antibiotic_clause})"]
        if organism_clause:
            relation_parts.append(f"({organism_clause})")
        relation = " AND ".join(relation_parts)
        queries.append(relation + " AND (review[pt] OR review[tiab])")
        queries.append(relation + " AND NOT review[pt]")
    elif gene_clause:
        relation_parts = [f"({gene_clause})"]
        if organism_clause:
            relation_parts.append(f"({organism_clause})")
        relation_parts.append(f"({concept_clause})")
        relation = " AND ".join(relation_parts)
        queries.append(relation + " AND (review[pt] OR review[tiab])")
        queries.append(relation + " AND NOT review[pt]")

    # --- Semantic fallback for free-text / concept queries ---
    # When no structured entities are detected, build an OR-based query from meaningful
    # tokens so broad keyword queries ("efflux pump", "porins") still retrieve literature.
    if not queries:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", original.lower())
        stopwords = {
            "what", "which", "when", "where", "who", "whom", "does", "how",
            "have", "has", "are", "was", "were", "the", "and", "for", "with",
            "from", "into", "that", "this", "about", "cause", "causes", "give",
            "find", "tell", "show", "known", "there", "their", "they", "its",
        }
        meaningful = _unique(t for t in tokens if t not in stopwords)
        if meaningful:
            # OR the meaningful tokens (not AND) so multi-word concepts are not over-restricted;
            # anchor to AMR domain with a MeSH/concept constraint.
            term_clause = " OR ".join(f'"{t}"[Title/Abstract]' for t in meaningful[:12])
            queries.append(
                f"({term_clause}) AND "
                '("antimicrobial resistance"[Title/Abstract] OR "antibiotic resistance"[Title/Abstract] '
                'OR "drug resistance"[MeSH Terms])'
            )
            # Also a plain phrase search for the full query as written
            if len(original) >= 4:
                queries.append(f'"{original}"[Title/Abstract] AND ("resistance" OR "antimicrobial")')
        else:
            queries.append('"antimicrobial resistance" AND resistance')

    return _unique(queries)



def build_pubmed_query(plan):
    queries = build_pubmed_queries(plan)
    return queries[0] if queries else _quote(plan.get("query", ""))


def _paper_text(paper):
    return f"{paper.get('title', '')} {paper.get('abstract', '')}".lower()



def _mutation_forms(value):
    """Return compact mutation spellings likely to occur in PubMed text."""
    raw = str(value or "").strip()
    if not raw:
        return set()
    forms = {raw.lower(), raw.upper().lower()}
    m = re.fullmatch(r"([A-Za-z])([0-9]+)([A-Za-z])", raw.replace("p.", "", 1), re.I)
    if m:
        forms.add(f"{m.group(1).upper()}{m.group(2)}{m.group(3).upper()}".lower())
    return {x for x in forms if x}


def _mutation_query_strict_filter(paper, plan):
    """For mutation-focused queries, keep a paper only if its OWN title/abstract
    (plus full text, once the organism is established) supports the complete
    requested context -- see agents.mutation_matching.assess_paper:

      gene + drug (any accepted alias/abbreviation) + queried organism as the
      paper's subject (any accepted name/abbreviation) + a mutation in gene
      context, in ANY accepted notation, matched against the COMPLETE reference
      set (never only the 20 displayed rows).
    """
    intents = set(plan.get("intent") or [])
    if "mutation_variant_information" not in intents:
        return True
    return assess_paper(paper, plan)["eligible"]


def _filter_rejection_reasons(paper, plan):
    return assess_paper(paper, plan)["reasons"]


def _score_paper(paper, plan):
    """Retrieval relevance only; not a scientific quality/confidence score."""
    text = _paper_text(paper)
    terms = _entity_terms(plan)
    intents = set(plan.get("intent") or [])
    score = 0
    matched = []

    core = f"{paper.get('title', '')} {paper.get('abstract', '')}"
    for key in ("genes", "antibiotics", "mutations", "mechanisms", "scientific_concepts", "query_keywords"):
        for term in terms.get(key, []):
            normalized = str(term).lower().strip()
            if not normalized:
                continue
            if key == "genes":
                hit = gene_hit(core, term)
            elif key == "antibiotics":
                hit = drug_hits(core, term)
            elif key == "mutations":
                hit = any(f.lower() in text for f in alternative_notations(term, terms.get("genes"), terms.get("organisms")))
            else:
                hit = normalized in text
            if hit:
                if key in ("genes", "antibiotics"):
                    weight = 12
                elif key in ("mechanisms", "scientific_concepts"):
                    weight = 8
                else:
                    weight = 3
                score += weight
                matched.append(term)

    if terms.get("organisms"):
        status = organism_assessment(paper, terms["organisms"])["status"]
        if status in ORGANISM_OK_STATUSES:
            score += 18
            matched.extend(terms["organisms"])
        else:
            score -= 35

    if "resistance_mechanism" in intents:
        for term in ("mechanism", "resistance", "susceptibility", "lipid", "enzyme", "pathway", "phosphoethanolamine", "membrane"):
            if term in text:
                score += 2
    if "mutation_variant_information" in intents:
        for term in ("mutation", "variant", "substitution", "amino acid", "resistance"):
            if term in text:
                score += 2

    return score, _unique(matched)


def rank_papers(papers, plan, limit=TOP_N):
    ranked = []
    for paper in papers:
        score, matched = _score_paper(paper, plan)
        item = dict(paper)
        item["query_relevance_score"] = score
        item["matched_concepts"] = matched
        text = _paper_text(item)
        genes = [str(x).lower() for x in _entity_terms(plan).get("genes", [])]
        item["exact_entity_match"] = any(g in text for g in genes) if genes else False
        ranked.append(item)

    ranked.sort(
        key=lambda p: (
            -(p.get("query_relevance_score") or 0),
            -(int(str(p.get("year", "0"))[:4]) if str(p.get("year", "0"))[:4].isdigit() else 0),
        )
    )
    return ranked[:limit] if limit else ranked


def _sentences(text):
    if not text:
        return []
    text_clean = html.unescape(re.sub(r"<[^>]+>", "", str(text)))
    text_clean = re.sub(r"\s+", " ", text_clean).strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text_clean)
    return [p.strip() for p in parts if len(p.strip()) >= 35]


def extract_evidence_snippets(paper, plan, max_snippets=4):
    text = paper.get("full_text") or paper.get("abstract") or ""
    sentences = _sentences(text)
    terms = _entity_terms(plan)
    intents = set(plan.get("intent") or [])
    weighted_terms = []
    for key in ("genes", "antibiotics", "mutations", "organisms", "mechanisms", "scientific_concepts", "query_keywords"):
        for term in terms.get(key, []):
            term = str(term).strip().lower()
            if term:
                weight = 5 if key in ("genes", "antibiotics") else (4 if key in ("mechanisms", "scientific_concepts") else 2)
                weighted_terms.append((term, weight))

    intent_terms = []
    if "resistance_mechanism" in intents:
        intent_terms += ["mechanism", "resistance", "susceptibility", "lipid", "enzyme", "pathway", "modify", "modification", "phosphoethanolamine"]
    if "mutation_variant_information" in intents:
        intent_terms += ["mutation", "variant", "substitution", "amino acid"]
    if "comparison" in intents:
        intent_terms += ["compare", "compared", "higher", "lower", "susceptibility", "resistance"]

    scored = []
    for sentence in sentences:
        low = sentence.lower()
        score = 0
        matched = []
        for term, weight in weighted_terms:
            if term in low:
                score += weight
                matched.append(term)
        for term in _unique(intent_terms):
            if term in low:
                score += 1
                matched.append(term)
        if score:
            scored.append((score, sentence, _unique(matched)))

    scored.sort(key=lambda x: (-x[0], -len(x[1])))
    return [{"text": sentence, "matched_terms": matched, "score": score} for score, sentence, matched in scored[:max_snippets]]


def _evidence_alignment(paper, plan):
    if "mutation_variant_information" in set(plan.get("intent") or []):
        return assess_paper(paper, plan)["alignment"]
    text = _paper_text(paper)
    terms = _entity_terms(plan)
    intents = set(plan.get("intent") or [])
    genes = [str(x).lower() for x in terms.get("genes", [])]
    antibiotics = [str(x).lower() for x in terms.get("antibiotics", [])]
    mutations = [str(x).lower() for x in terms.get("mutations", [])]
    organisms = [str(x).lower() for x in terms.get("organisms", [])]
    mechanisms = [str(x).lower() for x in terms.get("mechanisms", [])]

    gene_hit = any(x in text for x in genes)
    drug_hit = any(x in text for x in antibiotics)
    mutation_hit = any(x in text for x in mutations)
    organism_hit = any(x in text for x in organisms)
    mechanism_term_hit = any(m in text for m in mechanisms) if mechanisms else False
    mechanism_hit = mechanism_term_hit or any(x in text for x in (
        "mechanism", "resistance", "susceptibility", "lipid", "enzyme",
        "pathway", "modify", "modification", "phosphoethanolamine",
        "efflux", "porin", "porins", "promoter",
    ))
    mutation_text_hit = any(x in text for x in ("mutation", "variant", "substitution", "amino acid"))

    if "resistance_mechanism" in intents:
        if gene_hit and drug_hit and mechanism_hit:
            return "mechanism_aligned"
        if (gene_hit or drug_hit) and mechanism_hit:
            return "mechanism_related"
        if mechanism_term_hit:
            return "mechanism_aligned"
        if mechanism_hit:
            return "mechanism_related"
    if "mutation_variant_information" in intents and (gene_hit or mutation_hit) and mutation_text_hit:
        return "mutation_aligned"
    if "comparison" in intents and (gene_hit or drug_hit) and ("compare" in text or "susceptibility" in text):
        return "comparison_aligned"
    if gene_hit and drug_hit:
        return "entity_relationship_aligned"
    if gene_hit or drug_hit or mutation_hit or organism_hit or mechanism_term_hit:
        return "entity_aligned"
    return "related"


def classify_papers(papers, plan):
    direct, related, verify = [], [], []
    for paper in papers:
        alignment = _evidence_alignment(paper, plan)
        item = dict(paper)
        item["evidence_alignment"] = alignment
        if "mutation_variant_information" in set(plan.get("intent") or []):
            item["evidence_specificity"] = assess_paper(paper, plan)["specificity"]
        item["evidence_snippets"] = extract_evidence_snippets(paper, plan)
        if alignment in {"mechanism_aligned", "mutation_aligned", "comparison_aligned", "entity_relationship_aligned"}:
            direct.append(item)
        elif alignment in {"mechanism_related", "entity_aligned"}:
            related.append(item)
        else:
            verify.append(item)
    return {"direct_evidence": direct, "related_evidence": related, "requires_verification": verify}


def build_references(papers):
    return [{
        "title": p.get("title"),
        "authors": p.get("authors", []),
        "journal": p.get("journal"),
        "year": p.get("year"),
        "pmid": p.get("pmid"),
        "pubmed_url": p.get("pubmed_url"),
        "pmcid": p.get("pmcid"),
        "doi": p.get("doi"),
        "doi_url": p.get("doi_url"),
        "publication_types": p.get("publication_types", []),
        "source": p.get("source"),
        "source_provenance": p.get("source_provenance", []),
        "source_url": p.get("source_url") or p.get("doi_url") or p.get("pubmed_url"),
    } for p in papers]


def _retrieval_status(queries, query_counts, errors, retrieved, passing):
    """Separate 'PubMed could not be searched' from 'searched, nothing matched'.

    failed                  every query failed -> NOT evidence of absence
    partial_failure         some queries failed; results may be incomplete
    no_pubmed_records       searches succeeded and returned no records
    no_matching_evidence    records were retrieved but none met the evidence criteria
    matches_found           at least one record met the criteria
    """
    attempted = len(queries)
    failed = len(errors)
    if attempted and failed >= attempted:
        status = "failed"
    elif retrieved == 0:
        status = "partial_failure" if failed else "no_pubmed_records"
    elif passing == 0:
        status = "partial_failure" if failed else "no_matching_evidence"
    else:
        status = "partial_failure" if failed else "matches_found"
    return {
        "status": status,
        "queries_attempted": attempted,
        "queries_failed": failed,
        "records_retrieved": retrieved,
        "records_passing_filter": passing,
        "errors": list(errors),
    }


def search_literature(plan, top_n=TOP_N):
    started_at = time.perf_counter()
    # Keep the strongest core, constrained, and exact-variant formulations;
    # near-duplicate formulations add latency without improving typical evidence.
    built_queries = build_pubmed_queries(plan)
    requested_variants = [str(v) for v in (plan.get("entities", {}).get("mutations") or [])]
    if requested_variants:
        exact = [q for q in built_queries if any(f"{v}[Title/Abstract]" in q for v in requested_variants)]
        queries = (exact[:1] or built_queries[:1])
    else:
        queries = built_queries[:max(1, PUBMED_QUERY_VARIANTS)]
    recency = bool(plan.get("recency_requested"))
    sort = "pub date" if recency else "relevance"

    all_papers_list = []
    by_pmid = {}
    by_doi = {}
    by_title = {}

    def _clean_paper_title_key(title):
        t = re.sub(r"<[^>]+>", "", str(title or ""))
        t = html.unescape(t)
        return re.sub(r"[\W_]+", "", t).lower()

    def _ingest_paper(p):
        if not isinstance(p, dict):
            return
        pmid = str(p.get("pmid") or "").strip()
        doi = str(p.get("doi") or "").strip().lower()
        title_key = _clean_paper_title_key(p.get("title"))

        existing = None
        if pmid and pmid in by_pmid:
            existing = by_pmid[pmid]
        elif doi and doi in by_doi:
            existing = by_doi[doi]
        elif title_key and len(title_key) >= 20 and title_key in by_title:
            existing = by_title[title_key]

        if existing:
            existing["source_provenance"] = sorted(set(
                existing.get("source_provenance", [existing.get("source")])
                + p.get("source_provenance", [p.get("source")])
            ))
            if not existing.get("pmid") and pmid:
                existing["pmid"] = pmid
                by_pmid[pmid] = existing
            if not existing.get("doi") and doi:
                existing["doi"] = doi
                by_doi[doi] = existing
            if not existing.get("abstract") and p.get("abstract"):
                existing["abstract"] = p["abstract"]
            if not existing.get("full_text") and p.get("full_text"):
                existing["full_text"] = p["full_text"]
            if not existing.get("pubmed_url") and p.get("pubmed_url"):
                existing["pubmed_url"] = p["pubmed_url"]
            if not existing.get("doi_url") and p.get("doi_url"):
                existing["doi_url"] = p["doi_url"]
            if not existing.get("source_url") and p.get("source_url"):
                existing["source_url"] = p["source_url"]
            return

        item = dict(p)
        all_papers_list.append(item)
        if pmid:
            by_pmid[pmid] = item
        if doi:
            by_doi[doi] = item
        if title_key and len(title_key) >= 20:
            by_title[title_key] = item

    query_counts = []
    errors = []

    def retrieve_one(query):
        try:
            count = fetch_pubmed_count(query) if PUBMED_FETCH_COUNTS else None
            papers = fetch_papers(query, limit=PUBMED_MAX_RECORDS, sort=sort)
            return query, count, papers, None
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            return query, None, [], f"{type(exc).__name__}: {exc}"

    # Independent formulations share no state; parallel retrieval prevents a
    # slow query from serially consuming the entire latency budget.
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(queries)))) as pool:
        futures = [pool.submit(retrieve_one, query) for query in queries]
        for future in as_completed(futures):
            query, count, papers, failure = future.result()
            query_counts.append({"query": query, "count": count})
            if failure:
                errors.append(f"PubMed query failed: {failure}")
                continue
            for paper in papers:
                _ingest_paper(paper)
    query_counts.sort(key=lambda item: queries.index(item["query"]) if item["query"] in queries else 999)

    supplemental_papers, supplemental_meta = retrieve_supplemental(plan)
    for paper in supplemental_papers:
        _ingest_paper(paper)

    retrieved_pool = all_papers_list
    network_finished_at = time.perf_counter()
    candidate_pool = retrieved_pool
    rejection_counts = {}
    has_specific_mutation_or_gene = bool(
        plan.get("entities", {}).get("mutations")
        or plan.get("entities", {}).get("variants")
        or (plan.get("entities", {}).get("genes") and "resistance_mechanism" not in set(plan.get("intent") or []))
    )
    if "mutation_variant_information" in set(plan.get("intent") or []) and has_specific_mutation_or_gene:
        candidate_pool = []
        related_pool = []
        requested_variants = [str(v) for v in (plan.get("entities", {}).get("mutations") or [])] + [str(v) for v in (plan.get("entities", {}).get("variants") or [])]
        for paper in retrieved_pool:
            assessment = assess_paper(paper, plan)
            paper["assessment"] = assessment
            if assessment["eligible"]:
                candidate_pool.append(paper)
            else:
                for reason in assessment["reasons"]:
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                if not requested_variants and assessment["gene_ok"] and assessment["drug_ok"] and assessment["organism"]["status"] in ORGANISM_OK_STATUSES:
                    related_pool.append(paper)
        if not candidate_pool and related_pool:
            candidate_pool = related_pool
    ranked_all = rank_papers(candidate_pool, plan, limit=0)
    ranking_finished_at = time.perf_counter()
    # Only the strongest evidence candidates are sent to the LLM. This does
    # not mean PubMed search stopped at top_n: all retrieved records have been
    # searched, deduplicated and ranked first.
    answer_candidates = ranked_all[:CANDIDATE_N] if CANDIDATE_N > 0 else ranked_all
    classified = classify_papers(answer_candidates, plan)
    classified_finished_at = time.perf_counter()
    displayed = ranked_all[:top_n] if top_n else ranked_all

    retrieval = _retrieval_status(queries, query_counts, errors, len(retrieved_pool), len(candidate_pool))
    retrieval["filter_rejection_reasons"] = rejection_counts
    retrieval["supplemental_sources"] = supplemental_meta
    retrieval["timings_seconds"] = {
        "network": round(network_finished_at - started_at, 3),
        "filter_and_dedup": round(ranking_finished_at - network_finished_at, 3),
        "classification": round(classified_finished_at - ranking_finished_at, 3),
        "total": round(time.perf_counter() - started_at, 3),
    }

    total_count = (query_counts[0].get("count") or 0) if query_counts else 0
    return {
        "title": "Multi-source Literature Evidence",
        "search_query": queries[0] if queries else "",
        "search_queries": queries,
        "sort_basis": "publication date" if sort == "pub date" else "retrieval relevance, then evidence relevance, then publication year",
        "total_pubmed_records": total_count,
        "total_pubmed_records_available": bool(query_counts) and all(x.get("count") is not None for x in query_counts),
        "candidate_papers_retrieved": len(retrieved_pool),
        "papers_passing_filter": len(candidate_pool),
        "retrieval": retrieval,
        "all_retrieved_papers": ranked_all,
        "answer_candidates": answer_candidates,
        "top_papers": displayed,
        "papers": answer_candidates,
        "direct_evidence": classified["direct_evidence"],
        "related_evidence": classified["related_evidence"],
        "requires_verification": classified["requires_verification"],
        "references": build_references(answer_candidates),
        "query_counts": query_counts,
        "source_summary": {
            "primary": "PubMed / NCBI E-utilities",
            "supplemental": supplemental_meta,
            "provenance_preserved": True,
        },
        "errors": errors or None,
        "evidence_level": "literature" if answer_candidates else (
            "retrieval_failed" if retrieval["status"] == "failed" else ("count_only" if total_count else "unavailable")
        ),
        "summary": (
            f"PubMed was searched using {len(queries)} complementary query formulation(s); "
            f"supplemental providers were {'enabled' if supplemental_meta.get('enabled') else 'disabled'}. "
            f"The retriever found {len(retrieved_pool)} unique records across providers; "
            f"{len(candidate_pool)} met the evidence criteria. "
            f"The first query returned {total_count} matches; {len(answer_candidates)} highest-relevance "
            f"candidates were passed to evidence analysis and synthesis."
        ),
    }
