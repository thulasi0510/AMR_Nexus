"""General AMR query-understanding agent.


The module separates *what the user asks* from *which database is queried*.

The current release is deliberately restricted to mutation-focused AMR

questions, with resistance/susceptibility as the phenotype layer.

"""

import json
from functools import lru_cache

import re

import sys

import requests

from agents.card_reference import lookup_card, normalize_name, search_card_candidates, load_card_reference

from agents.ncbi_reference import load_ncbi_reference

from agents.gene_verification import verify_mentions

from agents.amr_lexicon import ANTIBIOTIC_ALIASES, extract_antibiotics, extract_mechanisms, extract_organisms

from agents.entity_aliases import (

    canonical_drug,

    canonical_organism,

    dedupe_by_key,

    drug_hits,

    find_organisms,

)

from agents.mutation_matching import extract_query_variants, format_substitution, parse_substitution

from config import OLLAMA_URL, OLLAMA_MODEL, OLLAMA_QUERY_TIMEOUT, AMR_DEBUG, AMR_LLM_QUERY_ENABLED


AA_CODES = "ARNDCQEGHILKMFPSTWYV"

VARIANT_RE = re.compile(r"\b([ARNDCQEGHILKMFPSTWY])\s*(\d{1,5})\s*([ARNDCQEGHILKMFPSTWY])\b", re.I)

TOKEN_RE = re.compile(r"[A-Za-z0-9().'-]+")


AMR_STOPWORDS = {
    "amr", "antibiotic", "antibiotics", "resistance", "resistant",
    "antimicrobial", "bacteria", "bacterial", "mutation", "mutations",
    "variant", "variants", "gene", "genes", "protein", "proteins",
    "mechanism", "mechanisms", "literature", "paper", "papers", "study",
    "studies", "research", "evidence", "ncbi", "card", "pubmed",
    "organism", "pathogen", "isolate", "isolates", "strain", "strains",
}

SCIENTIFIC_CONCEPT_PATTERNS = [
    # Promoters & Gene Expression
    r"\bpromot[eo]r\s+(?:region|mutation|mutations|alteration|alterations|variant|variants|based)\b",
    r"\bregulatory\s+(?:region|mutation|mutations|network|element|sequence)\b",
    r"\bupstream\s+(?:region|mutation|mutations|sequence)\b",
    r"\btranscriptional\s+(?:regulator|regulation|activator|repressor)\b",
    r"\boverexpression\b",
    # Efflux & Transport
    r"\befflux\s+(?:pump|pumps|system|systems|transporter|transporters|machinery|inhibition)\b",
    r"\bactive\s+efflux\b",
    r"\b(?:rnd|abc|mate|mfs|smr)\s+(?:family|efflux|transporter|pump)\b",
    r"\bmex[a-z0-9]+-opr[a-z0-9]+\b",
    r"\bacrab-tolc\b",
    # Membrane & Porins
    r"\bouter\s+membrane\s+(?:protein|proteins|permeability|vesicle|vesicles|barrier)\b",
    r"\bmembrane\s+(?:permeability|impermeability|integrity|disruption)\b",
    r"\bporin\s+(?:loss|deficiency|downregulation|closure|mutation|mutations|channel|channels)\b",
    # Cellular & Genetic Systems
    r"\btwo[- ]component\s+(?:system|systems|regulator|regulatory)\b",
    r"\bquorum\s+sensing\b",
    r"\bbiofilm\s+(?:formation|production|matrix|associated)\b",
    r"\bhorizontal\s+gene\s+transfer\b",
    r"\bplasmid[- ]mediated\b",
    r"\bintegron\b",
    r"\btransposon\b",
    r"\binsertion\s+sequence\b",
    # Evolution & Fitness
    r"\bfitness\s+cost\b",
    r"\bcompensatory\s+(?:mutation|mutations|evolution)\b",
    r"\bselection\s+pressure\b",
    # Phenotype & Clinical
    r"\bminimum\s+inhibitory\s+concentration\b",
    r"\b(?:clinical\s+)?breakpoint\b",
    r"\bheteroresistance\b",
    r"\bpersister\s+cells\b",
    r"\bsynerg(?:y|istic)\s+(?:effect|activity|combination)\b",
    r"\bcombination\s+therapy\b",
    r"\bcross[- ]resistance\b",
    r"\bco[- ]resistance\b",
    r"\bmultidrug\s+resist(?:ance|ant)\b",
]

