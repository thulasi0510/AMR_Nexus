"""Central organism / antibiotic alias handling.

Every component that needs to decide "does this text mention organism X / drug Y"
goes through this module, so that:

* rifampin == rifampicin (one canonical entity, many accepted surface forms);
* organism abbreviations (M. tuberculosis, Mtb, MTBC ...) are accepted;
* matching uses word boundaries, never raw substrings;
* a paper whose subject is a DIFFERENT organism can be detected.

Entity lists shown to the user / sent to the planner hold canonical names only.
Alias expansion happens at match time and when building PubMed queries.
"""
import re
from collections import Counter

from agents.amr_lexicon import (
    ANTIBIOTIC_ALIASES,
    DRUG_ABBREVIATIONS,
    DRUG_CLASS_MEMBERS,
    DRUG_SYNONYMS,
    ORGANISM_ALIASES,
    ORGANISM_DISEASE_TERMS,
    ORGANISM_GENERA,
)

_NONWORD = re.compile(r"[^a-z0-9]+")


def entity_key(value):
    """Case/punctuation-insensitive identity key ("M. tuberculosis" == "m tuberculosis")."""
    return _NONWORD.sub("", str(value or "").lower())


# ---------------------------------------------------------------- organisms --
_ORG_KEY_TO_CANON = {}
for _canon, _forms in ORGANISM_ALIASES.items():
    _ORG_KEY_TO_CANON[entity_key(_canon)] = _canon
    for _f in _forms:
        _ORG_KEY_TO_CANON.setdefault(entity_key(_f), _canon)

_GENUS_SET = {g.lower() for g in ORGANISM_GENERA} | {c.split()[0].lower() for c in ORGANISM_ALIASES}


def canonical_organism(name):
    """Canonical organism name if known to the lexicon, else the cleaned input."""
    cleaned = re.sub(r"\s+", " ", str(name or "").strip())
    return _ORG_KEY_TO_CANON.get(entity_key(cleaned), cleaned)


def is_known_organism(name):
    return entity_key(name) in _ORG_KEY_TO_CANON


def organism_terms(name):
    """Canonical name plus all accepted surface forms (deduplicated)."""
    canon = canonical_organism(name)
    forms = ORGANISM_ALIASES.get(canon, [])
    out, seen = [], set()
    for form in [canon, *forms]:
        key = form.lower()
        if key not in seen:
            seen.add(key)
            out.append(form if form == canon else form)
    return out


def _form_pattern(form):
    tokens = [t for t in re.split(r"[\s.]+", form.lower()) if t]
    body = r"\.?\s*".join(re.escape(t) for t in tokens)
    return r"(?<![a-z0-9])" + body + r"(?![a-z0-9])"


_ORG_PATTERNS = {
    canon: re.compile(
        "|".join(_form_pattern(f) for f in sorted({canon, *forms}, key=len, reverse=True)),
        re.I,
    )
    for canon, forms in ORGANISM_ALIASES.items()
}
_DISEASE_PATTERNS = {
    canon: re.compile("|".join(_form_pattern(t) for t in terms), re.I)
    for canon, terms in ORGANISM_DISEASE_TERMS.items()
}
_BINOMIAL = re.compile(r"\b([A-Z][a-z]{3,})\s+([a-z]{3,})\b")
_ABBREV_BINOMIAL = re.compile(r"\b([A-Z])\.\s?([a-z]{4,})\b")
_GENUS_ONLY_EPITHETS = {"spp", "species", "sp"}
_ABBREV_STOP = {
    "value", "values", "were", "with", "from", "that", "this", "than", "also", "only",
    "which", "after", "before", "these", "those", "such", "between", "using", "used",
    "been", "have", "other", "while", "when", "found", "showed", "study",
}


