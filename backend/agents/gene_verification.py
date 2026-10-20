"""CARD + NCBI entity verification and disambiguation agent.

This module deliberately knows nothing about PubMed, Flask, or LangGraph.
It receives extracted mentions and returns reference-supported candidates.
"""

import re

from agents.card_reference import normalize_name, search_card_candidates
from agents.ncbi_reference import search_ncbi_candidates

CANDIDATE_TYPE_PRIORITY = {
    "canonical": 0,
    "exact": 0,
    "synonym": 1,
    "bla_prefix_normalized": 2,
    "family_exact": 3,
    "product_exact": 4,
    "family_or_prefix_related": 5,
    "name_related": 6,
    "similar_name_candidate": 7,
}

STRONG_MATCH_TYPES = {"canonical", "exact", "synonym", "bla_prefix_normalized"}


def _merge_key(canonical_name):
    key = normalize_name(canonical_name or "")
    if key.startswith("bla") and len(key) > 3:
        return key[3:]
    return key


def _normalize_ncbi_candidate(candidate):
    """Re-score NCBI matches without changing ncbi_reference.py.

    NCBI's CSV has a family column shared by many allele rows. A query that
    exactly matches that family must not receive the same identity confidence
    as an exact allele match.
    """
    item = dict(candidate)
    field = item.get("matched_field")
    raw_score = item.get("match_score", 0) or 0
    raw_type = item.get("match_type")

    if field == "gene_family" and raw_type == "exact":
        item["match_score"] = 75
        item["match_type"] = "family_exact"
    elif field == "product_name" and raw_type == "exact":
        item["match_score"] = 70
        item["match_type"] = "product_exact"
    else:
        item["match_score"] = raw_score
        item["match_type"] = raw_type

    return item


def _reference_candidates_for_mention(mention):
    options = []

    try:
        card_result = search_card_candidates(mention, include_related=True)
    except (FileNotFoundError, OSError, ValueError):
        card_result = {"candidates": []}

    query_key = normalize_name(mention)
    query_key_without_bla = query_key[3:] if query_key.startswith("bla") else query_key

    for candidate in card_result.get("candidates", []):
        canonical_key = normalize_name(candidate.get("canonical_name") or "")
        synonym_keys = {
            normalize_name(value) for value in candidate.get("synonyms", [])
        }
        if query_key == canonical_key or query_key_without_bla == canonical_key:
            card_match_type = "canonical"
        elif query_key in synonym_keys or query_key_without_bla in synonym_keys:
            card_match_type = "synonym"
        else:
            card_match_type = candidate.get("match_type")

        options.append({
            "source": "CARD",
            "match_status": "candidate",
            "match_type": card_match_type,
            "mention": mention,
            "canonical_name": candidate.get("canonical_name"),
            "aro_id": candidate.get("aro_id"),
            "synonyms": candidate.get("synonyms", []),
            "description": candidate.get("description"),
            "specificity": candidate.get("specificity"),
            "match_score": candidate.get("match_score", 0),
            "matched_field": "canonical/synonym",
            "candidate_record": candidate,
        })

    try:
        ncbi_result = search_ncbi_candidates(mention, include_related=True)
    except (FileNotFoundError, OSError, ValueError):
        ncbi_result = {"candidates": []}

    for raw in ncbi_result.get("candidates", []):
        candidate = _normalize_ncbi_candidate(raw)
        canonical_name = (
            candidate.get("allele")
            or candidate.get("gene_family")
            or candidate.get("product_name")
        )
        options.append({
            "source": "NCBI",
            "match_status": "candidate",
            "match_type": candidate.get("match_type"),
            "mention": mention,
            "canonical_name": canonical_name,
            "aro_id": None,
            "synonyms": [],
            "description": candidate.get("product_name"),
            "specificity": None,
            "match_score": candidate.get("match_score", 0),
            "matched_field": candidate.get("matched_field"),
            "candidate_record": candidate,
        })

    return options