CONVERSATIONAL_STOPWORDS = {
    "what", "which", "when", "where", "who", "whom", "whose", "why", "how",
    "does", "do", "did", "done", "doing", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "having", "would", "could", "should",
    "can", "may", "might", "must", "will", "shall", "the", "a", "an", "and",
    "or", "nor", "but", "if", "then", "else", "when", "at", "by", "for", "with",
    "about", "against", "between", "into", "through", "during", "before", "after",
    "above", "below", "to", "from", "up", "down", "in", "out", "on", "off", "over",
    "under", "again", "further", "once", "here", "there", "all", "any", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "not", "only",
    "own", "same", "so", "than", "too", "very", "s", "t", "can", "will", "just",
    "don", "should", "now", "tell", "me", "give", "show", "find", "search", "list",
    "provide", "explain", "describe", "detail", "paper", "papers", "study", "studies",
    "literature", "publication", "publications", "report", "reports", "overview",
    "please", "help", "information", "info", "based", "like", "kind", "thing",
}


def _extract_scientific_concepts(query):
    if not query:
        return []
    concepts = []
    seen = set()
    for pattern in SCIENTIFIC_CONCEPT_PATTERNS:
        for match in re.finditer(pattern, query, re.I):
            matched_str = match.group(0).strip()
            key = matched_str.lower()
            if key not in seen:
                concepts.append(matched_str)
                seen.add(key)
    return concepts


def _extract_query_keywords(query):
    if not query:
        return []
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9]|[A-Za-z0-9]", query)
    keywords = []
    seen = set()
    for token in tokens:
        low = token.lower()
        if low not in CONVERSATIONAL_STOPWORDS and len(low) > 1 and low not in seen:
            keywords.append(token)
            seen.add(low)
    return keywords


def _debug_log(*args):
    if AMR_DEBUG:
        try:
            print(*args)
        except Exception:
            safe = [str(a).encode("ascii", errors="replace").decode("ascii") for a in args]
            print(*safe)



def extract_variant(query):

    """Extract an explicitly written amino-acid substitution such as S450L."""

    if not query:

        return None

    match = VARIANT_RE.search(query)

    if not match:

        return None

    return f"{match.group(1).upper()}{match.group(2)}{match.group(3).upper()}"


def _extract_json(text):

    if not text:

        return None

    text = text.strip()

    try:

        return json.loads(text)

    except Exception:

        match = re.search(r"\{.*\}", text, re.S)

        if not match:

            return None

        try:

            return json.loads(match.group(0))

        except Exception:

            return None


def _ollama_prompt(query):

    return f'''You are the query-understanding component of an antimicrobial-resistance research assistant.

Analyze ONLY the user's query below. Do not invent entities and do not copy entities from examples.

The system is restricted to AMR topics: antibiotics, antimicrobial resistance, resistance genes,

mutations, variants, mechanisms, organisms/pathogens, resistance phenotypes, and scientific literature.


Return JSON only with this schema:

{{

  "domain": "amr" | "out_of_scope" | "uncertain",

  "intent": ["..."],

  "genes": ["only explicitly supported gene/determinant names"],

  "antibiotics": ["..."],

  "mutations": ["..."],

  "variants": ["..."],

  "organisms": ["..."],

  "mechanisms": ["..."],

  "information_needed": ["..."],

  "literature_needed": true | false,

  "needs_reference_lookup": true | false,

  "recency_requested": true | false,

  "time_window": "... or null"

}}


Important rules:

1. Never invent a gene merely because an antibiotic or resistance mechanism is mentioned.

2. Never invent a mutation or variant.

3. Preserve gene names as written by the user.

4. A query can be AMR-related even when it contains no gene.

5. For questions such as "what mutations cause rifampicin resistance?", genes may be empty.

6. For a literature request, literature_needed must be true.

7. If the query is not meaningfully related to AMR, use domain="out_of_scope".

8. The assistant supports all questions within antimicrobial resistance (AMR): genes, mutations,
   variants, antibiotics, mechanisms, organisms, resistance phenotypes, and comparative questions.
   Only use domain="out_of_scope" if the query is genuinely unrelated to AMR, antibiotics, or resistant pathogens.

9. Preserve any explicit gene, mutation, antibiotic, or mechanism mentioned.


USER QUERY:

{query}

'''


def _llm_understand(query):

    if not AMR_LLM_QUERY_ENABLED:

        return {}

    try:

        response = requests.post(

            OLLAMA_URL,

            json={

                "model": OLLAMA_MODEL,

                "prompt": _ollama_prompt(query),

                "stream": False,

                "format": "json",

                "options": {"temperature": 0},

            },

            timeout=OLLAMA_QUERY_TIMEOUT,

        )

        response.raise_for_status()

        payload = response.json()

        parsed = _extract_json(payload.get("response", ""))

        if isinstance(parsed, dict):

            return parsed

        if AMR_DEBUG:

            print(

                f"[query_understanding] LLM response was not a JSON object "

                f"(got {type(parsed).__name__}); falling back to deterministic extraction.",

                file=sys.stderr,

            )

        return {}

    except (requests.RequestException, ValueError, TypeError) as exc:

        # Previously swallowed silently, which made a failed LLM call

        # (timeout, connection refused, bad response) indistinguishable from

        # "the model found nothing" -- organisms/antibiotics/information_needed

        # then silently defaulted to empty with no way to diagnose why. This

        # is debug-only (stderr, gated on AMR_DEBUG) so it never reaches the

        # user-facing answer or the API response.

        if AMR_DEBUG:

            print(f"[query_understanding] LLM understanding call failed: {type(exc).__name__}: {exc}", file=sys.stderr)

        return {}


