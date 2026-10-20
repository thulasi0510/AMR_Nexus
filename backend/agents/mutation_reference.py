"""Specialized mutation reference agent.

This agent is intentionally narrow: it retrieves mutation candidates for the
requested gene + organism + antibiotic. CARD/NCBI establish AMR identity and
known determinant annotations; the WHO TB catalogue is used conditionally for
M. tuberculosis drug-resistance queries. It never converts "uncertain" into
"susceptible".
"""
from functools import lru_cache
import os
import re
from openpyxl import load_workbook

from config import NCBI_AMR_PATH, WHO_TB_PATH, WHO_TB_INDEX_PATH
from agents.entity_aliases import canonical_drug, canonical_organism, dedupe_by_key
from agents.mutation_matching import MutationIndex, parse_substitution

# User-facing display limit ONLY. The complete reference result is always kept
# in retrieve_mutations()["all_mutations"]; nothing downstream (literature
# filtering, evidence matching) may be restricted to the displayed subset.
MAX_DISPLAY = 20


AA3 = {
    "Ala":"A","Arg":"R","Asn":"N","Asp":"D","Cys":"C","Gln":"Q","Glu":"E",
    "Gly":"G","His":"H","Ile":"I","Leu":"L","Lys":"K","Met":"M","Phe":"F",
    "Pro":"P","Ser":"S","Thr":"T","Trp":"W","Tyr":"Y","Val":"V",
}
DRUG_ALIASES = {
    "rifampicin": {"rifampicin", "rifampin", "rifamycin"},
    "rifampin": {"rifampicin", "rifampin", "rifamycin"},
    "fluoroquinolone": {"fluoroquinolone", "fluoroquinolones", "quinolone", "quinolones"},
    "ciprofloxacin": {"ciprofloxacin", "fluoroquinolone"},
    "levofloxacin": {"levofloxacin", "fluoroquinolone"},
    "moxifloxacin": {"moxifloxacin", "fluoroquinolone"},
}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _drug_matches(query_drug, row_drug):
    q = _norm(query_drug)
    r = _norm(row_drug)
    if q == r or q in r:
        return True
    aliases = DRUG_ALIASES.get(str(query_drug).lower(), {str(query_drug).lower()})
    return any(_norm(a) == r or _norm(a) in r for a in aliases)


def _three_to_one(mutation):
    # p.Ser450Leu -> S450L; preserve complex indels/deletions as reported.
    m = re.fullmatch(r"p\.([A-Z][a-z]{2})(\d+)([A-Z][a-z]{2})", str(mutation or ""))
    if not m:
        return mutation
    return f"{AA3.get(m.group(1), m.group(1))}{m.group(2)}{AA3.get(m.group(3), m.group(3))}"


