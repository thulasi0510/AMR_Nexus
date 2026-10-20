"""Notation-aware mutation matching and content-based paper assessment.

One implementation shared by the PubMed filter (literature.py), the evidence
tiering (evidence_analysis.py) and the literature mutation extractor
(mutation_evidence.py), so a paper cannot be "relevant" in one stage and
"direct evidence" in another for different reasons.

Accepted notations for the same substitution:
  S450L   p.S450L   Ser450Leu   p.Ser450Leu   Ser-450-Leu   S450->L   Ser450→Leu
and, where a documented numbering scheme exists (amr_lexicon.NUMBERING_SCHEMES),
the alternative-scheme position (e.g. rpoB S450L in M. tuberculosis == S531L in
E. coli numbering).
"""
import re

from agents.amr_lexicon import NUMBERING_SCHEMES
from agents.entity_aliases import (
    ORGANISM_OK_STATUSES,
    canonical_organism,
    drug_hits,
    entity_key,
    gene_hit,
    organism_assessment,
)

AA3 = {
    "Ala": "A", "Arg": "R", "Asn": "N", "Asp": "D", "Cys": "C", "Gln": "Q", "Glu": "E",
    "Gly": "G", "His": "H", "Ile": "I", "Leu": "L", "Lys": "K", "Met": "M", "Phe": "F",
    "Pro": "P", "Ser": "S", "Thr": "T", "Trp": "W", "Tyr": "Y", "Val": "V",
}
_AA3_UP = {k.upper(): v for k, v in AA3.items()}
_AA3_ALT = "|".join(AA3)
_AA1 = "ACDEFGHIKLMNPQRSTVWY"
_ARROW = r"(?:\s?(?:→|->|>|to)\s?)?"

_THREE_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?:p\.\(?)?({_AA3_ALT})[\s-]?(\d{{1,4}})[\s-]?{_ARROW}({_AA3_ALT})(?![a-z0-9])",
    re.I,
)
_ONE_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?:p\.\(?)?([{_AA1}])(\d{{1,4}}){_ARROW}([{_AA1}])(?![A-Za-z0-9])"
)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def parse_substitution(value):
    """'S450L' / 'p.Ser450Leu' -> ('S', 450, 'L'); None if not a simple substitution."""
    text = str(value or "").strip()
    m = _THREE_RE.fullmatch(text) or None
    if m:
        a, b = _AA3_UP.get(m.group(1).upper()), _AA3_UP.get(m.group(3).upper())
        return (a, int(m.group(2)), b) if a and b else None
    m = _ONE_RE.fullmatch(text)
    if m:
        return (m.group(1), int(m.group(2)), m.group(3))
    return None


def format_substitution(sub):
    return f"{sub[0]}{sub[1]}{sub[2]}"


def three_letter(sub):
    inv = {v: k for k, v in AA3.items()}
    return f"{inv[sub[0]]}{sub[1]}{inv[sub[2]]}"


def extract_substitutions(text):
    """All substitution mentions in text as dicts (ref, pos, alt, as_written, start)."""
    found = []
    spans = []
    for m in _THREE_RE.finditer(text or ""):
        a, b = _AA3_UP.get(m.group(1).upper()), _AA3_UP.get(m.group(3).upper())
        if a and b and a != b:
            found.append({"sub": (a, int(m.group(2)), b), "as_written": m.group(0), "start": m.start(), "end": m.end()})
            spans.append((m.start(), m.end()))
    for m in _ONE_RE.finditer(text or ""):
        if any(s <= m.start() < e for s, e in spans):
            continue
        a, b = m.group(1), m.group(3)
        if a != b:
            found.append({"sub": (a, int(m.group(2)), b), "as_written": m.group(0), "start": m.start(), "end": m.end()})
    found.sort(key=lambda x: x["start"])
    return found


def extract_query_variants(query):
    """Deterministic, ordered, de-duplicated one-letter variants written in a query."""
    out = []
    for item in extract_substitutions(query):
        v = format_substitution(item["sub"])
        if v not in out:
            out.append(v)
    return out