def _merge_reference_candidates(options):
    merged = {}

    for option in options:
        canonical = option.get("canonical_name") or ""
        key = _merge_key(canonical)
        if not key:
            key = "__candidate__" + str(len(merged) + 1)

        existing_key = None
        if key in merged:
            existing_key = key
        else:
            for k in merged:
                if len(k) >= 4 and len(key) >= 4 and (k.endswith(key) or key.endswith(k)):
                    existing_key = k
                    break

        if existing_key is None:
            copied = dict(option)
            copied["sources"] = [option.get("source")]
            copied["source"] = option.get("source")
            merged[key] = copied
            continue

        existing = merged[existing_key]
        # Prefer the shorter canonical gene symbol (e.g. ompK35 over Klebsiella pneumoniae OmpK35)
        if len(canonical) < len(existing.get("canonical_name") or "") and len(canonical) >= 3:
            existing["canonical_name"] = canonical

        source = option.get("source")
        if source and source not in existing["sources"]:
            existing["sources"].append(source)
        existing["source"] = " + ".join(existing["sources"])

        # Prefer the stronger identity signal, not merely the larger raw
        # score. This prevents a family-level NCBI hit from overriding an
        # exact CARD allele/synonym match.
        existing_rank = CANDIDATE_TYPE_PRIORITY.get(existing.get("match_type"), 99)
        option_rank = CANDIDATE_TYPE_PRIORITY.get(option.get("match_type"), 99)
        if (option_rank, -(option.get("match_score") or 0)) < (
            existing_rank,
            -(existing.get("match_score") or 0),
        ):
            existing["match_score"] = option.get("match_score")
            existing["match_type"] = option.get("match_type")
            existing["matched_field"] = option.get("matched_field")

        if not existing.get("aro_id") and option.get("aro_id"):
            existing["aro_id"] = option.get("aro_id")
        if not existing.get("description") and option.get("description"):
            existing["description"] = option.get("description")
        if not existing.get("specificity") and option.get("specificity"):
            existing["specificity"] = option.get("specificity")

        synonyms = existing.setdefault("synonyms", [])
        for synonym in option.get("synonyms", []):
            if synonym not in synonyms:
                synonyms.append(synonym)

    return list(merged.values())


def _build_gene_candidate_groups(mentions):
    groups = []
    for mention in mentions:
        options = _merge_reference_candidates(
            _reference_candidates_for_mention(mention)
        )

        options.sort(key=lambda item: (
            CANDIDATE_TYPE_PRIORITY.get(item.get("match_type"), 99),
            -(item.get("match_score") or 0),
            (item.get("canonical_name") or "").lower(),
        ))

        for number, option in enumerate(options, start=1):
            option["candidate_number"] = number
            option["candidate_label"] = (
                chr(64 + number) if number <= 26 else f"C{number}"
            )

        groups.append({
            "mention": mention,
            "candidate_count": len(options),
            "candidates": options,
        })

    return groups


def _flatten_candidate_groups(groups):
    flattened = []
    for group_number, group in enumerate(groups, start=1):
        for candidate in group.get("candidates", []):
            item = dict(candidate)
            item["mention_group"] = group_number
            flattened.append(item)
    return flattened