def _ground_genes(query, genes):

    """Keep LLM gene mentions only when supported by the actual query/CARD."""

    query_key = normalize_name(query)

    grounded = []

    seen = set()

    for gene in genes or []:

        if not isinstance(gene, str):

            continue

        gene = gene.strip()

        key = normalize_name(gene)

        if not key or key in seen:

            continue

        if key in query_key:

            grounded.append(gene)

            seen.add(key)

            continue

        try:

            card = lookup_card(gene)

        except Exception:

            card = {}

        forms = [card.get("canonical_name"), *(card.get("synonyms") or [])]

        if any(normalize_name(f) and normalize_name(f) in query_key for f in forms if f):

            grounded.append(gene)

            seen.add(key)

    return grounded


def _query_tokens(query):

    return TOKEN_RE.findall(query or "")


@lru_cache(maxsize=1)
def _ncbi_gene_name_keys():
    """Build the NCBI AMR gene-name map once per process."""
    exact_map = {}
    try:
        ncbi = load_ncbi_reference()
    except (FileNotFoundError, OSError, ValueError):
        return exact_map
    for record in ncbi.get("records", []):
        for field in ("allele", "gene_family"):
            value = record.get(field, "")
            key = normalize_name(value)
            if key:
                exact_map.setdefault(key, value)
    return exact_map


def _reference_grounded_gene_mentions(query):
    """Discover explicit gene/determinant mentions using CARD/NCBI vocabulary.

    Supports exact allele names as well as partial gene symbols (e.g. ompK,
    pbp, mcr, qnr) grounded against CARD and NCBI references.
    """
    tokens = _query_tokens(query)
    if not tokens:
        return []

    spans = []
    seen_spans = set()
    for size in (3, 2, 1):
        for i in range(len(tokens) - size + 1):
            phrase = " ".join(tokens[i:i + size]).strip()
            key = normalize_name(phrase)
            if key and key not in seen_spans:
                spans.append(phrase)
                seen_spans.add(key)

    found = []
    found_keys = set()
    exact_map = _ncbi_gene_name_keys()

    # Standard English non-AMR words that must never be treated as gene names
    stopwords = {
        "what", "which", "how", "does", "cause", "causes", "with", "from", "are", "is",
        "about", "compare", "comparison", "versus", "vs", "difference", "between",
        "when", "where", "into", "that", "this", "their", "have", "been",
        "show", "tell", "paper", "papers", "study", "studies", "result", "results", "overview",
        "find", "finds", "found", "report", "reports", "reported", "role", "roles", "function",
        "associated", "association", "resistance", "resistant", "susceptibility", "susceptible",
        "antibiotic", "antibiotics", "antimicrobial", "antimicrobials", "bacterial", "bacteria",
        "organism", "pathogen", "strain", "isolate", "infection", "clinical", "mechanism",
        "mechanisms", "mutation", "mutations", "variant", "variants", "phenotype", "profile",
        "for", "the", "and", "but", "not", "all", "any", "can", "will", "would", "could",
        "confer", "confers", "conferring", "mediate", "mediates", "mediating",
        "harbor", "harbors", "harboring", "carry", "carries", "carrying",
        "express", "expresses", "expressing", "involve", "involves", "involved",
        "lead", "leads", "leading", "relate", "relates", "related",
        "efflux", "pump", "pumps", "porin", "porins", "permease", "permeases",
        "channel", "channels", "transporter", "transporters", "system", "systems",
        "complex", "complexes", "subunit", "subunits", "enzyme", "enzymes", "protein", "proteins",
        "target", "targets", "inactivation", "alteration", "alterations",
        "recipe", "chocolate", "cake", "capital", "france", "paris", "weather", "tokyo",
        "cookie", "cookies", "bake", "baking", "food", "city", "country", "make", "cook",
    }

    # Pass 1: exact matches in CARD or NCBI
    for phrase in spans:
        key = normalize_name(phrase)
        if not key or key in stopwords or len(key) < 2:
            continue
        try:
            result = search_card_candidates(phrase, include_related=False)
        except (FileNotFoundError, OSError, ValueError):
            result = {}
        if result.get("candidates") and key not in found_keys:
            top_rec = result["candidates"][0]
            if top_rec.get("category") == "mechanism":
                continue
            found.append(phrase)
            found_keys.add(key)
        elif key in exact_map and key not in found_keys:
            found.append(phrase)
            found_keys.add(key)

    # Pass 2: partial gene / prefix matching against CARD and NCBI (e.g. ompK, pbp, mcr, qnr)
    # A candidate gene prefix is 3 to 6 characters (e.g. ompk, pbp, mcr, vana)
    for phrase in spans:
        key = normalize_name(phrase)
        if not key or key in stopwords or len(key) < 3 or len(key) > 6 or key in found_keys:
            continue
        try:
            card_res = search_card_candidates(phrase, include_related=True)
            top_score = max((c.get("match_score", 0) for c in card_res.get("candidates", [])), default=0)
            if top_score >= 80:
                top_cand = next((c for c in card_res.get("candidates", []) if c.get("match_score", 0) >= 80), None)
                if top_cand and top_cand.get("category") == "mechanism":
                    continue
                found.append(phrase)
                found_keys.add(key)
                continue
        except (FileNotFoundError, OSError, ValueError):
            pass

        # Check NCBI prefix matching: does any NCBI gene symbol start with this prefix?
        key_without_bla = key[3:] if key.startswith("bla") and len(key) > 3 else key
        if any(k.startswith(key) or k.startswith(key_without_bla) for k in exact_map if len(k) > len(key_without_bla)):
            found.append(phrase)
            found_keys.add(key)
            continue

    found.sort(key=lambda item: -len(normalize_name(item)))
    selected = []
    selected_keys = []
    for phrase in found:
        key = normalize_name(phrase)
        if any(key == existing or key in existing or existing in key for existing in selected_keys):
            continue
        selected.append(phrase)
        selected_keys.append(key)
    return selected


