import os
import re
import unicodedata

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OBO_PATH = os.path.join(BASE_DIR, "data", "CARD_AMR.obo")

_CARD_INDEX = None

GENE_PROPERTY_TAGS = {
    "classified_as_amr_gene_family",
    "classified_as_efflux_component",
    "classified_as_efflux_regulator",
}

NON_GENE_PROPERTY_TAGS = {
    "classified_as_antibiotic",
    "classified_as_antibiotic_plus_adjuvant",
    "classified_as_adjuvant",
    "classified_as_drug_class",
    "classified_as_drug_subclass",
    "classified_as_resistance_mechanism",
}

MAX_ANCESTOR_DEPTH = 15
EXACT_SYNONYM_SCOPES = {"EXACT"}

_PRIME_LIKE_CHARS = (
    "\u2032", "\u2033", "\u2034", "\u02b9", "\u02bc",
    "\u2018", "\u2019", "\u0060", "\u00b4",
)
_PRIME_TRANSLATION = {ord(c): ord("'") for c in _PRIME_LIKE_CHARS}


def normalize_name(value):
    """Normalize a gene identifier for safe comparison."""
    if not value:
        return ""

    value = unicodedata.normalize("NFKC", str(value))
    value = value.translate(_PRIME_TRANSLATION)
    value = value.strip().lower()

    # Formatting characters are removed, but apostrophe/prime characters
    # are retained because CARD uses them to distinguish loci.
    return re.sub(r"[^a-z0-9']", "", value)


def _parse_synonym(line):
    match = re.search(r'synonym:\s*"([^"]+)"\s*([A-Z]+)?', line)
    if not match:
        return None, None
    return match.group(1).strip(), match.group(2)


def _parse_is_a_id(line):
    match = re.match(r"\s*(ARO:\d+)", line)
    return match.group(1) if match else None


def _property_tags(term):
    tags = set()
    for prop in term.get("properties", []):
        tag = prop.split()[0] if prop else None
        if tag:
            tags.add(tag)
    return tags


def _infer_category(term, id_index):
    visited_ids = set()
    frontier = [term]
    depth = 0

    while frontier and depth <= MAX_ANCESTOR_DEPTH:
        level_tags = set()
        next_frontier = []

        for node in frontier:
            node_id = node.get("accession")
            if node_id in visited_ids:
                continue

            visited_ids.add(node_id)
            level_tags |= _property_tags(node)

            for is_a_line in node.get("is_a", []):
                parent_id = _parse_is_a_id(is_a_line)
                if parent_id and parent_id in id_index:
                    next_frontier.append(id_index[parent_id])

        if level_tags & NON_GENE_PROPERTY_TAGS:
            return "non_gene"
        if level_tags & GENE_PROPERTY_TAGS:
            return "gene"

        frontier = next_frontier
        depth += 1

    return "unknown"


def _add_to_index(index, key, record, collisions, key_kind):
    if not key:
        return

    existing = index.get(key)

    if existing is None:
        index[key] = {"status": "single", "record": record}
        return

    if existing["status"] == "single":
        if existing["record"]["aro_id"] == record["aro_id"]:
            return

        records = [existing["record"], record]
        index[key] = {"status": "collision", "records": records}
        collisions.append({
            "key": key,
            "key_kind": key_kind,
            "aro_ids": [r["aro_id"] for r in records],
        })
        return

    existing_ids = {r["aro_id"] for r in existing["records"]}
    if record["aro_id"] not in existing_ids:
        existing["records"].append(record)

        for collision in collisions:
            if (
                collision["key"] == key
                and collision["key_kind"] == key_kind
            ):
                collision["aro_ids"].append(record["aro_id"])
                break