def find_organisms(text):
    """Count organism mentions in text.

    Returns Counter keyed by canonical name for gazetteer organisms, or by
    "Genus species" / "X. species" / "Genus spp." for organisms outside it.
    """
    counts = Counter()
    work = str(text or "")
    for canon, pattern in _ORG_PATTERNS.items():
        hits = pattern.findall(work)
        if hits:
            counts[canon] += len(hits)
            work = pattern.sub(" ", work)
    for m in _BINOMIAL.finditer(work):
        genus, species = m.group(1), m.group(2)
        if genus.lower() not in _GENUS_SET:
            continue
        if species in _GENUS_ONLY_EPITHETS:
            counts[f"{genus} spp."] += 1
        else:
            counts[f"{genus} {species}"] += 1
    for m in _ABBREV_BINOMIAL.finditer(work):
        if m.group(2) in _ABBREV_STOP:
            continue
        counts[f"{m.group(1)}. {m.group(2)}"] += 1
    return counts


def disease_term_hit(text, organism):
    pattern = _DISEASE_PATTERNS.get(canonical_organism(organism))
    return bool(pattern and pattern.search(str(text or "")))


def organism_assessment(paper, queried_organisms):
    """Decide whether a paper is about the queried organism, from its own content.

    Only title, abstract and curated MeSH headings are used; a full-text mention
    (which may sit in a reference list or a comparison sentence) never
    establishes the organism.

    status:
      unspecified           no organism in the query
      queried               queried organism named in title/abstract and not outweighed
      queried_by_mesh       only curated MeSH headings name it, nothing else competes
      queried_by_disease_term  only the disease (e.g. "tuberculosis") is named, nothing else competes
      other_organism        a different organism is the paper's subject
      mixed_organism        queried and another organism are about equally prominent
      not_stated            no organism information at all
    """
    queried = [canonical_organism(o) for o in (queried_organisms or []) if str(o).strip()]
    queried = list(dict.fromkeys(queried))
    if not queried:
        return {"status": "unspecified", "basis": "no organism requested", "queried": [], "others": {}}

    title = str(paper.get("title") or "")
    abstract = str(paper.get("abstract") or "")
    mesh = [str(m) for m in (paper.get("mesh_terms") or [])]
    t_counts, a_counts = find_organisms(title), find_organisms(abstract)

    q_genera = {q.split()[0].lower() for q in queried}

    def split(counts):
        q = sum(counts.get(c, 0) for c in queried)
        others = {}
        for name, n in counts.items():
            if name in queried:
                continue
            if name.endswith(" spp.") and name.split()[0].lower() in q_genera:
                continue  # "Mycobacterium spp." does not contradict M. tuberculosis
            others[name] = n
        return q, others

    q_t, o_t = split(t_counts)
    q_a, o_a = split(a_counts)
    others = dict(Counter(o_t) + Counter(o_a))
    q_total = q_t + q_a
    max_other = max(others.values()) if others else 0
    mesh_hit = any(entity_key(m) == entity_key(c) for m in mesh for c in queried)
    disease_hit = any(disease_term_hit(f"{title} {abstract}", c) for c in queried)

    base = {"queried": queried, "others": others, "queried_in_title": q_t, "queried_in_abstract": q_a}
    if o_t and not q_t:
        return {**base, "status": "other_organism", "basis": "title names a different organism"}
    if o_t and q_t:
        # e.g. "... in Acinetobacter baumannii and Escherichia coli": a finding cannot be
        # attributed to the queried organism from the title/abstract alone.
        return {**base, "status": "mixed_organism", "basis": "title names the queried and another organism"}
    if q_total:
        if others and not q_t and max_other >= q_total:
            status = "other_organism" if max_other > q_total else "mixed_organism"
            return {**base, "status": status, "basis": "other organism at least as prominent in abstract"}
        return {**base, "status": "queried", "basis": "title" if q_t else "abstract"}
    if others:
        return {**base, "status": "other_organism", "basis": "abstract names a different organism"}
    if mesh_hit:
        return {**base, "status": "queried_by_mesh", "basis": "MeSH heading"}
    if disease_hit:
        return {**base, "status": "queried_by_disease_term", "basis": "disease term in title/abstract"}
    return {**base, "status": "not_stated", "basis": "organism not stated in title/abstract/MeSH"}


ORGANISM_OK_STATUSES = {"unspecified", "queried", "queried_by_mesh", "queried_by_disease_term"}