@lru_cache(maxsize=1)
def _ncbi_rows():
    import csv
    rows = []
    if not os.path.exists(NCBI_AMR_PATH):
        return rows
    with open(NCBI_AMR_PATH, newline="", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.DictReader(fh):
            rows.append(row)
    return rows


@lru_cache(maxsize=1)
def _who_rows():
    # Fast derived index generated from the supplied WHO workbook. The Excel
    # workbook remains the authoritative bundled source; this JSON only avoids
    # parsing 48k rows on every request.
    if os.path.exists(WHO_TB_INDEX_PATH):
        try:
            import json
            with open(WHO_TB_INDEX_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError, TypeError):
            pass
    if not os.path.exists(WHO_TB_PATH):
        return []
    wb = load_workbook(WHO_TB_PATH, read_only=True, data_only=True)
    ws = wb["Catalogue_master_file"]
    iterator = ws.iter_rows(values_only=True)
    next(iterator, None)
    next(iterator, None)
    headers = list(next(iterator, None) or [])
    index = {str(h).strip(): i for i, h in enumerate(headers) if h is not None}
    needed = ["drug","gene","mutation","variant","tier","effect","FINAL CONFIDENCE GRADING",
              "Present_R","Present_S","Absent_R","Absent_S"]
    out = []
    for row in iterator:
        rec = {k: row[index[k]] if k in index and index[k] < len(row) else None for k in needed}
        if rec.get("gene"):
            out.append(rec)
    wb.close()
    return out


def _who_grade(value):
    text = str(value or "").lower().strip()
    # Check the negative form first: "not assoc w r" contains the shorter
    # substring "assoc w r" and would otherwise be mislabeled as resistance.
    if "not assoc w r - interim" in text:
        return "not associated with resistance (interim)"
    if "not assoc w r" in text:
        return "not associated with resistance"
    if "assoc w r - interim" in text:
        return "resistance-associated (interim)"
    if "assoc w r" in text:
        return "resistance-associated"
    if "uncertain" in text:
        return "uncertain significance"
    return str(value or "").strip() or "not reported"


def retrieve_mutations(understanding):
    entities = understanding.get("entities", {})
    understanding = dict(understanding)
    understanding["query"] = understanding.get("query") or understanding.get("raw_query") or ""
    genes = entities.get("genes") or []
    organisms = entities.get("organisms") or []
    antibiotics = entities.get("antibiotics") or []
    if not genes:
        return {"status":"not_applicable","mutations":[],"sources":[]}

    # Resolve canonical organism when entity normalization has supplied it.
    resolutions = understanding.get("organism_resolutions") or []
    canonical_orgs = [x.get("canonical_name") for x in resolutions if x.get("canonical_name")]
    org_text = " ".join(
        [canonical_organism(o) for o in [*organisms, *canonical_orgs]]
    ).lower()
    gene_text = {str(g).lower() for g in genes}
    # rifampin/rifampicin etc. are one canonical drug (see entity_aliases).
    drug_text = [str(x).lower() for x in dedupe_by_key(antibiotics, canonical_drug)]

    records = []
    for row in _ncbi_rows():
        gene = str(row.get("Gene family") or "").lower()
        if gene not in gene_text:
            continue
        if antibiotics:
            classes = " ".join([str(row.get("Class") or ""), str(row.get("Subclass") or "")]).lower()
            if not any(_drug_matches(d, classes) or _norm(d) in _norm(classes) for d in drug_text):
                continue
        allele = str(row.get("#Allele") or "")
        mutation = allele.split("_", 1)[1] if "_" in allele else allele
        records.append({
            "mutation": mutation,
            "phenotype": "resistance-associated AMR determinant",
            "confidence": "NCBI AMRFinderPlus annotation",
            "source": "NCBI AMRFinderPlus reference",
            "source_url": "https://www.ncbi.nlm.nih.gov/pathogens/antimicrobial-resistance/AMRFinder/",
            "gene": row.get("Gene family"),
            "drug_class": row.get("Class"),
            "drug": row.get("Subclass"),
            "scope": row.get("Scope"),
            "organism_specific": False,
            "scope_note": "NCBI AMRFinderPlus reference record does not provide organism-specific phenotype attribution in this bundled table.",
        })

    # WHO is a specialist catalogue for Mycobacterium tuberculosis resistance queries.
    is_tb = "mycobacterium tuberculosis" in org_text or "tb" in org_text or "mtb" in org_text
    if is_tb:
        target_tb_drugs = [canonical_drug(d).lower() for d in drug_text] if drug_text else []
        for row in _who_rows():
            row_gene = str(row.get("gene") or "").lower()
            if gene_text and row_gene not in gene_text:
                continue
            if target_tb_drugs:
                row_drug = str(row.get("drug") or "").lower()
                if not any(_drug_matches(d, row_drug) or _norm(d) in _norm(row_drug) for d in target_tb_drugs):
                    continue
            raw_grade = str(row.get("FINAL CONFIDENCE GRADING") or "").strip()
            q_text = " ".join([str(x) for x in (understanding.get("query") or "").split()]).lower()
            # The normal mutation query asks for resistance-associated variants.
            # Do not dump WHO grade 3/4/5 catalogue entries into that answer.
            # Only include non-resistance grades when the user explicitly asks
            # about susceptibility/non-association (or asks whether a named
            # mutation is resistant versus susceptible).
            asks_susceptibility = any(w in q_text for w in (
                "susceptible", "susceptibility", "not associated with resistance",
                "not associated", "non-resistant",
            ))
            asks_resistance = any(w in q_text for w in (
                "resistance", "resistant", "resistance-associated", "associated with resistance",
            ))
            asks_both = asks_susceptibility and asks_resistance
            include = False
            if asks_both:
                include = bool(re.match(r"^(?:1\)\s*Assoc w R|2\)\s*Assoc w R - Interim|4\)\s*Not assoc w R - Interim|5\)\s*Not assoc w R)$", raw_grade, re.I))
            elif asks_susceptibility and not asks_resistance:
                include = bool(re.match(r"^(?:4\)\s*Not assoc w R - Interim|5\)\s*Not assoc w R)$", raw_grade, re.I))
            else:
                include = bool(re.match(r"^(?:1\)\s*Assoc w R|2\)\s*Assoc w R - Interim)$", raw_grade, re.I))

            # For mutation-focused output, prefer protein-level variants.
            # Synonymous/upstream c.-notation is not a useful default answer
            # to "which mutations cause/are associated with resistance".
            reported = str(row.get("mutation") or "")
            protein_level = reported.startswith("p.")
            if not protein_level:
                continue
            if include:
                reported = row.get("mutation")
                records.append({
                    "mutation": _three_to_one(reported),
                    "reported_mutation": reported,
                    "reported_variant": row.get("variant"),
                    "phenotype": _who_grade(raw_grade),
                    "phenotype_detail": (
                        f"WHO grading: {raw_grade}; Present_R={row.get('Present_R')}, "
                        f"Present_S={row.get('Present_S')}"
                    ),
                    "confidence": raw_grade,
                    "source": "WHO TB Mutation Catalogue 2023",
                    "source_url": "https://www.who.int/publications/i/item/9789240082410",
                    "gene": row.get("gene"),
                    "drug": row.get("drug"),
                    "tier": row.get("tier"),
                    "effect": row.get("effect"),
                    "organism_specific": True,
                    "scope_note": "Organism-specific WHO TB catalogue record.",
                    "present_resistant": row.get("Present_R"),
                    "present_susceptible": row.get("Present_S"),
                    "absent_resistant": row.get("Absent_R"),
                    "absent_susceptible": row.get("Absent_S"),
                })

    # If the user named a specific mutation, restrict the specialized source
    # to that exact variant rather than returning the whole gene catalogue.
    requested_values = [*(entities.get("mutations") or []), *(entities.get("variants") or [])]
    requested = {str(x).upper().replace(" ", "") for x in requested_values}
    requested_subs = {parse_substitution(x) for x in requested_values}
    requested_subs.discard(None)
    if requested:
        def _canon(v):
            return str(v or "").upper().replace("P.", "").replace(" ", "")
        if requested_subs:
            ref_variants = [
                v for r in records for v in (r.get("mutation"), r.get("reported_mutation")) if v
            ]
            notation_index = MutationIndex(ref_variants, genes, organisms)
            matched_canon = set(requested)
            for sub in requested_subs:
                canon, numbering = notation_index.lookup(sub)
                if canon:
                    matched_canon.add(_canon(canon))
            records = [
                r for r in records
                if _canon(r.get("mutation")) in matched_canon
                or _canon(r.get("reported_mutation")) in matched_canon
            ]
            for record in records:
                native = parse_substitution(record.get("mutation") or record.get("reported_mutation"))
                for sub in requested_subs:
                    canon, numbering = notation_index.lookup(sub)
                    if canon and native == parse_substitution(canon) and sub != native:
                        record["query_notation"] = f"{sub[0]}{sub[1]}{sub[2]}"
                        record["query_notation_scheme"] = numbering
        else:
            records = [r for r in records if _canon(r.get("mutation")) in requested or _canon(r.get("reported_mutation")) in requested]

    # Resistance-associated first, uncertain last.
    def _rank(r):
        return (
            0 if str(r.get("phenotype", "")).lower() == "resistance-associated" else
            1 if "resistance-associated (interim)" in str(r.get("phenotype", "")).lower() else 2,
            -(float(r.get("present_resistant") or 0) if str(r.get("present_resistant") or "").replace(".", "", 1).isdigit() else 0),
            str(r.get("mutation", "")),
        )
    records.sort(key=_rank)

    # De-duplicate by mutation + source. If one source lists the same mutation
    # with different classifications, the strongest-ranked record is kept and
    # the others are preserved under "other_classifications" -- a source
    # classification is never silently dropped or rewritten.
    merged = {}
    duplicates_merged = 0
    for rec in records:
        key = (str(rec.get("mutation") or "").upper(), rec.get("source"))
        if key not in merged:
            merged[key] = rec
            continue
        duplicates_merged += 1
        kept = merged[key]
        if rec.get("phenotype") != kept.get("phenotype"):
            kept.setdefault("other_classifications", []).append(
                {"phenotype": rec.get("phenotype"), "confidence": rec.get("confidence")}
            )
    unique = list(merged.values())

    # The user-facing mutation list is capped; the complete, ranked reference
    # result is retained in "all_mutations" for literature matching.
    displayed = unique[:MAX_DISPLAY]
    truncated = max(0, len(unique) - MAX_DISPLAY)
    by_source = {}
    for r in unique:
        by_source[r.get("source")] = by_source.get(r.get("source"), 0) + 1
    return {
        "status": "found" if unique else "no_reference_mutations",
        "mutations": displayed,
        "all_mutations": unique,
        "display_limit": MAX_DISPLAY,
        "sources": sorted(set(r.get("source") for r in unique if r.get("source"))),
        "total_matching_records": len(unique),
        "truncated_records": truncated,
        "records_by_source": by_source,
        "duplicate_records_merged": duplicates_merged,
    }