def _reference_amr_entities(query):

    """Extract exact CARD-backed non-gene AMR entities when LLM extraction is incomplete."""

    tokens = _query_tokens(query)

    if not tokens:

        return {"antibiotics": [], "mechanisms": []}


    found = {"antibiotics": [], "mechanisms": []}

    seen = {"antibiotics": set(), "mechanisms": set()}


    try:

        index = load_card_reference()

        lookup_maps = [

            index.get("canonical_lookup", {}),

            index.get("synonym_lookup", {}),

        ]

        for token in tokens:

            key = normalize_name(token)

            if not key:

                continue


            records = []

            for mapping in lookup_maps:

                match = mapping.get(key)

                if not match:

                    continue

                if match.get("status") == "collision":

                    records.extend(match.get("records", []))

                else:

                    records.append(match.get("record"))


            for record in records:

                if not record or record.get("category") == "gene":

                    continue

                props = set(record.get("properties", []))

                canonical = record.get("canonical_name")

                if not canonical:

                    continue


                if any("classified_as_antibiotic" in prop for prop in props):

                    if key not in seen["antibiotics"]:

                        found["antibiotics"].append(canonical)

                        seen["antibiotics"].add(key)


                if any("classified_as_resistance_mechanism" in prop for prop in props):

                    if key not in seen["mechanisms"]:

                        found["mechanisms"].append(canonical)

                        seen["mechanisms"].add(key)

    except (FileNotFoundError, OSError, ValueError):

        pass


    return found


_COMMON_AMR_GENE_SYMBOLS = {

    "rpob", "katg", "inha", "fabg1", "gyrA".lower(), "gyrb", "parc", "pare",

    "rrs", "rpsl", "pncA".lower(), "embb", "ethA".lower(), "eis", "folp1",

    "alr", "ddl", "mura", "murb", "gyra", "ompk35", "ompk36", "ompc",

}


def _explicit_gene_symbol_mentions(query):

    """Recover gene symbols explicitly written in the query, even if absent from CARD/NCBI.


    This is important for common chromosomal resistance genes such as katG,

    which may not occur as a standalone determinant in the bundled AMRFinderPlus

    table. A candidate must occur in a gene/mutation context; ordinary words,

    organisms and amino-acid substitutions are excluded.

    """

    text = str(query or "")

    token_re = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_-]{1,15})(?![A-Za-z0-9])")

    found = []

    for match in token_re.finditer(text):

        token = match.group(1)

        low = token.lower()

        if low in _COMMON_AMR_GENE_SYMBOLS:

            found.append(token)

            continue

        if parse_substitution(token):

            continue

        # Require a gene-like mixed-case / digit-bearing token plus a nearby

        # syntactic cue; do not treat arbitrary nouns as genes.

        gene_like = (any(c.islower() for c in token) and any(c.isupper() for c in token[1:])) or any(c.isdigit() for c in token)

        if not gene_like:

            continue

        left = text[max(0, match.start() - 30):match.start()]

        right = text[match.end():min(len(text), match.end() + 35)]

        before_cue = re.search(r"\b(?:in|of|within|for|gene|genes|determinant|determinants)\s*$", left, re.I)

        after_cue = re.search(r"^\s*(?:gene|genes|mutations?|variants?|substitutions?|alterations?)\b", right, re.I)

        after_list_cue = re.search(r"^.{0,25}\b(?:mutations?|variants?|substitutions?|genes|determinants)\b", right, re.I)

        if before_cue or after_cue or after_list_cue:

            found.append(token)

    out, seen = [], set()

    for gene in found:

        key = normalize_name(gene)

        if key and key not in seen:

            out.append(gene)

            seen.add(key)

    return out