# -------------------------------------------------------------------- drugs --
def canonical_drug(name):
    cleaned = re.sub(r"\s+", " ", str(name or "").strip())
    low = cleaned.lower()
    if low in DRUG_SYNONYMS:
        return DRUG_SYNONYMS[low]
    if low in ANTIBIOTIC_ALIASES:
        return low
    return cleaned


def drug_terms(name, include_class_members=True, include_abbreviations=False):
    """Accepted surface forms for a drug: canonical, synonyms, lexicon aliases,
    and (for a class term) member drugs. Abbreviations are optional because they
    are too noisy for PubMed queries but are fine for content matching."""
    canon = canonical_drug(name)
    low = canon.lower()
    terms = [canon]
    terms += [s for s, c in DRUG_SYNONYMS.items() if c == low]
    terms += list(ANTIBIOTIC_ALIASES.get(low, []))
    if include_class_members:
        terms += DRUG_CLASS_MEMBERS.get(low, [])
    if include_abbreviations:
        terms += DRUG_ABBREVIATIONS.get(low, [])
    out, seen = [], set()
    for t in terms:
        k = t.lower() if t.upper() != t else t
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def drug_hits(text, name):
    """Match the queried drug without silently substituting another drug.

    A named drug accepts its aliases/synonyms only (e.g. rifampin/rifampicin).
    A query naming a drug class may match that class's members. This distinction
    prevents a ciprofloxacin-specific query from accepting a levofloxacin-only
    paper as direct evidence while preserving class-level queries.
    """
    text = str(text or "")
    canon = canonical_drug(name)
    include_members = canon.lower() in DRUG_CLASS_MEMBERS
    for term in drug_terms(canon, include_class_members=include_members):
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text, re.I):
            return True
    for abbr in DRUG_ABBREVIATIONS.get(canon.lower(), []):
        # case-sensitive; "MTB/RIF" is an assay name, not a drug mention
        if re.search(r"(?<![A-Za-z0-9/])" + re.escape(abbr) + r"(?![A-Za-z0-9])", text):
            return True
    return False


def drug_resistance_link(sentence, drug):
    """Verify that `drug` is the target of the resistance/susceptibility claim in `sentence`.

    If the drug is mentioned purely as an isolate/organism background descriptor
    (e.g. 'vancomycin-resistant enterococci' / 'VRE', 'methicillin-resistant S. aureus' / 'MRSA')
    while the resistance claim in the sentence pertains to a different antibiotic (such as
    linezolid resistance in VRE), this returns False for the background drug.
    """
    text = str(sentence or "")
    if not drug_hits(text, drug):
        return False

    canon = canonical_drug(drug).lower()
    # Remove isolate/strain descriptor clauses where a drug serves as an organism label
    cleaned = re.sub(
        r"\b(vancomycin|methicillin|oxacillin|carbapenem|colistin|rifampicin|rifampin|multidrug|multi-drug)[- ]"
        r"(?:resistant|susceptible)\s+(?:enterococc\w*|staphylococc\w*|klebsiella\w*|acinetobacter\w*|pseudomonas\w*|tuberculosis|bacilli|isolates?|strains?|patients?|cases?|infections?)\b",
        "",
        text,
        flags=re.I,
    )
    if not drug_hits(cleaned, drug):
        # The queried drug was in a descriptor. If another distinct antibiotic is present,
        # then the queried drug is indeed just background context.
        other_drugs = [d for d in ANTIBIOTIC_ALIASES if canonical_drug(d).lower() != canon and drug_hits(text, d)]
        if other_drugs:
            return False
        return True
    return True


# ----------------------------------------------------------------- generic --
def dedupe_by_key(values, canonicalizer=None):
    """Order-preserving, case/punctuation-insensitive de-duplication."""
    out, seen = [], set()
    for v in values or []:
        v = str(v).strip()
        if not v:
            continue
        v = canonicalizer(v) if canonicalizer else v
        k = entity_key(v)
        if k and k not in seen:
            seen.add(k)
            out.append(v)
    return out


def gene_hit(text, gene):
    gene = str(gene or "").strip()
    if not gene:
        return False
    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(gene) + r"(?![A-Za-z0-9])", str(text or ""), re.I))