def _store_term(
    term,
    id_index,
    child_counts,
    canonical_lookup,
    synonym_lookup,
    description_lookup,
    records_by_aro,
    collisions,
    stats,
):
    accession = term.get("accession")
    name = term.get("name")

    if not accession or not accession.startswith("ARO:"):
        stats["missing_accession"] += 1
        return

    if not name or not name.strip():
        stats["missing_name"] += 1
        return

    canonical_key = normalize_name(name)
    if not canonical_key:
        stats["empty_or_invalid_name"] += 1
        return

    seen_syn = set()
    valid_synonyms = []

    for text, scope in term.get("synonyms", []):
        if not text or not text.strip():
            stats["malformed_synonyms"] += 1
            continue

        dedup_key = (text, scope)
        if dedup_key in seen_syn:
            continue

        seen_syn.add(dedup_key)
        valid_synonyms.append({"text": text, "scope": scope})

    has_children = child_counts.get(accession, 0) > 0

    record = {
        "aro_id": accession,
        "canonical_name": name,
        "synonyms": valid_synonyms,
        "description": term.get("description"),
        "is_a": term.get("is_a", []),
        "relationships": term.get("relationships", []),
        "properties": term.get("properties", []),
        "category": _infer_category(term, id_index),
        "specificity": "family" if has_children else "specific",
    }

    records_by_aro[accession] = record
    stats["records_indexed"] += 1

    _add_to_index(
        canonical_lookup,
        canonical_key,
        record,
        collisions,
        "canonical",
    )

    for synonym in valid_synonyms:
        scope = synonym["scope"]

        if scope is not None and scope not in EXACT_SYNONYM_SCOPES:
            stats["non_exact_synonyms_skipped"] += 1
            continue

        synonym_key = normalize_name(synonym["text"])
        _add_to_index(
            synonym_lookup,
            synonym_key,
            record,
            collisions,
            "synonym",
        )

    description = record.get("description")
    if description:
        description_key = normalize_name(description)
        if description_key:
            description_lookup[description_key] = record