def _merge_explicit_genes(query, llm_genes):

    grounded = _ground_genes(query, llm_genes)

    reference_genes = _reference_grounded_gene_mentions(query)

    explicit_symbols = _explicit_gene_symbol_mentions(query)


    result = []

    seen = set()

    for gene in [*grounded, *reference_genes, *explicit_symbols]:

        key = normalize_name(gene)

        if key and key not in seen:

            result.append(gene)

            seen.add(key)

    return result


def _deterministic_variant(query):

    variants = extract_query_variants(query)

    return variants[0] if variants else extract_variant(query)


def _literal_in_query(value, query):

    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(str(value).strip()) + r"(?![A-Za-z0-9])", query or "", re.I))

def _ground_terms(values, query, kind):

    """Keep an LLM-proposed entity only if the user's own query supports it.


    The LLM may not introduce an organism, antibiotic, mechanism or mutation the

    user did not write (the hallucination failure mode this project already hit

    for mutations). Accepted aliases/abbreviations count as support.

    """

    grounded = []

    query_orgs = find_organisms(query) if kind == "organisms" else {}

    for value in _clean_list(values):

        if kind == "antibiotics":

            # Do not let a specific drug query silently expand to a class label

            # (e.g. ciprofloxacin -> fluoroquinolone). A class is accepted only

            # when the class itself is present in the user's words.

            class_terms = {

                "fluoroquinolone", "carbapenem", "cephalosporin", "aminoglycoside",

                "glycopeptide", "macrolide", "beta-lactam", "sulfonamide",

            }

            canonical = canonical_drug(value).lower()

            if canonical in class_terms:

                ok = _literal_in_query(value, query) or any(

                    _literal_in_query(alias, query)

                    for alias in ANTIBIOTIC_ALIASES.get(canonical, [])

                )

            else:

                ok = drug_hits(query, value) or _literal_in_query(value, query)

        elif kind == "organisms":

            ok = canonical_organism(value) in query_orgs or _literal_in_query(value, query)

        else:

            ok = _literal_in_query(value, query)

        if ok:

            grounded.append(value)

    return grounded


def _ground_mutations(values, query):

    """LLM mutations/variants survive only if written in the query (any notation)."""

    written = set()

    for item in extract_query_variants(query):

        written.add(parse_substitution(item))

    out = []

    for value in _clean_list(values):

        sub = parse_substitution(value)

        if sub and sub in written:

            out.append(format_substitution(sub))

    return out