def records_for_matching(mutation_reference):
    """COMPLETE reference record set used to recognise mutations in papers.

    Never the 20 displayed rows. When organism-specific records exist (e.g. the WHO
    TB catalogue) only those define the notation, because organism-unspecified
    NCBI rows may use a different residue numbering.
    """
    ref = mutation_reference or {}
    records = ref.get("all_mutations") or ref.get("mutations") or []
    specific = [r for r in records if r.get("organism_specific") is not False]
    if specific:
        return specific
    # If every available record is organism-unspecified (e.g. AMRFinderPlus
    # allele annotations), do not use it to canonicalize mutation numbering for
    # a species-specific literature query. The paper's own mutation notation
    # remains available through the non-reference extraction path.
    if records and all(r.get("organism_specific") is False for r in records):
        return []
    return records


def numbering_schemes_for(genes, organisms):
    genes_l = {str(g).strip().lower() for g in genes or []}
    orgs = {canonical_organism(o) for o in organisms or []}
    return [s for s in NUMBERING_SCHEMES if s["gene"] in genes_l and s["organism"] in orgs]


def alternative_notations(variant, genes, organisms):
    """Other spellings of a variant to put in PubMed queries (3-letter, other numbering)."""
    sub = parse_substitution(variant)
    if not sub:
        return [str(variant)]
    forms = [format_substitution(sub), three_letter(sub)]
    for scheme in numbering_schemes_for(genes, organisms):
        alt = (sub[0], sub[1] + scheme["offset"], sub[2])
        forms += [format_substitution(alt), three_letter(alt)]
    return list(dict.fromkeys(forms))


class MutationIndex:
    """Lookup from any accepted notation to the reference mutation it denotes."""

    def __init__(self, reference_mutations, genes=None, organisms=None):
        self.native = {}
        self.alternative = {}
        self.non_substitution = {}
        self.genes = genes or []
        self.organisms = organisms or []
        schemes = numbering_schemes_for(genes, organisms)
        for ref in reference_mutations or []:
            sub = parse_substitution(ref)
            if sub:
                canon = format_substitution(sub)
                self.native.setdefault(sub, canon)
                for scheme in schemes:
                    alt = (sub[0], sub[1] + scheme["offset"], sub[2])
                    self.alternative.setdefault(alt, (canon, scheme["scheme"]))
            elif str(ref or "").strip():
                self.non_substitution[str(ref).strip().lower()] = str(ref).strip()

    def lookup(self, sub):
        if sub in self.native:
            return self.native[sub], "reference numbering"
        if sub in self.alternative:
            return self.alternative[sub]
        return None, None

    def __bool__(self):
        return bool(self.native or self.non_substitution)


def _sentences(text):
    return [s for s in _SENT_SPLIT.split(re.sub(r"\s+", " ", str(text or "")).strip()) if s]


def _gene_windows(sentences, genes, title=""):
    """Return conservative context windows for the queried gene.

    Adjacent sentences are useful when a paper says, for example, "The katG
    gene was sequenced. The S315T substitution was detected." However, context
    must not cross a sentence that explicitly discusses multiple/different
    genes, because that is a common source of rpoB/katG and gyrA/parC leakage.
    A neighboring sentence is therefore admitted only when its anchor sentence
    names the queried gene and no other recognizable gene token.
    """
    queried = {str(g).strip().lower() for g in genes or []}
    flagged = set()
    for i, sentence in enumerate(sentences):
        all_tokens = {m.group(0).lower() for m in _GENE_TOKEN.finditer(sentence)}
        queried_hits = {str(g).strip().lower() for g in genes or [] if gene_hit(sentence, g)}
        if not queried_hits:
            continue
        # A gene-bearing sentence that mentions another gene is not a safe
        # anchor for borrowing mutation mentions from adjacent sentences.
        if all_tokens and not all_tokens.issubset(queried):
            flagged.add(i)
            continue
        flagged.update(j for j in (i - 1, i, i + 1) if 0 <= j < len(sentences))
    return flagged