def _auto_resolve_group(group):
    candidates = group.get("candidates", [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    top = candidates[0]
    top_type = top.get("match_type")

    if top_type not in STRONG_MATCH_TYPES:
        return None

    # A family-level query can legitimately map to several allele records.
    # Treat it as ambiguous even when one CARD synonym points to a specific
    # record, because the synonym may denote the family rather than that
    # particular allele. This is entirely data-driven.
    if top.get("matched_field") == "gene_family":
        return None
    if top_type == "synonym" and any(
        candidate.get("match_type") == "family_exact"
        for candidate in candidates[1:]
    ):
        return None

    top_rank = CANDIDATE_TYPE_PRIORITY.get(top_type, 99)
    for other in candidates[1:]:
        other_rank = CANDIDATE_TYPE_PRIORITY.get(other.get("match_type"), 99)
        if other_rank <= top_rank:
            return None

    return top


def _select_gene_candidate(candidate_groups, selection):
    if selection is None:
        return None
    selection_text = str(selection).strip()
    if not selection_text:
        return None

    match = re.fullmatch(r"(\d+)[\s-]*([A-Za-z]+)", selection_text)
    if match:
        group_number = int(match.group(1))
        label = match.group(2).upper()
        if 1 <= group_number <= len(candidate_groups):
            for candidate in candidate_groups[group_number - 1].get("candidates", []):
                if candidate.get("candidate_label", "").upper() == label:
                    selected = dict(candidate)
                    selected["mention_group"] = group_number
                    return selected
        return None

    if len(candidate_groups) > 1 and selection_text.isdigit():
        group_number = int(selection_text)
        if 1 <= group_number <= len(candidate_groups):
            candidates = candidate_groups[group_number - 1].get("candidates", [])
            if candidates:
                selected = dict(candidates[0])
                selected["mention_group"] = group_number
                return selected
        return None

    if len(candidate_groups) == 1:
        candidates = candidate_groups[0].get("candidates", [])
        if selection_text.isdigit():
            number = int(selection_text)
            for candidate in candidates:
                if candidate.get("candidate_number") == number:
                    selected = dict(candidate)
                    selected["mention_group"] = 1
                    return selected

        for candidate in candidates:
            if candidate.get("candidate_label", "").upper() == selection_text.upper():
                selected = dict(candidate)
                selected["mention_group"] = 1
                return selected

        selection_norm = normalize_name(selection_text)
        matches = [
            candidate for candidate in candidates
            if normalize_name(candidate.get("canonical_name") or "") == selection_norm
        ]
        if len(matches) == 1:
            selected = dict(matches[0])
            selected["mention_group"] = 1
            return selected

    return None


def _build_resolutions(candidate_groups, selected_candidate):
    resolutions = []
    for index, group in enumerate(candidate_groups, start=1):
        auto = _auto_resolve_group(group)
        if auto:
            chosen = dict(auto)
            chosen["mention_group"] = index
            resolutions.append({
                "mention_group": index,
                "mention": group.get("mention"),
                "status": "auto_resolved",
                "candidate_count": group.get("candidate_count", 0),
                "selected": chosen,
            })
        else:
            resolutions.append({
                "mention_group": index,
                "mention": group.get("mention"),
                "status": "ambiguous",
                "candidate_count": group.get("candidate_count", 0),
                "selected": None,
            })

    user_pick = _select_gene_candidate(candidate_groups, selected_candidate)
    if user_pick:
        target = user_pick.get("mention_group")
        if target and 1 <= target <= len(resolutions):
            resolutions[target - 1]["status"] = "user_selected"
            resolutions[target - 1]["selected"] = user_pick

    return resolutions


def verify_mentions(mentions, variant=None, selected_candidate=None):
    """Verify extracted mentions against CARD + NCBI and resolve ambiguity."""
    candidate_mentions = []
    seen = set()
    for mention in mentions or []:
        key = normalize_name(mention)
        if key and key not in seen:
            seen.add(key)
            candidate_mentions.append(mention)

    groups = [
        group for group in _build_gene_candidate_groups(candidate_mentions)
        if group.get("candidate_count", 0) > 0
    ]

    flattened = _flatten_candidate_groups(groups)
    resolutions = _build_resolutions(groups, selected_candidate)

    recognized = []
    for group in groups:
        top = (group.get("candidates") or [{}])[0]
        recognized.append({
            "mention": group.get("mention"),
            "canonical_name": top.get("canonical_name"),
            "aro_id": top.get("aro_id"),
            "match_type": top.get("match_type"),
            "synonyms": top.get("synonyms", []),
            "source": top.get("source"),
            "sources": top.get("sources", [top.get("source")]),
            "candidate_count": group.get("candidate_count", 0),
        })

    selected = _select_gene_candidate(groups, selected_candidate)
    return {
        "all_genes": recognized,
        "gene_candidate_count": len(flattened),
        "gene_candidate_groups": groups,
        "gene_candidates": flattened,
        "selected_candidate": selected,
        "selection_required": any(r["status"] == "ambiguous" for r in resolutions),
        "resolutions": resolutions,
    }