def _fallback_understanding(query):
    q = query.lower()

    ref_genes = _reference_grounded_gene_mentions(query)
    lex_drugs = extract_antibiotics(query)
    ref_drugs = _reference_amr_entities(query).get("antibiotics", [])
    lex_orgs = extract_organisms(query)
    lex_mechs = extract_mechanisms(query)
    ref_mechs = _reference_amr_entities(query).get("mechanisms", [])
    variants = extract_query_variants(query)

    amr_terms = [
        "antibiotic", "antibiotics", "antimicrobial", "antimicrobials",
        "resistance", "resistant", "amr", "drug resistance", "susceptibility",
        "susceptible", "carbapenemase", "esbl", "efflux pump", "efflux", "mutation",
        "mutations", "variant", "variants", "substitution", "substitutions",
        "allele", "alleles", "polymorphism", "genetic change", "sequence change",
        "alteration", "gene", "genes", "colistin", "rifampicin", "rifampin",
        "carbapenem", "fluoroquinolone", "beta-lactam", "aminoglycoside",
        "tetracycline", "macrolide", "porin", "porins", "penicillin", "vancomycin",
        "meropenem", "imipenem", "polymyxin", "isoniazid", "linezolid",
        "promoter", "promoter region", "mexab", "mexab-oprm", "acrab", "acrab-tolc",
        "rnd efflux", "outer membrane", "membrane permeability", "target alteration",
        "methyltransferase", "phosphoethanolamine", "lipid a",
    ]

    has_any_amr_entity = bool(ref_genes or lex_drugs or ref_drugs or variants or lex_mechs or ref_mechs)
    domain = "amr" if (has_any_amr_entity or any(t in q for t in amr_terms)) else "uncertain"

    intent = []

    if any(x in q for x in ["paper", "papers", "literature", "study", "studies", "publication"]):
        intent.append("literature_retrieval")

    mutation_terms = (
        "mutation", "mutations", "mutational", "variant", "variants",
        "variation", "variations", "substitution", "substitutions",
        "allele", "alleles", "polymorphism", "polymorphisms",
        "genetic change", "genetic changes", "amino-acid change",
        "amino acid change", "sequence change", "sequence changes",
        "alteration", "alterations", "replacement", "replacements",
    )
    if variants or any(term in q for term in mutation_terms):
        intent.append("mutation_variant_information")

    if any(x in q for x in [
        "mechanism", "mechanisms", "how does", "how do", "confer", "cause resistance",
        "pathway", "porin", "porins", "efflux", "pump", "promoter", "promoter region",
        "outer membrane", "permeability", "target alteration", "target modification",
        "methyltransferase", "rnd efflux", "mexab", "acrab", "oprm", "oprd",
        "phosphoethanolamine", "lipid a", "enzymatic inactivation", "drug inactivation",
    ]):
        intent.append("resistance_mechanism")

    if any(x in q for x in ["compare", "comparison", "versus", " vs ", "difference"]):
        intent.append("comparison")

    if not intent:
        if variants:
            intent.append("mutation_variant_information")
        elif ref_genes and (lex_drugs or ref_drugs or "resistance" in q):
            intent.extend(["resistance_mechanism", "amr_information_retrieval"])
        elif ref_genes:
            intent.extend(["amr_information_retrieval", "resistance_mechanism"])
        elif lex_mechs or ref_mechs:
            intent.append("resistance_mechanism")
        else:
            intent.append("amr_information_retrieval")

    return {
        "domain": domain,
        "intent": list(dict.fromkeys(intent)),
        "genes": ref_genes,
        "antibiotics": list(dict.fromkeys([*lex_drugs, *ref_drugs])),
        "mutations": variants,
        "variants": variants,
        "organisms": lex_orgs,
        "mechanisms": list(dict.fromkeys([*lex_mechs, *ref_mechs])),
        "scientific_concepts": _extract_scientific_concepts(query),
        "query_keywords": _extract_query_keywords(query),
        "information_needed": ["AMR information", "supporting evidence"],
        "literature_needed": True,
        "needs_reference_lookup": True,
        "recency_requested": any(x in q for x in ["recent", "latest", "new", "last year", "last 5 years"]),
        "time_window": None,
    }


def _amr_scope(query, intents, entities, llm_domain=None):
    """Determine whether the query is within the domain of antimicrobial resistance (AMR).

    The research assistant supports all queries within AMR, including:
    - Resistance genes, determinants, alleles, and gene families (e.g. rpoB, vanA, mecA, blaKPC)
    - Genetic variants, mutations, polymorphisms, and amino acid substitutions (e.g. S450L, S83L)
    - Antibiotics and antimicrobial drug classes (e.g. rifampicin, vancomycin, carbapenems)
    - Bacterial pathogens and organisms in AMR contexts (e.g. M. tuberculosis, MRSA, VRE, E. coli)
    - Resistance mechanisms (e.g. efflux pumps, porin loss, enzymatic inactivation, target alteration)
    - Genotype-phenotype relationships, comparative queries, and clinical susceptibility.

    Only queries that have no biological relevance to AMR/antimicrobial/bacterial topics are rejected.
    """
    q = (query or "").lower()

    # Explicit entity presence
    has_genes = bool(entities.get("genes"))
    has_drugs = bool(entities.get("antibiotics"))
    has_mutations = bool(entities.get("mutations") or entities.get("variants"))
    has_mechanisms = bool(entities.get("mechanisms"))
    has_organisms = bool(entities.get("organisms"))

    if has_genes or has_drugs or has_mutations or has_mechanisms:
        return True

    # Check for AMR domain terms in query text
    amr_keywords = (
        "amr", "antibiotic", "antibiotics", "antimicrobial", "antimicrobials",
        "resistance", "resistant", "susceptibility", "susceptible", "multidrug",
        "multi-drug", "mdr", "xdr", "esbl", "carbapenemase", "efflux", "porin",
        "porins", "mrsa", "vre", "cre", "crkp", "pathogen", "superbug", "colistin",
        "beta-lactam", "beta-lactamase", "aminoglycoside", "fluoroquinolone",
        "glycopeptide", "macrolide", "tetracycline", "mutation", "mutations",
        "variant", "variants", "determinant", "determinants", "mechanism",
        "mechanisms", "minimum inhibitory", "mic", "breakpoint", "vancomycin",
        "rifampicin", "rifampin", "carbapenem", "meropenem", "imipenem",
        "promoter", "promoter region", "mexab", "acrab", "rnd efflux",
        "outer membrane", "membrane permeability", "phosphoethanolamine",
        "methyltransferase", "lipid a", "target alteration", "target modification",
    )
    if any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", q) for w in amr_keywords):
        return True

    if has_organisms and any(w in q for w in ("strain", "isolate", "infection", "bacteria", "bacterial", "drug", "treat", "therapy", "resistance", "amr")):
        return True

    if llm_domain == "amr":
        return True

    return False