# Known AMR determinants, RNA genes, and gene prefixes across bacteria
_KNOWN_AMR_GENE_SYMBOLS = {
    "ddl", "ddla", "ddlb", "vana", "vanb", "vanc", "vand", "vane", "vang",
    "pbp", "pbp1a", "pbp1b", "pbp2", "pbp2a", "pbp2x", "meca", "mecc",
    "gyra", "gyrb", "parc", "pare", "rpob", "katg", "inha", "fabg1", "pnca",
    "embb", "etha", "rrs", "rrl", "rpsl", "eis", "folp", "folp1", "fola", "dfra",
    "sul1", "sul2", "sul3", "erma", "ermb", "ermc", "teta", "tetb", "tetm", "teto",
    "cfr", "optra", "poxta", "mcr", "mcr-1", "alr", "mura", "murb",
    "ompk35", "ompk36", "ompc", "ompf", "oprd", "mexa", "mexb", "oprm",
    "adea", "adeb", "adec", "arca", "arcb", "acra", "acrb", "tola", "tolc",
    "23s", "23s rrna", "16s", "16s rrna", "rrna", "rrn",
}

_KNOWN_GENES_RE = r"(?i:" + "|".join(sorted((re.escape(s) for s in _KNOWN_AMR_GENE_SYMBOLS), key=len, reverse=True)) + r")"

# Gene/protein-like tokens (gyrA, GyrA, parC, ParE, rpoB, Ddl, 23S rRNA, blaKPC ...)
# used to tell WHICH gene a substitution belongs to when a paper discusses several genes.
_GENE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?:[a-z]{3}[A-Z]\d?)"                     # e.g. gyrA, parC, rpoB, vanA, katG
    r"|(?:[A-Z][a-z]{2}[A-Z]\d?)"               # e.g. GyrA, ParC, RpoB, VanA, KatG
    r"|(?:[12][36]S(?:\s+rRNA)?)"               # e.g. 23S rRNA, 16S rRNA
    r"|(?i:bla[A-Za-z0-9_-]+)"                  # e.g. blaKPC-2, blaCTX-M-15
    r"|(?i:pbp\d[a-z]?|mec[AC]|van[A-G]\d?)"    # e.g. pbp2a, mecA, vanA
    r"|(?i:[A-Za-z]{2,5}\d{1,2}[a-z]?)"         # e.g. folP1, ompK35, oprD
    r"|(?:[A-Z]{3,5})"                          # ALL-CAPS acronyms: DDL, KPC, NDM, SHV (case sensitive: will not match English words)
    rf"|{_KNOWN_GENES_RE}"                      # known AMR gene symbols (ddl, alr, gyrA, katG ...)
    r")(?![A-Za-z0-9])"
)


_LIST_SEP = re.compile(r"^(?:\s|[/,&+]|\band\b|\bor\b|\bplus\b|\bwith\b|\bthen\b|\bfollowed by\b)*$", re.I)
_PREP_AFTER = re.compile(
    r"^\s*(?:\([^()]{0,40}\)\s*)?"
    r"(?:(?:point\s+|amino acid\s+|missense\s+)?(?:mutations?|substitutions?|variants?|changes?|alterations?)\s+)?"
    r"(?:in|of|within|on|at)\s+(?:the\s+)?(?:\w+\s+){0,2}$",
    re.I,
)