def load_card_reference(force_reload=False):
    """
    Load and index CARD.

    Exact lookup is preserved for compatibility. The returned index also
    contains records_by_aro, which is used by candidate generation.
    """
    global _CARD_INDEX

    if _CARD_INDEX is not None and not force_reload:
        return _CARD_INDEX

    if not os.path.exists(OBO_PATH):
        raise FileNotFoundError(
            f"CARD ontology not found at: {OBO_PATH}"
        )

    raw_terms = []
    current = None

    with open(OBO_PATH, "r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()

            if line == "[Term]":
                if current:
                    raw_terms.append(current)

                current = {
                    "accession": None,
                    "name": None,
                    "description": None,
                    "synonyms": [],
                    "is_a": [],
                    "relationships": [],
                    "properties": [],
                }
                continue

            if line.startswith("[") and line.endswith("]"):
                if current:
                    raw_terms.append(current)
                current = None
                continue

            if not current:
                continue

            if line.startswith("id: "):
                current["accession"] = line[4:].strip()

            elif line.startswith("name: "):
                current["name"] = line[6:].strip()

            elif line.startswith("def: "):
                current["description"] = line[5:].strip()

            elif line.startswith("synonym: "):
                text, scope = _parse_synonym(line)
                if text:
                    current["synonyms"].append((text, scope))

            elif line.startswith("is_a: "):
                current["is_a"].append(line[6:].strip())

            elif line.startswith("relationship: "):
                current["relationships"].append(
                    line[len("relationship: "):].strip()
                )

            elif line.startswith("property_value: "):
                current["properties"].append(
                    line[len("property_value: "):].strip()
                )

        if current:
            raw_terms.append(current)

    id_index = {
        term["accession"]: term
        for term in raw_terms
        if term.get("accession")
    }

    child_counts = {}
    for term in raw_terms:
        for is_a_line in term.get("is_a", []):
            parent_id = _parse_is_a_id(is_a_line)
            if parent_id:
                child_counts[parent_id] = (
                    child_counts.get(parent_id, 0) + 1
                )

    canonical_lookup = {}
    synonym_lookup = {}
    description_lookup = {}
    records_by_aro = {}
    collisions = []

    stats = {
        "total_terms_parsed": len(raw_terms),
        "records_indexed": 0,
        "missing_accession": 0,
        "missing_name": 0,
        "empty_or_invalid_name": 0,
        "malformed_synonyms": 0,
        "non_exact_synonyms_skipped": 0,
    }

    for term in raw_terms:
        _store_term(
            term,
            id_index,
            child_counts,
            canonical_lookup,
            synonym_lookup,
            description_lookup,
            records_by_aro,
            collisions,
            stats,
        )

    stats["canonical_keys"] = len(canonical_lookup)
    stats["synonym_keys"] = len(synonym_lookup)
    stats["collisions"] = len(collisions)

    category_counts = {"gene": 0, "non_gene": 0, "unknown": 0}
    for record in records_by_aro.values():
        category_counts[record["category"]] = (
            category_counts.get(record["category"], 0) + 1
        )

    stats["category_counts"] = category_counts

    _CARD_INDEX = {
        "canonical_lookup": canonical_lookup,
        "synonym_lookup": synonym_lookup,
        "description_lookup": description_lookup,
        "records_by_aro": records_by_aro,
        "collisions": collisions,
        "stats": stats,
    }

    print(
        f"CARD reference loaded: "
        f"{stats['records_indexed']} records, "
        f"{len(canonical_lookup)} canonical keys, "
        f"{len(synonym_lookup)} synonym keys, "
        f"{len(collisions)} collisions"
    )

    return _CARD_INDEX


def _empty_result(candidate, status="not_found", **extra):
    result = {
        "status": status,
        "match_type": None,
        "candidate": candidate,
        "canonical_name": None,
        "aro_id": None,
        "synonyms": [],
    }
    result.update(extra)
    return result


def _record_to_result(record, candidate, match_type, score=None):
    result = {
        "status": "validated",
        "match_type": match_type,
        "candidate": candidate,
        "canonical_name": record["canonical_name"],
        "aro_id": record["aro_id"],
        "synonyms": [s["text"] for s in record["synonyms"]],
        "description": record["description"],
        "specificity": record["specificity"],
        "source": "CARD",
    }

    if score is not None:
        result["match_score"] = score

    return result


def _match_result_to_response(match, candidate, match_type):
    if match["status"] == "collision":
        matches = [
            {
                "aro_id": r["aro_id"],
                "canonical_name": r["canonical_name"],
                "category": r["category"],
                "specificity": r["specificity"],
                "description": r["description"],
                "synonyms": [s["text"] for s in r["synonyms"]],
            }
            for r in match["records"]
            if r.get("category") == "gene"
        ]

        if not matches:
            return _empty_result(candidate)

        return {
            "status": "ambiguous",
            "match_type": match_type,
            "candidate": candidate,
            "canonical_name": None,
            "aro_id": None,
            "synonyms": [],
            "matches": matches,
        }

    record = match["record"]

    if record.get("category") != "gene":
        return _empty_result(
            candidate,
            rejected_reason=(
                "matched_non_gene_card_term"
                if record.get("category") == "non_gene"
                else "matched_uncategorized_card_term"
            ),
        )

    return _record_to_result(record, candidate, match_type)


def lookup_card(gene_name):
    """
    Exact CARD lookup.

    Search order:
      1. canonical name
      2. exact synonym

    The original exact-match behaviour is retained.
    """
    if not gene_name or not gene_name.strip():
        return _empty_result(None)

    index = load_card_reference()
    candidate = gene_name.strip()
    key = normalize_name(candidate)

    if not key:
        return _empty_result(candidate)

    lookup_keys = [key]

    if key.startswith("bla") and len(key) > 3:
        lookup_keys.append(key[3:])

    for lookup_key in lookup_keys:
        match = index["canonical_lookup"].get(lookup_key)
        if match:
            return _match_result_to_response(
                match,
                candidate,
                "canonical",
            )

    for lookup_key in lookup_keys:
        match = index["synonym_lookup"].get(lookup_key)
        if match:
            return _match_result_to_response(
                match,
                candidate,
                "synonym",
            )

    return _empty_result(candidate)


def _candidate_score(query_key, record):
    """
    Rank CARD records without treating fuzzy similarity as proof of identity.

    Exact matches are handled separately. Here we generate *candidates* only;
    the user can disambiguate before downstream evidence retrieval.
    """
    names = [record["canonical_name"]]
    names.extend(s["text"] for s in record.get("synonyms", []))

    best_score = 0
    best_type = "related"

    query_without_bla = (
        query_key[3:]
        if query_key.startswith("bla") and len(query_key) > 3
        else query_key
    )

    for name in names:
        key = normalize_name(name)

        if not key:
            continue

        if query_key == key:
            return 100, "exact"

        key_without_bla = (
            key[3:]
            if key.startswith("bla") and len(key) > 3
            else key
        )

        if query_without_bla == key_without_bla:
            best_score = max(best_score, 96)
            best_type = "bla_prefix_normalized"

        # Check individual tokens in the name (e.g. "OmpK35" in "Klebsiella pneumoniae OmpK35")
        tokens = re.split(r"[\s_\-./()]+", str(name or ""))
        for token in tokens:
            t_key = normalize_name(token)
            if not t_key:
                continue
            t_without_bla = t_key[3:] if t_key.startswith("bla") and len(t_key) > 3 else t_key
            if t_without_bla == query_without_bla:
                best_score = max(best_score, 94)
                best_type = "token_exact"
            elif len(query_without_bla) >= 3 and len(t_without_bla) > len(query_without_bla) and t_without_bla.startswith(query_without_bla):
                best_score = max(best_score, 88)
                best_type = "family_or_prefix_related"

        if (
            len(query_without_bla) >= 3
            and len(key_without_bla) > len(query_without_bla)
            and key_without_bla.startswith(query_without_bla)
        ):
            best_score = max(best_score, 88)
            best_type = "family_or_prefix_related"

        elif (
            len(query_without_bla) >= 4
            and len(key_without_bla) > len(query_without_bla)
            and query_without_bla in key_without_bla
        ):
            best_score = max(best_score, 76)
            best_type = "name_related"

        elif len(query_without_bla) >= 4:
            # Character overlap is used only as a low-priority candidate
            # signal, never as validation.
            common = len(set(query_without_bla) & set(key_without_bla))
            denom = max(len(set(query_without_bla)), len(set(key_without_bla)))
            overlap = common / denom if denom else 0

            if overlap >= 0.75 and abs(len(query_without_bla) - len(key_without_bla)) <= 2:
                best_score = max(best_score, 62)
                best_type = "similar_name_candidate"

    return best_score, best_type


def search_card_candidates(gene_name, include_related=True):
    """
    Return exact, synonym, and related CARD gene candidates.

    Unlike lookup_card(), this function does NOT stop when an exact record
    is absent. It returns a ranked candidate list so the calling workflow
    can present A/B/C choices to the user.

    Important:
      - Candidates are not automatically treated as confirmed identity.
      - Only CARD records classified as gene determinants are returned.
      - There is no artificial 5-result restriction.
    """
    if not gene_name or not str(gene_name).strip():
        return {
            "status": "not_found",
            "query": gene_name,
            "source": "CARD",
            "candidates": [],
        }

    index = load_card_reference()
    query = str(gene_name).strip()
    query_key = normalize_name(query)

    candidates = {}

    # First collect exact canonical/synonym matches.
    lookup_keys = [query_key]
    if query_key.startswith("bla") and len(query_key) > 3:
        lookup_keys.append(query_key[3:])

    for key in lookup_keys:
        match = index["canonical_lookup"].get(key)

        if match:
            records = (
                match["records"]
                if match["status"] == "collision"
                else [match["record"]]
            )
            for record in records:
                if record.get("category") == "gene":
                    candidates[record["aro_id"]] = _record_to_result(
                        record,
                        query,
                        "canonical",
                        100,
                    )

        match = index["synonym_lookup"].get(key)

        if match:
            records = (
                match["records"]
                if match["status"] == "collision"
                else [match["record"]]
            )
            for record in records:
                if record.get("category") == "gene":
                    current = candidates.get(record["aro_id"])
                    synonym_score = 98
                    if current is None or current["match_score"] < synonym_score:
                        candidates[record["aro_id"]] = _record_to_result(
                            record,
                            query,
                            "synonym",
                            synonym_score,
                        )

    if include_related:
        for record in index["records_by_aro"].values():
            if record.get("category") != "gene":
                continue

            score, match_type = _candidate_score(query_key, record)

            if score < 60:
                continue

            existing = candidates.get(record["aro_id"])

            if existing is None or score > existing.get("match_score", 0):
                candidates[record["aro_id"]] = _record_to_result(
                    record,
                    query,
                    match_type,
                    score,
                )

    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            -item.get("match_score", 0),
            item.get("canonical_name", "").lower(),
            item.get("aro_id", ""),
        ),
    )

    for number, candidate in enumerate(ordered, start=1):
        candidate["candidate_number"] = number

    return {
        "status": "found" if ordered else "not_found",
        "query": query,
        "source": "CARD",
        "candidate_count": len(ordered),
        "candidates": ordered,
    }


def lookup_card_by_aro(aro_id):
    """Direct CARD accession lookup."""
    index = load_card_reference()
    return index["records_by_aro"].get(aro_id)