VALID_INTENTS = {

    "literature_retrieval",

    "mutation_variant_information",

    "resistance_mechanism",

    "comparison",

    "amr_information_retrieval",

}


def _clean_list(values):

    """Remove null-like LLM placeholders and normalize entity lists."""

    if not isinstance(values, list):

        return []

    out = []

    seen = set()

    for value in values:

        if value is None:

            continue

        value = str(value).strip()

        if not value or value.lower() in {"null", "none", "n/a", "na"}:

            continue

        key = value.lower()

        if key not in seen:

            out.append(value)

            seen.add(key)

    return out


def _normalize_intents(query, raw_intents):

    """Keep only supported intents and recover obvious intent wording from the query."""

    q = (query or "").lower()

    intents = []

    for value in _clean_list(raw_intents):

        key = value.strip().lower()

        if key in VALID_INTENTS:

            intents.append(key)

        elif any(term in key for term in ("mechanism", "how does", "how do", "confer", "cause resistance")):

            intents.append("resistance_mechanism")

        elif any(term in key for term in ("mutation", "variant", "substitution")):

            intents.append("mutation_variant_information")

        elif any(term in key for term in ("compare", "comparison", "versus", " vs ")):

            intents.append("comparison")

        elif any(term in key for term in ("literature", "paper", "publication", "study")):

            intents.append("literature_retrieval")


    if any(term in q for term in (
        "how does", "how do", "mechanism", "confer resistance", "cause resistance",
        "porin", "porins", "efflux", "pump", "promoter", "promoter region",
        "outer membrane", "permeability", "target alteration", "target modification",
        "methyltransferase", "rnd efflux", "mexab", "acrab", "phosphoethanolamine",
    )):
        intents.append("resistance_mechanism")

    if any(term in q for term in ("mutation", "mutations", "mutational", "variant", "variants", "variation", "variations", "substitution", "substitutions", "allele", "alleles", "polymorphism", "polymorphisms", "genetic change", "genetic changes", "amino-acid change", "amino acid change", "sequence change", "sequence changes", "alteration", "alterations", "replacement", "replacements")):

        intents.append("mutation_variant_information")

    if any(term in q for term in ("compare", "comparison", "versus", " vs ")):

        intents.append("comparison")

    if any(term in q for term in ("paper", "papers", "literature", "publication", "study", "studies")):

        intents.append("literature_retrieval")


    if not intents:

        intents.append("amr_information_retrieval")

    return list(dict.fromkeys(intents))