def _gene_mentions(sentence, genes):
    queried = {str(g).lower() for g in genes}
    mentions = []
    # Queried genes first with exact spans
    for g in genes:
        for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(str(g)) + r"(?![A-Za-z0-9])", sentence, re.I):
            mentions.append((m.start(), m.end(), str(g).lower(), True))
    # Other gene/determinant tokens
    for m in _GENE_TOKEN.finditer(sentence):
        token_str = m.group(0).lower()
        if token_str in queried or any(s <= m.start() < e for s, e, _, _ in mentions):
            continue
        if (
            token_str in _KNOWN_AMR_GENE_SYMBOLS
            or re.match(r"^[a-z]{3}[A-Z]\d?$", m.group(0))
            or token_str.startswith("bla")
            or "rrna" in token_str
            or token_str in {"23s", "16s"}
        ):
            mentions.append((m.start(), m.end(), token_str, False))
        else:
            left = sentence[max(0, m.start() - 25):m.start()]
            right = sentence[m.end():min(len(sentence), m.end() + 25)]
            if (
                re.search(r"\b(?:gene|genes|locus|operon|protein|mutation|mutations|variant|variants|substitutions?)\b", right, re.I)
                or re.search(r"\b(?:gene|genes|in|of|within)\s+$", left, re.I)
            ):
                mentions.append((m.start(), m.end(), token_str, False))
    return sorted(mentions)


def _cluster_substitutions(items, sentence):
    clusters = []
    for item in items:
        if clusters:
            prev = clusters[-1][-1]
            gap = sentence[prev["end"]:item["start"]]
            if len(gap) <= 25 and _LIST_SEP.match(gap):
                clusters[-1].append(item)
                continue
        clusters.append([item])
    return clusters


# Conservative protein-length sanity checks for well-characterized gene/species
# pairs and universal AMR determinant lengths.
_PROTEIN_LENGTH_MAX = {
    ("katg", "Mycobacterium tuberculosis"): 740,
    ("gyra", "Mycobacterium tuberculosis"): 838,
    ("gyra", "Escherichia coli"): 875,
    ("parc", "Escherichia coli"): 752,
    ("vana", "Enterococcus faecium"): 343,
    ("vana", "Enterococcus faecalis"): 343,
}

_CANONICAL_GENE_LENGTHS = {
    "vana": 343,
    "vanb": 343,
    "katg": 740,
    "gyra": 875,
    "parc": 752,
    "ddl": 360,
    "rpob": 1342,
}


def _mutation_position_plausible(mutation, genes, organisms=None):
    """Return False for impossible residue positions in known gene/species pairs.

    Only simple amino-acid substitutions are checked. Positions > 2000 are rejected
    for bacterial proteins as they indicate rRNA nucleotide positions or genomic coordinates.
    """
    sub = parse_substitution(mutation) if not isinstance(mutation, tuple) else mutation
    if not sub:
        return True
    pos = sub[1]
    gene_keys = {str(g or "").strip().lower() for g in (genes or [])}
    organism_keys = {canonical_organism(o) for o in (organisms or []) if str(o or "").strip()}

    # Species-specific limit
    limits = [limit for (gene, organism), limit in _PROTEIN_LENGTH_MAX.items()
              if gene in gene_keys and organism in organism_keys]
    if limits and pos > min(limits):
        return False

    # Gene-level universal length guards
    for g, limit in _CANONICAL_GENE_LENGTHS.items():
        if g in gene_keys and pos > limit:
            return False

    # Universal bacterial protein sanity bound
    if pos > 2000:
        return False

    return True


def _attributed_gene(sentence, cluster, mentions):
    """Which gene mention does this run of substitutions belong to?
    'M1/M2 in GyrA', 'GyrA: M1 + M2', 'in the gyrA gene (M1, M2)'. Falls back to
    the nearest mention. Returns the mention tuple, or None if the sentence names no gene."""
    if not mentions:
        return None
    start, end = cluster[0]["start"], cluster[-1]["end"]
    after = [m for m in mentions if m[0] >= end]
    if after and _PREP_AFTER.match(sentence[end:after[0][0]]):
        return after[0]
    before = [m for m in mentions if m[1] <= start]
    if before and start - before[-1][1] <= 80:
        between = sentence[before[-1][1]:start]
        if not re.search(r"[;]|\b(?:while|whereas|although|however|but)\b", between, re.I):
            return before[-1]
    nearest = min(mentions, key=lambda m: min(abs(m[0] - end), abs(start - m[1])))
    # Conservative attribution: a gene mentioned elsewhere in a long sentence
    # must not automatically own a mutation attached to another drug/phenotype.
    distance = min(abs(nearest[0] - end), abs(start - nearest[1]))
    if distance <= 40:
        between_near = sentence[min(nearest[1], start):max(nearest[0], end)]
        if not re.search(r"[;]|\b(?:while|whereas|although|however|but)\b", between_near, re.I):
            return nearest
    return None


