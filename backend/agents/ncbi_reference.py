import csv
import os
import re
import unicodedata

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NCBI_PATH = os.path.join(BASE_DIR, "data", "NCBI_AMR.csv")

_NCBI_INDEX = None

EXPECTED_COLUMNS = [
    "#Allele",
    "Gene family",
    "Product name",
    "Scope",
    "Type",
    "Subtype",
    "Class",
    "Subclass",
    "RefSeq protein",
    "RefSeq nucleotide",
    "GenBank protein",
    "GenBank nucleotide",
    "Curated RefSeq start",
    "Links",
]


def normalize_name(value):
    """Normalize text for case/format-insensitive comparison."""
    if not value:
        return ""

    value = unicodedata.normalize("NFKC", str(value))
    value = value.strip().lower()

    return re.sub(r"[^a-z0-9']", "", value)


def _clean(value):
    if value is None:
        return ""
    return str(value).strip()


def _row_to_record(row):
    """
    Convert one NCBI AMR reference row to a stable application record.

    The CSV itself does not contain a dedicated synonym field. Therefore
    allele, gene family, and product name are kept separately rather than
    incorrectly calling them synonyms.
    """
    return {
        "allele": _clean(row.get("#Allele")),
        "gene_family": _clean(row.get("Gene family")),
        "product_name": _clean(row.get("Product name")),
        "scope": _clean(row.get("Scope")),
        "type": _clean(row.get("Type")),
        "subtype": _clean(row.get("Subtype")),
        "class": _clean(row.get("Class")),
        "subclass": _clean(row.get("Subclass")),
        "refseq_protein": _clean(row.get("RefSeq protein")),
        "refseq_nucleotide": _clean(row.get("RefSeq nucleotide")),
        "genbank_protein": _clean(row.get("GenBank protein")),
        "genbank_nucleotide": _clean(row.get("GenBank nucleotide")),
        "curated_refseq_start": _clean(row.get("Curated RefSeq start")),
        "links": _clean(row.get("Links")),
        "source": "NCBI_AMR_REFERENCE",
    }


def load_ncbi_reference(force_reload=False):
    """
    Load the NCBI AMR reference catalog into memory.

    The downloaded CSV is used locally, so candidate searches do not require
    an internet connection after the file has been placed in data/.
    """
    global _NCBI_INDEX

    if _NCBI_INDEX is not None and not force_reload:
        return _NCBI_INDEX

    if not os.path.exists(NCBI_PATH):
        raise FileNotFoundError(
            f"NCBI AMR reference file not found at: {NCBI_PATH}"
        )

    records = []

    with open(
        NCBI_PATH,
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        reader = csv.DictReader(file)

        for row in reader:
            record = _row_to_record(row)

            # Keep AMR records for this AMR-gene recognition module.
            # The catalog can also contain virulence entries.
            if record["type"].upper() != "AMR":
                continue

            if (
                not record["allele"]
                and not record["gene_family"]
                and not record["product_name"]
            ):
                continue

            records.append(record)

    _NCBI_INDEX = {
        "records": records,
        "stats": {
            "records_loaded": len(records),
            "source": "NCBI_AMR_REFERENCE",
            "path": NCBI_PATH,
        },
    }

    print(
        f"NCBI AMR reference loaded: "
        f"{len(records)} AMR records"
    )

    return _NCBI_INDEX


def _field_match_score(query_key, field_key):
    if not query_key or not field_key:
        return 0, None

    if query_key == field_key:
        return 100, "exact"

    query_without_bla = (
        query_key[3:]
        if query_key.startswith("bla") and len(query_key) > 3
        else query_key
    )
    field_without_bla = (
        field_key[3:]
        if field_key.startswith("bla") and len(field_key) > 3
        else field_key
    )

    if query_without_bla == field_without_bla:
        return 96, "bla_prefix_normalized"

    if (
        len(query_without_bla) >= 3
        and len(field_without_bla) >= 3
        and (
            (len(field_without_bla) > len(query_without_bla) and field_without_bla.startswith(query_without_bla))
            or (len(query_without_bla) > len(field_without_bla) and query_without_bla.startswith(field_without_bla))
        )
    ):
        return 88, "family_or_prefix_related"

    if (
        len(query_without_bla) >= 4
        and len(field_without_bla) >= 4
        and (
            (len(field_without_bla) > len(query_without_bla) and query_without_bla in field_without_bla)
            or (len(query_without_bla) > len(field_without_bla) and field_without_bla in query_without_bla)
        )
    ):
        return 76, "name_related"

    if len(query_without_bla) >= 5:
        common = len(
            set(query_without_bla) & set(field_without_bla)
        )
        denom = max(
            len(set(query_without_bla)),
            len(set(field_without_bla)),
        )
        overlap = common / denom if denom else 0

        if overlap >= 0.65:
            return 62, "similar_name_candidate"

    return 0, None


def _record_match(record, query_key):
    fields = [
        ("allele", record["allele"]),
        ("gene_family", record["gene_family"]),
        ("product_name", record["product_name"]),
    ]

    best_score = 0
    best_type = None
    best_field = None

    for field_name, field_value in fields:
        field_key = normalize_name(field_value)
        score, match_type = _field_match_score(
            query_key,
            field_key,
        )

        if score > best_score:
            best_score = score
            best_type = match_type
            best_field = field_name

    return best_score, best_type, best_field


def search_ncbi_candidates(gene_name, include_related=True):
    """
    Search the local NCBI AMR reference catalog.

    Returns exact and related AMR candidates. No artificial five-record limit
    is applied. The result is intended for downstream candidate
    presentation/disambiguation (A/B/C/...) rather than automatic identity
    confirmation.
    """
    if not gene_name or not str(gene_name).strip():
        return {
            "status": "not_found",
            "query": gene_name,
            "source": "NCBI_AMR_REFERENCE",
            "candidate_count": 0,
            "candidates": [],
        }

    index = load_ncbi_reference()
    query = str(gene_name).strip()
    query_key = normalize_name(query)

    candidates = []

    for record in index["records"]:
        score, match_type, matched_field = _record_match(
            record,
            query_key,
        )

        if score < 60:
            continue

        candidate = dict(record)
        candidate["query"] = query
        candidate["match_score"] = score
        candidate["match_type"] = match_type
        candidate["matched_field"] = matched_field

        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            -item["match_score"],
            item["gene_family"].lower(),
            item["allele"].lower(),
        )
    )

    # De-duplicate identical rows while preserving the strongest match.
    deduplicated = []
    seen = set()

    for candidate in candidates:
        identity = (
            candidate["allele"],
            candidate["gene_family"],
            candidate["class"],
            candidate["subclass"],
            candidate["refseq_nucleotide"],
            candidate["genbank_nucleotide"],
        )

        if identity in seen:
            continue

        seen.add(identity)
        deduplicated.append(candidate)

    candidates = deduplicated

    if not include_related:
        candidates = [
            candidate
            for candidate in candidates
            if candidate["match_type"] in {
                "exact",
                "bla_prefix_normalized",
            }
        ]

    for number, candidate in enumerate(candidates, start=1):
        candidate["candidate_number"] = number

    return {
        "status": "found" if candidates else "not_found",
        "query": query,
        "source": "NCBI_AMR_REFERENCE",
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def lookup_ncbi_exact(gene_name):
    """
    Convenience function for exact NCBI AMR matching.

    Unlike search_ncbi_candidates(), this returns only exact or
    bla-prefix-normalized matches.
    """
    result = search_ncbi_candidates(
        gene_name,
        include_related=False,
    )

    return result