def understand_query(query, selected_candidate=None):

    _debug_log("\n========== AMR QUERY DEBUG ==========")

    _debug_log("ORIGINAL QUERY:", repr(query))


    llm = _llm_understand(query)

    _debug_log("LLM OUTPUT:", repr(llm))


    if not llm:

        llm = _fallback_understanding(query)

        _debug_log("FALLBACK OUTPUT:", repr(llm))


    genes = _merge_explicit_genes(query, llm.get("genes", []))

    reference_entities = _reference_amr_entities(query)


    lexical_antibiotics = extract_antibiotics(query)

    lexical_mechanisms = extract_mechanisms(query)

    lexical_organisms = extract_organisms(query)


    variant = _deterministic_variant(query)

    query_variants = extract_query_variants(query)


    grounded_mutations = _ground_mutations(

        llm.get("mutations"), query

    )

    llm_variants = _ground_mutations(

        llm.get("variants"), query

    )


    llm_antibiotics = _ground_terms(

        llm.get("antibiotics"), query, "antibiotics"

    )

    llm_organisms = _ground_terms(

        llm.get("organisms"), query, "organisms"

    )

    llm_mechanisms = _ground_terms(

        llm.get("mechanisms"), query, "mechanisms"

    )


    organisms = dedupe_by_key(

        [*lexical_organisms, *llm_organisms],

        canonical_organism,

    )


    antibiotics = dedupe_by_key(

        [

            *llm_antibiotics,

            *(reference_entities.get("antibiotics") or []),

            *lexical_antibiotics,

        ],

        canonical_drug,

    )


    mutations = list(dict.fromkeys([

        *query_variants,

        *grounded_mutations,

        *([variant] if variant else []),

    ]))


    _debug_log("GENES:", repr(genes))

    _debug_log("REFERENCE ENTITIES:", repr(reference_entities))

    _debug_log("LEXICAL ANTIBIOTICS:", repr(lexical_antibiotics))

    _debug_log("LLM ANTIBIOTICS:", repr(llm_antibiotics))

    _debug_log("FINAL ANTIBIOTICS:", repr(antibiotics))

    _debug_log("LEXICAL ORGANISMS:", repr(lexical_organisms))

    _debug_log("LLM ORGANISMS:", repr(llm_organisms))

    _debug_log("FINAL ORGANISMS:", repr(organisms))

    _debug_log("DETERMINISTIC VARIANT:", repr(variant))

    _debug_log("QUERY VARIANTS:", repr(query_variants))

    _debug_log("GROUNDED LLM MUTATIONS:", repr(grounded_mutations))

    _debug_log("GROUNDED LLM VARIANTS:", repr(llm_variants))

    _debug_log("FINAL MUTATIONS:", repr(mutations))


    verification = verify_mentions(

        genes,

        variant=variant,

        selected_candidate=selected_candidate,

    )

    _debug_log("GENE VERIFICATION:", repr(verification))


    intents = _normalize_intents(query, llm.get("intent"))


    if variant and "mutation_variant_information" not in intents:

        intents.append("mutation_variant_information")


    scope_entities = {

        "genes": genes,

        "mutations": mutations,

        "variants": llm_variants,

        "organisms": organisms,

    }


    scope_entities["antibiotics"] = antibiotics
    scope_entities["mechanisms"] = dedupe_by_key([
        *llm_mechanisms,
        *(reference_entities.get("mechanisms") or []),
        *lexical_mechanisms,
    ])

    scope_ok = _amr_scope(query, intents, scope_entities, llm_domain=llm.get("domain"))
    scope_domain = "amr" if scope_ok else "out_of_scope"


    _debug_log("FINAL INTENTS:", repr(intents))

    _debug_log("SCOPE ENTITIES:", repr(scope_entities))

    _debug_log("SCOPE OK:", repr(scope_ok))

    _debug_log("SCOPE DOMAIN:", repr(scope_domain))

    _debug_log("========== END QUERY DEBUG ==========\n")



    candidate_names = []
    for grp in verification.get("gene_candidate_groups", []):
        for c in grp.get("candidates", []):
            c_name = c.get("canonical_name")
            if c_name and c_name not in candidate_names and c.get("match_score", 0) >= 80:
                candidate_names.append(c_name)

    return {
        "original_query": query,
        "domain": scope_domain,
        "scope_ok": scope_ok,
        "intent": list(dict.fromkeys(intents)),
        "entities": {
            "genes": genes,
            "candidate_genes": candidate_names[:4],
            "antibiotics": antibiotics,
            "mutations": _clean_list(mutations),
            "variants": llm_variants,
            "organisms": organisms,
            "mechanisms": dedupe_by_key([
                *llm_mechanisms,
                *(reference_entities.get("mechanisms") or []),
                *lexical_mechanisms,
            ]),
            "scientific_concepts": _extract_scientific_concepts(query),
            "query_keywords": _extract_query_keywords(query),
        },

        "information_needed": llm.get("information_needed")

        or ["AMR information", "supporting evidence"],

        "literature_needed": bool(

            llm.get("literature_needed", True)

        ),

        "needs_reference_lookup": bool(

            llm.get("needs_reference_lookup", bool(genes))

        ),

        "recency_requested": bool(

            llm.get("recency_requested", False)

        ),

        "time_window": llm.get("time_window"),

        "variant": variant,

        "gene_mention": genes[0] if len(genes) == 1

        else (genes if genes else None),

        "gene": (

            (verification.get("selected_candidate") or {}).get(

                "canonical_name"

            )

            if verification.get("selected_candidate")

            else (genes[0] if len(genes) == 1 else None)

        ),

        "aro_id": (

            (verification.get("selected_candidate") or {}).get(

                "aro_id"

            )

            if verification.get("selected_candidate")

            else None

        ),

        "all_genes": verification.get("all_genes", []),

        "validated_card_genes": [],

        "gene_candidate_count": verification.get(

            "gene_candidate_count", 0

        ),

        "gene_candidate_groups": verification.get(

            "gene_candidate_groups", []

        ),

        "gene_candidates": verification.get("gene_candidates", []),

        "selected_candidate": verification.get("selected_candidate"),

        "selection_required": (

            bool(verification.get("selection_required", False))

            and not candidate_names

            and "mutation_variant_information" not in intents

        ),

        "resolutions": verification.get("resolutions", []),

        "literature_results": [],

    }