def find_mutations_in_text(text, index, genes, requested=None, title="", organisms=None):
    """Mutation mentions that belong to the queried gene.

    A substitution counts when (a) it is attributed to the queried gene, or (b) its
    sentence names no gene at all and sits next to a sentence that does. A substitution
    attributed to a different gene in the same sentence (e.g. ParC/GyrB next to GyrA)
    is dropped. Returns (reference_matches, other_substitutions).
    """
    sentences = _sentences(f"{title}. {text}" if title else text)
    windows = _gene_windows(sentences, genes)
    requested_subs = {parse_substitution(r) for r in (requested or []) if parse_substitution(r)}
    orgs = organisms or getattr(index, "organisms", None)
    matches, others, seen = [], [], set()
    for i, sentence in enumerate(sentences):
        mentions = _gene_mentions(sentence, genes) if genes else []
        if genes:
            has_queried_gene = any(m[3] for m in mentions) or any(gene_hit(sentence, g) for g in genes)
            other_gene_mentions = [m for m in mentions if not m[3]]
            # A context-only sentence may contribute only if it does not name a
            # different gene itself. Explicitly gene-labelled sentences are
            # handled below by mutation-to-gene attribution.
            if not has_queried_gene and (i not in windows or other_gene_mentions):
                continue
        if index and index.non_substitution and not requested_subs and (not genes or i in windows):
            for low, original in index.non_substitution.items():
                literal = re.sub(r"^p\.", "", original)
                if re.search(r"(?<![A-Za-z0-9])(?:p\.)?" + re.escape(literal) + r"(?![A-Za-z0-9])", sentence, re.I):
                    key = (original, "literal", sentence.lower())
                    if key not in seen:
                        seen.add(key)
                        matches.append({
                            "mutation": original, "as_written": literal, "numbering": "as written",
                            "sentence": sentence, "in_reference": True, "gene": None,
                        })
        items = extract_substitutions(sentence)
        for cluster in _cluster_substitutions(items, sentence):
            owner = _attributed_gene(sentence, cluster, mentions) if genes else None
            if genes and owner is not None and not owner[3]:
                continue  # belongs to another gene (parC, gyrB, parE ...)
            for item in cluster:
                sub = item["sub"]
                target_genes = [owner[2]] if (owner and owner[2]) else genes
                if target_genes and not _mutation_position_plausible(sub, target_genes, orgs):
                    continue
                canon, numbering = index.lookup(sub) if index else (None, None)
                if requested_subs:
                    req_canon = None
                    if sub in requested_subs:
                        # Exact user notation is valid evidence even without a
                        # curated row; keep it as a literature-only ``other``.
                        numbering = "as written"
                    elif index:
                        c, n = index.lookup(sub)
                        if c and parse_substitution(c) in requested_subs:
                            req_canon, numbering = c, n
                    if req_canon:
                        canon = req_canon
                    elif sub not in requested_subs:
                        continue
                # Keep separate sentence occurrences: a mutation may first appear
                # in the title without phenotype wording and then be explicitly
                # linked to resistance in the abstract.
                key = (canon or format_substitution(sub), item["as_written"].lower(), sentence.lower())
                if key in seen:
                    continue
                seen.add(key)
                record = {
                    "mutation": canon or format_substitution(sub),
                    "as_written": item["as_written"],
                    "numbering": numbering or "as written",
                    "sentence": sentence,
                    "in_reference": bool(canon),
                    "gene": owner[2] if owner else (genes[0] if (genes and (len(genes) == 1 or canon)) else None),
                }
                (matches if canon else others).append(record)
    return matches, others


def _lower_list(values):
    return [str(v).strip() for v in values or [] if str(v or "").strip()]


def assess_paper(paper, plan):
    """Content-based assessment of one paper for a mutation-focused plan.

    Direct (mutation_aligned / exact) requires, all from the paper's OWN text:
      * the queried gene,
      * the queried drug (any accepted alias/abbreviation),
      * the queried organism established as the subject (never an incidental mention),
      * at least one mutation in gene context (reference mutation in any accepted
        notation, the explicitly requested mutation, or - when none was requested -
        any substitution written next to the gene).
    """
    entities = plan.get("entities", {}) or {}
    genes = _lower_list(entities.get("genes"))
    drugs = _lower_list(entities.get("antibiotics"))
    organisms = _lower_list(entities.get("organisms"))
    requested = _lower_list([*(entities.get("mutations") or []), *(entities.get("variants") or [])])
    reference = [*(plan.get("mutation_reference_mutations") or []), *(plan.get("mutation_reference_reported") or [])]

    title = str(paper.get("title") or "")
    abstract = str(paper.get("abstract") or "")
    core = f"{title} {abstract}"
    org = organism_assessment(paper, organisms)

    gene_ok = any(gene_hit(core, g) for g in genes) if genes else True
    drug_ok = any(drug_hits(core, d) for d in drugs) if drugs else True
    org_ok = org["status"] in ORGANISM_OK_STATUSES

    index = MutationIndex(reference + requested, genes, organisms)
    matches, others = find_mutations_in_text(abstract, index, genes, requested, title=title, organisms=organisms)
    full_text = str(paper.get("full_text") or "")
    if full_text and gene_ok and org_ok:
        # Full text may ADD mutation mentions, but only for a paper whose subject
        # organism/gene/drug were already established from title/abstract.
        ft_matches, ft_others = find_mutations_in_text(full_text, index, genes, requested, organisms=organisms)
        have = {m["mutation"] for m in matches}
        matches += [m for m in ft_matches if m["mutation"] not in have]
        others += ft_others
    if requested:
        others = []

    # For a gene-specific question, a mutation is direct evidence only when the
    # extractor attributes it to the queried gene. Unattributed substitutions
    # (including mutations explicitly attached to another protein) remain useful
    # context but cannot qualify a paper as direct mutation-level evidence.
    queried_genes = {str(g).strip().lower() for g in genes}
    candidate_mutations = matches + others
    mutation_ok = (
        any(str(m.get("gene") or "").strip().lower() in queried_genes for m in candidate_mutations)
        if genes else bool(candidate_mutations)
    )

    reasons = []
    if not gene_ok:
        reasons.append("gene_not_in_title_or_abstract")
    if not drug_ok:
        reasons.append("drug_not_in_title_or_abstract")
    if not org_ok:
        reasons.append(f"organism:{org['status']}")
    if not mutation_ok:
        reasons.append("no_mutation_in_gene_context")
    eligible = gene_ok and drug_ok and org_ok and mutation_ok

    if eligible:
        alignment, specificity = "mutation_aligned", "exact"
    elif not org_ok and org["status"] == "other_organism":
        alignment, specificity = ("entity_aligned" if (gene_ok or drug_ok) else "related"), "other_organism"
    elif not org_ok and org["status"] == "mixed_organism":
        alignment, specificity = "entity_aligned", "mixed_organism"
    elif not org_ok:
        alignment, specificity = ("entity_aligned" if (gene_ok or drug_ok) else "related"), "organism_not_stated"
    elif gene_ok and drug_ok:
        alignment, specificity = "entity_relationship_aligned", "exact"
    elif gene_ok or drug_ok:
        alignment, specificity = "entity_aligned", "partial"
    else:
        alignment, specificity = "related", "none"

    return {
        "eligible": eligible,
        "alignment": alignment,
        "specificity": specificity,
        "reasons": reasons,
        "gene_ok": gene_ok,
        "drug_ok": drug_ok,
        "organism": org,
        "reference_matches": matches,
        "other_substitutions": others,
    }
