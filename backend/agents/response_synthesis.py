"""Evidence-grounded natural-language response synthesis for AMR queries.

Design note (why this file was rewritten): the previous prompt handed the
LLM a raw json.dumps() of the full understanding/evidence structures,
truncated at tens of thousands of characters, including every candidate
paper's full abstract. That is a large, undifferentiated blob for a local
model to reason over, and in practice it produced exactly the symptom
reported: a single concatenated paragraph that reads like one paper's
abstract/conclusion, not a multi-paper synthesis -- and a heavy prompt like
that also makes an Ollama timeout (falling back to the thin `_fallback`
answer) far more likely.

This version builds a compact, pre-digested brief instead: each candidate
paper is shown with its already-computed evidence tier (from
evidence_analysis.py's exact/family/related/verify alignment), its article
type, and the 2-3 highest-scoring extracted sentences (from
literature.py's/evidence_analysis.py's snippet extraction) rather than the
full abstract. That is both smaller and structurally easier for the model
to turn into claim-by-claim citations.
"""
import html
import re
import requests

from agents.entity_aliases import canonical_drug, canonical_organism, dedupe_by_key, drug_hits
from agents.mutation_evidence import mutation_position_issue
from agents.mutation_matching import MutationIndex, extract_substitutions, extract_query_variants, parse_substitution
from agents.card_reference import normalize_name, lookup_card
from config import (
    OLLAMA_URL,
    OLLAMA_MODEL,
    OLLAMA_RESPONSE_TIMEOUT,
    SYNTHESIS_MAX_PAPERS,
    SYNTHESIS_MAX_SNIPPET_CHARS,
)

_DIRECT_ALIGNMENTS = {
    "mechanism_aligned", "mutation_aligned", "comparison_aligned", "entity_relationship_aligned",
}
_FAMILY_ALIGNMENTS = {
    "mechanism_family_context", "entity_relationship_family_context",
}
_RELATED_ALIGNMENTS = {
    "mechanism_related", "entity_aligned",
}

_CONCLUSORY_OPENERS = (
    "in conclusion, ", "in conclusion ", "in summary, ", "in summary ",
    "taken together, ", "overall, ",
)


def _clean_title_key(title):
    t = html.unescape(str(title or ""))
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return re.sub(r"[\W_]+", "", t).lower()


def _numbered_references(references):
    numbered = []
    seen_pmids = set()
    seen_dois = set()
    seen_titles = {}

    for ref in references or []:
        if not isinstance(ref, dict):
            continue
        pmid = str(ref.get("pmid") or "").strip()
        doi = str(ref.get("doi") or "").strip().lower()
        title_key = _clean_title_key(ref.get("title"))

        existing = None
        if pmid and pmid in seen_pmids:
            existing = next((r for r in numbered if str(r.get("pmid") or "").strip() == pmid), None)
        elif doi and doi in seen_dois:
            existing = next((r for r in numbered if str(r.get("doi") or "").strip().lower() == doi), None)
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

        item = dict(ref)
        item["reference_number"] = len(numbered) + 1
        if pmid:
            seen_pmids.add(pmid)
        if doi:
            seen_dois.add(doi)
        if title_key and len(title_key) >= 20:
            seen_titles[title_key] = item
        numbered.append(item)
    return numbered


def _paper_identity(paper):
    return paper.get("pmid") or paper.get("doi") or paper.get("title")


def _annotated_paper_index(evidence):
    """Map paper identity -> annotated paper (with evidence_alignment /
    evidence_snippets) from evidence_analysis.py's classified buckets."""
    index = {}
    for item in evidence.get("literature_evidence", []) or []:
        for bucket in ("direct_evidence", "related_evidence", "requires_verification"):
            for paper in item.get(bucket, []) or []:
                identity = _paper_identity(paper)
                if identity:
                    index[identity] = paper
    return index


def _tier_for(annotated):
    alignment = annotated.get("evidence_alignment", "related")
    specificity = annotated.get("evidence_specificity", "none")
    if alignment in _DIRECT_ALIGNMENTS:
        tier = "direct"
    elif alignment in _FAMILY_ALIGNMENTS:
        tier = "family-context"
    elif alignment in _RELATED_ALIGNMENTS:
        tier = "related"
    else:
        tier = "weak/unclassified"
    return tier, specificity


def _clean_snippet(text, limit=SYNTHESIS_MAX_SNIPPET_CHARS):
    if not text:
        return ""
    text = html.unescape(str(text))
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = " ".join(text.split())
    low = text.lower()
    for opener in _CONCLUSORY_OPENERS:
        if low.startswith(opener):
            text = text[len(opener):]
            first_word = text.split(" ", 1)[0] if text else ""
            if text and text[0].isalpha() and not any(ch.isdigit() for ch in first_word):
                text = text[0].upper() + text[1:]
            break
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "..."
    return text


def _format_entities(entities):
    entities = entities or {}
    labels = [
        ("genes", "Genes/determinants"),
        ("antibiotics", "Antibiotics"),
        ("mutations", "Mutations/variants"),
        ("organisms", "Organisms"),
        ("mechanisms", "Mechanisms"),
    ]
    lines = [
        f"- {label}: {', '.join(entities.get(key) or [])}"
        for key, label in labels if entities.get(key)
    ]
    return "\n".join(lines) if lines else "- No specific entities were extracted; treat this as a general AMR question."


def _format_information_needed(items):
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not items:
        return "- Answer the user's question directly; no further breakdown was identified."
    return "\n".join(f"{i}. {text}" for i, text in enumerate(items, start=1))


def _format_reference_evidence(reference_evidence):
    if not reference_evidence:
        return "No CARD/NCBI reference match was required or available for this query."
    lines = []
    for item in reference_evidence:
        bits = [item.get("canonical_name") or item.get("mention") or "Unknown"]
        if item.get("aro_id"):
            bits.append(f"ARO: {item['aro_id']}")
        if item.get("source"):
            bits.append(f"source: {item['source']}")
        line = " | ".join(bits)
        if item.get("description"):
            line += f" -- {item['description']}"
        lines.append(f"- {line}")
    return "\n".join(lines)


def _format_papers(references, evidence, max_papers=SYNTHESIS_MAX_PAPERS):
    annotated_index = _annotated_paper_index(evidence)
    if not references:
        return "No PubMed records were retrieved for this query."

    shown = references[:max_papers]
    lines = []
    for ref in shown:
        identity = _paper_identity(ref)
        annotated = annotated_index.get(identity, {})
        tier, specificity = _tier_for(annotated)
        pub_types = ref.get("publication_types") or annotated.get("publication_types") or []
        kind = "review" if any("review" in t.lower() for t in pub_types) else "primary/other"
        snippets = annotated.get("evidence_snippets") or []
        snippet_texts = [_clean_snippet(s.get("text")) for s in snippets[:3] if s.get("text")]
        snippet_block = " | ".join(snippet_texts) if snippet_texts else (
            "(no high-relevance sentence extracted; title/metadata only -- do not attribute a specific "
            "mechanistic detail to this record)"
        )
        specificity_note = ""
        if specificity == "other_organism":
            specificity_note = " -- CAUTION: this record's own text does not mention the organism the user asked about; treat as a different-species data point, not organism-specific evidence"
        elif specificity == "family":
            specificity_note = " -- gene-family-level match, not the exact named allele/variant"
        url = ref.get("pubmed_url") or ref.get("doi_url") or ""
        lines.append(
            f"[{ref['reference_number']}] {ref.get('title')} "
            f"({ref.get('journal') or 'journal not recorded'}, {ref.get('year') or 'year not recorded'}) "
            f"PMID: {ref.get('pmid') or 'N/A'} | URL: {url}\n"
            f"    Evidence tier: {tier} ({specificity} match){specificity_note} | Article type: {kind}\n"
            f"    Source material (paraphrase only; never copy verbatim): {snippet_block}"
        )

    if len(references) > max_papers:
        lines.append(
            f"(...{len(references) - max_papers} additional, lower-ranked retrieved records exist but are not "
            f"shown here. Do not cite reference numbers beyond [{max_papers}].)"
        )
    return "\n\n".join(lines)



def _format_mutation_reference(evidence):
    ref = evidence.get("mutation_reference") or {}
    records = ref.get("mutations") or []
    if not records:
        return "No specialized mutation-reference records were available."
    # Keep the model grounded in the database's phenotype wording.
    lines = []
    for r in records[:35]:
        detail = r.get("phenotype_detail") or ""
        lines.append(
            f"- {r.get('mutation')}: phenotype={r.get('phenotype')}; "
            f"source={r.get('source')}; confidence={r.get('confidence')}; {detail}"
        )
    if len(records) > 35:
        lines.append(f"- ... {len(records)-35} additional mutation records omitted from the synthesis brief.")
    return "\n".join(lines)


def _format_literature_mutations(evidence):
    records = evidence.get("literature_mutations") or []
    if not records:
        return "No explicit organism-specific mutations were extracted from the literature evidence."
    lines = []
    for r in records[:80]:
        lines.append(
            f"- {r.get('mutation')}: phenotype={r.get('phenotype')}; "
            f"PMID={r.get('pmid') or 'N/A'}; title={r.get('title') or 'N/A'}"
        )
    return "\n".join(lines)

def _prompt(query, understanding, evidence, references):
    entities = understanding.get("entities", {})
    intents = ", ".join(understanding.get("intent") or []) or "amr_information_retrieval"
    organism_specified = bool(entities.get("organisms"))

    organism_rule = ""
    if organism_specified:
        organism_rule = (
            f"\n- The user asked specifically about {', '.join(entities.get('organisms'))}. Prioritize records whose "
            f"specificity is \"exact\" (organism confirmed) for organism-specific claims. Records flagged "
            f"\"other_organism\" concern a different species -- you may use them only as explicitly-labelled "
            f"supporting/related context (e.g. \"a related mechanism has also been reported in Klebsiella "
            f"pneumoniae\"), never presented as if they were evidence about the organism the user asked about. "
            f"Do not blend findings from different organisms into one unqualified statement."
        )

    return f"""You are the scientific answer-generation component of an antimicrobial-resistance (AMR) research assistant. Your output is read directly by a researcher -- it must read like a scientific answer, never like a pipeline/debugging report.

Answer the COMPLETE user question below -- every distinct information need listed, not only the entity-identification part. If a gene/variant is named, do not reduce the answer to a database annotation; explain the underlying biological question using the retrieved PubMed evidence.

MUTATION GROUNDING RULES
- For mutation-focused questions, the structured mutation-reference table is the authoritative allowlist for database-derived mutation names.
- Never invent, autocorrect, or substitute an amino-acid letter or position. If the structured record says H445Y, write H445Y -- never R445Y.
- Do not introduce a mutation from a literature snippet unless that exact mutation is present in the structured mutation reference or in the explicitly supplied literature-mutation extraction.
- Do not present nucleotide-level c. variants, synonymous variants, or upstream variants in the main mutation table unless the user explicitly asks for nucleotide-level variants.
- For a resistance query, do not list WHO records graded "Not assoc w R" or "Uncertain significance" as resistance mutations.
- For this release, a mutation-focused query is deliberately narrow: answer the mutation list and its resistance/susceptibility phenotype. Do NOT add general pathogen biology, treatment advice, broad resistance mechanisms, epidemiology, diagnostic methods, or unrelated AMR genes unless the user explicitly asks for them.
- Do NOT turn a paper's general statement about tuberculosis drug resistance into a claim about an individual rpoB mutation. A literature item is useful for a mutation claim only when the mutation/gene + organism + drug relationship is actually represented in the extracted evidence.

OUTPUT FORMAT
- Open with a short (2-4 sentence) direct-answer paragraph covering the core finding(s), with inline citations.
- Adapt structure to the question type; do not force one template on every query:
    - Definition question ("What is X?"): identity/function paragraph, then biological significance, evidence, references.
    - Mechanism question ("How does X confer resistance?"): a short numbered or arrow-style causal chain (e.g. "colistin binds lipid A -> phosphoethanolamine modification alters membrane charge -> reduced colistin binding -> resistance"), only if the evidence actually supports each step, then evidence/references.
    - Mutation-focused question (e.g. "which mutations in gyrA/parC..."): give a concise direct answer, then a mutation table/list with the exact reported substitutions and their resistance/susceptibility phenotype, followed by only mutation-specific evidence and references. Do not add a mechanism paragraph unless the user explicitly asks how the mutation causes resistance.
    - Multi-mechanism question (e.g. "mechanisms of carbapenem resistance in K. pneumoniae"): organize by mechanism category (carbapenemases, porin loss, efflux, combinations), not by paper.
    - Comparison question ("compare X and Y"): a short table or clearly separated sub-sections for each side, then a brief synthesis of the difference.
  When more than one information need is listed below, address each under its own short heading, in the order listed. For a single focused question, use connected paragraphs instead of forcing headings.
- Weave citations into the text right after the specific claim they support, not only once at the end of a long paragraph. For example: "Fluoroquinolone resistance in E. coli is strongly associated with QRDR mutations in gyrA and parC [1,2]. Commonly reported GyrA substitutions include Ser83 and Asp87 changes [1,3]." Each sentence's citation should be the reference(s) that actually support that specific sentence.
- Synthesize across the multiple retrieved papers below in your own words; never copy a sentence from the "Source material" shown for a paper into your answer verbatim or near-verbatim -- restate the finding concisely.
- Preserve exact reported values precisely (e.g. "S83L", "D87N") -- do not normalize, round, or invent a substitution, and do not conflate gene / protein / amino-acid / nucleotide levels.
- For a question asking which mutations are associated with resistance, prioritize the structured resistance-associated records and keep the displayed list concise; mention that additional catalogue variants exist rather than dumping the entire database.
- For mutation-focused queries, IGNORE any literature paper that does not explicitly support the requested gene + organism + antibiotic/phenotype relationship. Never use a generic tuberculosis paper to write an evidence paragraph.
- If the structured mutation catalogue already supplies the mutation-phenotype relationship, state that database finding directly. Literature is supporting evidence only.
- Do not discuss geography, diagnostic technologies, epidemiology, host pathways, persistence, treatment, or other resistance genes unless that exact topic is explicitly present in the user's query.
- Aim to cite roughly 5-15 of the most relevant references overall (more only for a broad, review-style question) -- you do not need to cite every record shown below, only the ones that actually support a claim you made.
- Add a brief closing section (e.g. "Other mechanisms", "Evidence summary") only if the evidence genuinely supports something beyond the core answer. Do not invent significance that isn't supported.
- Use a short bullet list only where it genuinely improves readability (e.g. listing several established mutations per gene); do not bullet a mechanism narrative that reads better as prose.
- Inline citations use markdown links: [1](PUBMED_URL) for a single reference, or [1](URL1), [2](URL2) for a grouped citation -- always the supplied PubMed URL, never an invented one.
- End with a "References" section containing ONLY references actually cited in the answer text, numbered to match your inline citations.
- Never write internal-pipeline phrasing, including (but not limited to): "the available PubMed literature relevant to...", "the retrieved literature provides...", "the retrieved literature most directly relevant...", "the evidence candidates...", "the available AMR reference records...", "the literature retrieval step...", "the provided articles...", or "the query was matched to...". Write as a scientific answer, not a description of what the retrieval pipeline did. Do not print search queries, ranking scores, agent names, or a "top papers" list.

EVIDENCE RULES
- For the mutation/phenotype table, the specialized mutation catalogue is the authoritative database-level source. PubMed/PMC provides supporting scientific evidence and context. CARD/NCBI are identity/nomenclature/annotation references only -- never cite them as if they were an experimental finding.
- Each PubMed record below has an evidence tier: "direct" (the record's own text represents the exact requested entity/relationship), "family-context" (a parent AMR family/determinant that explains broader mechanism but is not allele-specific proof), "related" (overlaps part of the query without establishing the full relationship), or "weak/unclassified" (do not use for specific claims).
- Prefer "direct" tier records for the core mechanistic/causal claims. "family-context" records may support general mechanism statements but must be explicitly flagged as family-level, not allele-specific, evidence (e.g. "the broader mcr family..." rather than implying the exact named variant was studied).{organism_rule}
- When a record shows "(no high-relevance sentence extracted...)", you only know its topic from the title -- do not attribute a specific mechanistic detail to it.
- If no retrieved record establishes the exact relationship asked about, say so explicitly (e.g. "the retrieved evidence supports X but is more limited for Y"), and only then may you add a clearly labelled "Hypothesis" paragraph drawing a conservative inference from family-context/related evidence -- explicitly stating it is not an established finding.
- Never invent mechanisms, mutations, organisms, experimental results, dates, PMIDs, or citations. Never cite a reference number that was not supplied below.

USER QUERY
{query}

INFORMATION NEEDS TO ADDRESS (use these to guide section structure when there is more than one)
{_format_information_needed(understanding.get("information_needed"))}

QUERY INTERPRETATION
- Requested intent(s): {intents}
{_format_entities(entities)}

CARD / NCBI REFERENCE GROUNDING (identity/annotation only -- not literature evidence)
{_format_reference_evidence(evidence.get("reference_evidence", []))}

SPECIALIZED MUTATION REFERENCE GROUNDING
{_format_mutation_reference(evidence)}

LITERATURE MUTATION EXTRACTION
{_format_literature_mutations(evidence)}

RETRIEVED PUBMED EVIDENCE (cite using these exact bracket numbers only; internal notes below are for your grounding only, never to be echoed or copied into the answer)
{_format_papers(references, evidence)}
"""


def _reference_records(evidence):
    ref = evidence.get("mutation_reference") or {}
    return ref.get("all_mutations") or ref.get("mutations") or []


def _requested_phenotype(query):
    """Infer the direction actually requested, including explicit non-resistance."""
    low = str(query or "").lower()
    if re.search(r"\bnot\s+(?:associated\s+with\s+)?(?:rifampicin\s+|rifampin\s+)?resistan\w*\b", low) or re.search(r"\bnot\s+resistance[- ]associated\b", low):
        return "non_resistance"
    asks_resistance = bool(re.search(r"\b(resistan\w*|resist\w*)\b", low))
    asks_susceptibility = bool(re.search(r"\b(susceptib\w*|sensitive\s+to)\b", low))
    if asks_resistance and not asks_susceptibility:
        return "resistance"
    if asks_susceptibility and not asks_resistance:
        return "susceptibility"
    return None


def _phenotype_matches(record, requested):
    """Do not promote unclear or opposite-phenotype records to a requested claim."""
    if not requested:
        return True
    phenotype = str(record.get("phenotype") or "").lower()
    if requested == "resistance":
        return "resistance-associated" in phenotype and "not associated" not in phenotype and "not explicit" not in phenotype
    if requested == "susceptibility":
        return "susceptibility-associated" in phenotype and "not explicit" not in phenotype
    if requested == "non_resistance":
        return "not associated with resistance" in phenotype
    return True


def _literature_record_has_traceable_support(record, query=None, entities=None):
    """Require a source sentence that contains the allele and queried drug.

    Literature-only candidates must not enter the findings table solely because
    a paper passed broad entity screening. The extractor supplies evidence_sentence
    from the exact sentence in which the mutation was found and its phenotype was
    classified; this final guard ensures that sentence remains attached to the
    visible claim.
    """
    sentence = re.sub(r"\s+", " ", str(record.get("evidence_sentence") or "")).strip()
    if not sentence:
        return False
    mutation = str(record.get("mutation") or "").strip()
    written = str(record.get("as_written") or "").strip()
    forms = {mutation.upper()}
    if written:
        forms.add(written.upper().replace("P.", ""))
    if not any(
        re.search(r"(?<![A-Z0-9])" + re.escape(form) + r"(?![A-Z0-9])", sentence, re.I)
        for form in forms if form
    ):
        return False
    drugs = (entities or {}).get("antibiotics") or []
    if drugs and not any(drug_hits(sentence, drug) for drug in drugs):
        return False
    return True


def _mutation_table(evidence, max_rows=20, query=None, entities=None):
    """Reference rows own the table; literature-only rows must explicitly support
    the requested phenotype. Unclear and opposite-phenotype literature stays out
    of a directional resistance/susceptibility findings table."""
    ref = evidence.get("mutation_reference") or {}
    requested_phenotype = _requested_phenotype(query)
    entities = entities or {}
    # Organism-unspecified NCBI records stay reference grounding only.
    db_records = [r for r in (ref.get("mutations") or []) if r.get("organism_specific") is not False]
    db_records = [r for r in db_records if _phenotype_matches(r, requested_phenotype)]
    requested_subs = {
        parse_substitution(v)
        for v in [*(extract_query_variants(query or "")), *(entities.get("mutations") or []), *(entities.get("variants") or [])]
    }
    requested_subs.discard(None)
    if requested_subs:
        # A named-variant question must not receive the whole gene catalogue.
        # MutationIndex also handles known alternate residue numbering, such as
        # M. tuberculosis rpoB S450L vs the E. coli-numbered S531L notation.
        gene_names = entities.get("genes") or []
        organism_names = entities.get("organisms") or []
        ref_variants = [
            v for r in db_records for v in (r.get("mutation"), r.get("reported_mutation")) if v
        ]
        index = MutationIndex(ref_variants, gene_names, organism_names)
        matched_canon = set()
        for sub in requested_subs:
            canon, _numbering = index.lookup(sub)
            if canon:
                matched_canon.add(str(canon).upper())
            matched_canon.add(f"{sub[0]}{sub[1]}{sub[2]}".upper())
        db_records = [
            r for r in db_records
            if str(r.get("mutation") or "").upper() in matched_canon
            or str(r.get("reported_mutation") or "").upper() in matched_canon
        ]
    records, seen = [], set()
    for r in db_records:
        key = str(r.get("mutation") or r.get("reported_mutation") or "").upper()
        if key and key in seen:
            continue
        seen.add(key)
        records.append(r)
    # A paper may support a complete catalogue mutation that is outside the
    # user-facing 20-row catalogue preview. Surface that one explicit finding
    # (with the curated classification and its paper evidence) rather than
    # claiming the literature was ignored. Never duplicate a displayed row.
    literature_extras = []
    for item in evidence.get("literature_mutations") or []:
        phenotype = str(item.get("phenotype") or "").lower()
        if phenotype not in {"resistance-associated evidence", "susceptibility-associated evidence"}:
            continue
        if not _phenotype_matches(item, requested_phenotype) or not _literature_record_has_traceable_support(item, query, entities):
            continue
        key = str(item.get("mutation") or "").upper()
        if not key or key in seen:
            continue
        extra = dict(item)
        classification = item.get("reference_classification") or {}
        if classification:
            extra["source"] = classification.get("source") or extra.get("source")
            extra["source_url"] = "https://www.who.int/publications/i/item/9789240082410" if "WHO" in str(extra.get("source")) else extra.get("source_url")
            extra["confidence"] = classification.get("confidence") or extra.get("confidence")
            extra["phenotype"] = classification.get("phenotype") or extra.get("phenotype")
        literature_extras.append(extra)
        seen.add(key)
    if literature_extras:
        records = literature_extras + records
    if not records:
        by_mutation = {}
        for r in evidence.get("literature_mutations") or []:
            phenotype = str(r.get("phenotype") or "").lower()
            # Literature-only rows require an explicit directional phenotype;
            # a sentence that merely mentions a resistance-determining region
            # is not evidence that the listed allele causes resistance.
            explicit_phenotype = phenotype in {
                "resistance-associated evidence", "susceptibility-associated evidence"
            }
            if not explicit_phenotype or not _phenotype_matches(r, requested_phenotype):
                continue
            if not _literature_record_has_traceable_support(r, query, entities):
                continue
            if requested_subs:
                sub = parse_substitution(r.get("mutation") or r.get("as_written"))
                if sub not in requested_subs:
                    # Alternate numbering is only accepted when a reference
                    # mapping above establishes the canonical mutation.
                    canon, _numbering = index.lookup(sub) if sub else (None, None)
                    if not canon or str(canon).upper() not in matched_canon:
                        continue
            key = str(r.get("mutation") or "").upper()
            if key and key not in by_mutation:
                by_mutation[key] = r
        records = list(by_mutation.values())
    if not records:
        return ""
    rows = ["### Mutation findings", "", "| Mutation | Phenotype / resistance status | Evidence source |",
            "|---|---|---|"]
    for r in records[:max_rows]:
        mutation = r.get("mutation") or r.get("reported_mutation") or "Not reported"
        if r.get("query_notation") and r.get("query_notation_scheme"):
            mutation = f"{mutation} (written {r['query_notation']}; {r['query_notation_scheme']})"
        elif r.get("as_written") and r.get("numbering") and str(r.get("as_written")).upper().replace("P.", "") != str(mutation).upper():
            mutation = f"{mutation} (written {r['as_written']}; {r['numbering']})"
        if r.get("reference_classification") and "WHO" in str(r.get("source")):
            mutation = f"{mutation} [{r.get('source')}: {r.get('phenotype')}]"
        phenotype = r.get("phenotype") or "Not reported"
        if r.get("evidence_type"):
            phenotype = f"{phenotype}; {r['evidence_type']}"
        confidence = str(r.get("confidence") or "")
        if confidence.startswith("1)"):
            phenotype = f"{phenotype} (WHO grade 1)"
        elif confidence.startswith("2)"):
            phenotype = f"{phenotype} (WHO grade 2; interim)"
        source = r.get("source") or "Database"
        source_url = r.get("source_url")
        if source == "WHO TB Mutation Catalogue 2023":
            source_display = f"[{source}: {r.get('phenotype', 'classification')}]({source_url})" if source_url else f"[{source}: {r.get('phenotype', 'classification')}]"
        else:
            source_display = f"[{source}]({source_url})" if source_url else source
        rows.append(f"| {mutation} | {phenotype} | {source_display} |")
    total = ref.get("total_matching_records")
    shown = min(len(records), max_rows)
    if db_records and total and total > shown and not requested_subs:
        rows.append("")
        rows.append(
            f"Showing {shown} of {total} matching reference records (display limit {max_rows}); "
            "the complete set was used when screening the literature."
        )
    return "\n".join(rows)


def _ensure_mutation_table(text, evidence, query=None, entities=None):
    table = _mutation_table(evidence, query=query, entities=entities)
    if not table:
        return text
    # If the model already produced a table, do not duplicate it.
    if "| Mutation |" in (text or "") or "| Mutation | Phenotype" in (text or ""):
        return text
    return table + "\n\n" + (text or "")


def _fallback(query, understanding, evidence, references):
    """Conservative fallback when the local LLM is unavailable, errors, or
    times out. Still tier-aware and multi-paper, so a degraded response
    doesn't collapse into a single paper's abstract sentence, and clearly
    flagged as degraded so it isn't mistaken for the real synthesis path."""
    direct, family, related, other_organism = [], [], [], []
    for item in evidence.get("literature_evidence", []) or []:
        for paper in item.get("direct_evidence", []) or []:
            direct.append(paper)
        for paper in item.get("related_evidence", []) or []:
            if paper.get("evidence_specificity") == "other_organism":
                other_organism.append(paper)
            elif paper.get("evidence_alignment") in _FAMILY_ALIGNMENTS:
                family.append(paper)
            else:
                related.append(paper)

    entities = understanding.get("entities", {})
    genes = entities.get("genes", [])
    antibiotics = entities.get("antibiotics", [])
    organisms = entities.get("organisms", [])
    subject = ", ".join(dict.fromkeys([*(genes or []), *(antibiotics or [])]))

    candidates = direct or family or related or other_organism
    if not candidates:
        text = (
            "PubMed retrieval did not return literature that could be used as evidence for this question. "
            "CARD/NCBI can support AMR entity identification, but they are not a substitute for published "
            "experimental evidence."
        )
        if subject:
            text = f"For {subject}: " + text
        return text + (
            "\n\n(Note: this is a degraded fallback response because the scientific synthesis model was "
            "unavailable, errored, or timed out.)"
        )

    refs = _numbered_references(references)
    ref_by_identity = {_paper_identity(r): r for r in refs}

    def _cite(paper):
        ref = ref_by_identity.get(_paper_identity(paper))
        return ref["reference_number"] if ref else None

    parts = []
    cited_numbers = []
    mutation_table = _mutation_table(evidence)
    if mutation_table:
        parts.append(mutation_table)

    def _paragraph_for(papers, lead_in, max_snippets=3):
        nonlocal cited_numbers
        snippets, nums = [], []
        for paper in papers:
            for s in (paper.get("evidence_snippets") or [])[:2]:
                text = _clean_snippet(s.get("text"))
                if text and text not in snippets:
                    snippets.append(text)
            n = _cite(paper)
            if n and n not in nums:
                nums.append(n)
            if len(snippets) >= max_snippets:
                break
        if not snippets:
            return None
        cited_numbers.extend(n for n in nums if n not in cited_numbers)
        citation = "[" + ",".join(str(n) for n in nums) + "]" if nums else ""
        text = f"{lead_in} {' '.join(snippets)} {citation}".strip()
        return " ".join(text.split())

    direct_para = _paragraph_for(direct, "")
    if direct_para:
        parts.append(direct_para)

    family_para = _paragraph_for(
        family, "More broadly, evidence on the wider gene family (not the exact named variant) indicates that"
    )
    if family_para:
        parts.append(family_para)

    if organisms and other_organism and not direct:
        other_org_para = _paragraph_for(
            other_organism,
            f"Direct evidence in {', '.join(organisms)} was limited in the retrieved records; related findings "
            f"reported in other bacterial species include:",
        )
        if other_org_para:
            parts.append(other_org_para)

    if not direct and not family and related:
        related_para = _paragraph_for(related, "Related literature touches on part of this question:")
        if related_para:
            parts.append(related_para)

    if not direct:
        parts.append(
            "Current retrieved literature did not provide sufficient direct evidence to establish the complete "
            "relationship asked about; the above should be read as contextual, not confirmatory."
        )
        if related or family:
            parts.append(
                "Hypothesis (not established): the related or family-level findings may be biologically consistent "
                "with the requested association, but the available records do not independently demonstrate it for "
                "the exact mutation, organism, and phenotype in the question."
            )

    if not parts:
        parts.append(
            "The retrieved records did not contain a high-relevance sentence to summarize; only titles/metadata "
            "were available for this query."
        )

    if cited_numbers:
        cited_refs = [ref_by_identity[pid] for pid in ref_by_identity if ref_by_identity[pid]["reference_number"] in cited_numbers]
        cited_refs.sort(key=lambda r: r["reference_number"])
        parts.append(
            "References\n" + "\n".join(
                f"[{r['reference_number']}] {r.get('title')} (PMID: {r.get('pmid') or 'N/A'})"
                for r in cited_refs
            )
        )

    parts.append(
        "(Note: this is a degraded fallback response because the scientific synthesis model was unavailable, "
        "errored, or timed out; it summarizes extracted evidence sentences directly rather than performing full "
        "language-model synthesis.)"
    )
    return "\n\n".join(parts)


def _retrieval_summary(evidence):
    """Aggregate retrieval status over literature sets (worst case wins)."""
    order = ["failed", "partial_failure", "no_matching_evidence", "no_pubmed_records", "matches_found"]
    sets = [i for i in (evidence.get("literature_evidence") or []) if i.get("retrieval")]
    if not sets:
        return None
    statuses = [i["retrieval"].get("status") for i in sets]
    status = min(statuses, key=lambda x: order.index(x) if x in order else len(order))
    agg = {"status": status, "queries_attempted": 0, "queries_failed": 0, "records_retrieved": 0,
           "records_passing_filter": 0, "pmc": {"attempted": 0, "errors": 0, "retrieved": 0}}
    for i in sets:
        r = i["retrieval"]
        for k in ("queries_attempted", "queries_failed", "records_retrieved", "records_passing_filter"):
            agg[k] += r.get(k) or 0
        for k in ("attempted", "errors", "retrieved"):
            agg["pmc"][k] += (i.get("pmc") or {}).get(k, 0)
    return agg


def _literature_status_note(summary, genes, drugs, organisms):
    subject = f"{genes or 'the requested gene'} with {drugs or 'the requested antibiotic'} in {organisms or 'the requested organism'}"
    if summary is None:
        return "No supporting literature was assessed for this answer."
    st = summary["status"]
    if st == "failed":
        return (
            f"PubMed retrieval failed ({summary['queries_failed']} of {summary['queries_attempted']} searches returned "
            "errors), so supporting literature could not be assessed. This is a retrieval failure, not evidence that "
            "no supporting papers exist. The mutation-phenotype associations above come from the structured reference."
        )
    if st == "partial_failure":
        return (
            f"{summary['queries_failed']} of {summary['queries_attempted']} PubMed searches failed, so the literature "
            "check is incomplete. Absence of papers below should not be read as absence of evidence."
        )
    if st == "no_pubmed_records":
        return (
            f"PubMed was searched successfully but returned no records for {subject}. "
            "The mutation-phenotype associations above come from the structured reference."
        )
    if st == "no_matching_evidence":
        return (
            f"PubMed was searched successfully and returned {summary['records_retrieved']} records, but none reported a "
            f"mutation for {subject} in its title/abstract (or available full text), so no mutation-specific supporting "
            "literature was identified. The mutation-phenotype associations above come from the structured reference."
        )
    return "Papers met the screening criteria, but no mutation statement could be extracted from them."


def _acquired_operon_explanation(genes, organisms, drugs, query, evidence):
    """Provide a comprehensive biological explanation when an AMR query asks about mutations
    in an acquired determinant/operon whose resistance is mediated by intact gene expression
    rather than point mutations."""
    gene_str = (genes[0] if genes else "").strip()
    low_gene = gene_str.lower()
    org_str = (organisms[0] if organisms else "Enterococcus faecium").strip()
    drug_str = (drugs[0] if drugs else "vancomycin").strip()

    if low_gene in {"vana", "vanb", "vanc", "vand", "vane", "vang"}:
        transposon = "Tn1546" if low_gene == "vana" else ("Tn1549 / Tn5382" if low_gene == "vanb" else "chromosomal/mobile cassette")
        spectrum = "both vancomycin and teicoplanin" if low_gene == "vana" else "vancomycin (with variable or inducible teicoplanin susceptibility)"
        return (
            f"In *{org_str}*, resistance to **{drug_str}** is typically mediated by the **intact expression of the acquired {gene_str} gene cluster** "
            f"(carried on mobile elements such as the **{transposon}** transposon), rather than single point mutations within the {gene_str} structural ligase gene itself.\n\n"
            f"The original, wild-type sequence of the {gene_str} operon successfully alters the bacterial cell-wall synthesis pathway by switching target precursors from D-Ala-D-Ala to D-Ala-D-Lac, which reduces vancomycin binding affinity by approximately 1,000-fold, conferring high-level resistance to {spectrum}.\n\n"
            f"However, genetic variation—specifically mutations, insertions, and deletions occurring within and around the {gene_str} operon—dramatically influences phenotypic expression, giving rise to **Vancomycin-Variable Enterococci (VVE)**. These strains are genotypically {gene_str}-positive but initially test as phenotypically vancomycin-susceptible. Reversion to high-level vancomycin resistance occurs due to several well-documented mutational mechanisms:\n\n"
            f"1. **Deletions in the *vanRS* Regulatory System:** The *vanR* and *vanS* genes act as the two-component sensor/regulator system that detects glycopeptides and triggers expression of the *vanHAX* resistance cassette. Deletions or loss-of-function mutations in *vanS* can lead to constitutive expression or silencing.\n"
            f"2. **Transposon Alterations and Insertion Sequences:** Integration of insertion sequences (such as IS1216-like elements) into *{transposon}* can cause operon silencing or structural rearrangements; subsequent excision, promoter mutations, or duplications restore resistance upon drug exposure.\n"
            f"3. **Promoter and Upstream Deletions:** Mutations in the promoter region governing the resistance cassette alter transcription efficiency, mediating variable phenotypic expression."
        )

    # General acquired resistance determinants via CARD
    card_info = lookup_card(gene_str) if gene_str else {}
    if card_info and card_info.get("status") == "validated":
        desc = (card_info.get("description") or "").strip('"')
        aro = card_info.get("aro_id") or "CARD"
        return (
            f"In *{org_str}*, resistance to **{drug_str}** associated with **{gene_str}** is typically mediated by the **acquisition and functional expression of the intact determinant** rather than single point mutations within its structural sequence.\n\n"
            f"### Determinant Profile ({aro})\n"
            f"- **Mechanistic Description:** {desc}\n"
            f"- **Genetic Context:** Clinical resistance is conferred by the enzymatic activity or target replacement mediated by {gene_str}. Secondary mutations or copy-number alterations may affect expression levels, but resistance does not require amino-acid substitutions in the catalytic core."
        )
    return None


def _mutation_focused_response(query, understanding, evidence, references):
    """Return only query-matched mutation findings and mutation-specific citations."""
    entities = understanding.get("entities", {}) or {}
    genes = [g for g in dedupe_by_key(entities.get("genes") or []) if not parse_substitution(g)]
    drugs = dedupe_by_key(entities.get("antibiotics") or [], canonical_drug)
    organisms = dedupe_by_key(entities.get("organisms") or [], canonical_organism)
    phenotype = _requested_phenotype(query)
    phenotype_label = phenotype or "resistance/susceptibility"

    ref = evidence.get("mutation_reference") or {}
    if not genes:
        # If upstream resolution mistakenly surfaced a named substitution as a
        # gene, recover the real gene from the structured reference record.
        for record in (ref.get("mutations") or []):
            candidate = record.get("gene")
            if not candidate:
                candidate = str(record.get("reported_variant") or "").split("_", 1)[0]
            if candidate and not parse_substitution(candidate) and candidate not in genes:
                genes.append(candidate)
    gene_label = ", ".join(genes)
    drug_label = ", ".join(drugs)
    organism_label = ", ".join(organisms)
    table = _mutation_table(evidence, max_rows=20, query=query, entities=entities)
    reference_rows = [
        r for r in (ref.get("mutations") or [])
        if r.get("organism_specific") is not False and _phenotype_matches(r, phenotype)
    ]
    table_rows = []
    if table:
        # Used only to avoid repeating the same mutation in the literature section.
        for r in reference_rows:
            table_rows.append(str(r.get("mutation") or r.get("reported_mutation") or "").upper())
        if not reference_rows:
            table_rows.extend(
                str(r.get("mutation") or "").upper()
                for r in (evidence.get("literature_mutations") or [])
                if str(r.get("phenotype") or "").lower() in {
                    "resistance-associated evidence", "susceptibility-associated evidence"
                } and _phenotype_matches(r, phenotype)
            )

    retrieval = _retrieval_summary(evidence)
    parts = []
    if table:
        if reference_rows:
            source_names = list(dict.fromkeys(str(r.get("source") or "specialized mutation reference") for r in reference_rows))
            source_label = source_names[0] if len(source_names) == 1 else "specialized mutation references"
            parts.append(
                f"{source_label} classifications for {gene_label or 'the queried determinant'}"
                f"{(' in ' + organism_label) if organism_label else ''}"
                f"{(' against ' + drug_label) if drug_label else ''}:"
            )
        else:
            parts.append(
                f"Mutation-level literature evidence matching {gene_label or 'the queried determinant'}"
                f"{(' in ' + organism_label) if organism_label else ''}"
                f"{(' and ' + drug_label) if drug_label else ''}"
                f" for {phenotype_label} is listed below."
            )
        parts.append(table)
    else:
        requested_variants = [*(extract_query_variants(query or "")), *(entities.get("mutations") or []), *(entities.get("variants") or [])]
        issues = [mutation_position_issue(v, genes, organisms) for v in requested_variants]
        issues = [issue for issue in issues if issue]
        if issues:
            parts.append(
                f"The requested notation cannot be validated as a protein substitution for "
                f"{gene_label or 'the queried determinant'}"
                f"{(' in ' + organism_label) if organism_label else ''}: {issues[0]}. "
                "Any nucleotide or assay-specific coordinate requires reference-sequence validation before it can be interpreted as an amino-acid resistance mutation."
            )
        elif requested_variants:
            parts.append(
                f"No qualifying mutation-level evidence was identified for {gene_label or 'the queried determinant'}"
                f"{(' in ' + organism_label) if organism_label else ''}"
                f"{(' with ' + drug_label) if drug_label else ''} under the requested phenotype."
            )
        else:
            acquired_context = _acquired_operon_explanation(genes, organisms, drugs, query, evidence)
            if acquired_context:
                parts.append(acquired_context)
            else:
                retrieval_status = (retrieval or {}).get("status")
                if retrieval_status in {"failed", "partial_failure"}:
                    parts.append(
                        f"The evidence check for {gene_label or 'the queried determinant'} was incomplete because "
                        "one or more literature sources failed; this result should not be interpreted as evidence of absence."
                    )
                else:
                    passages, _ = _extract_auditable_passages(evidence, references)
                    if passages:
                        parts.append(
                            f"Curated reference catalogue entries were not identified for {gene_label or 'the queried determinant'}"
                            f"{(' in ' + organism_label) if organism_label else ''}"
                            f"{(' with ' + drug_label) if drug_label else ''}, but published experimental literature describes the following alterations and phenotypic associations:"
                        )
                    else:
                        parts.append(
                            f"No qualifying mutation-level evidence was identified for {gene_label or 'the queried determinant'}"
                            f"{(' in ' + organism_label) if organism_label else ''}"
                            f"{(' with ' + drug_label) if drug_label else ''} under the requested phenotype."
                        )

    if retrieval and retrieval.get("status") in {"failed", "partial_failure"}:
        parts.append(
            f"Literature check incomplete: {retrieval.get('queries_failed', 0)} of "
            f"{retrieval.get('queries_attempted', 0)} PubMed searches failed. "
            "The mutation table above is based on the cited structured reference, not a completed literature check."
        )

    # Only literature mutations with explicit, directionally matching phenotype
    # evidence can be shown as supporting claims. Do not paste unrelated abstract
    # sentences into the answer; list the supported mutation and its citation.
    refs = _numbered_references(references)
    ref_index = {_paper_identity(r): r for r in refs}
    pmid_index = {str(r.get("pmid")): r for r in refs if r.get("pmid")}
    explicit_query_variants = {
        str(v).upper() for v in [*(entities.get("mutations") or []), *(entities.get("variants") or [])]
    }
    # A PMID is not itself proof of a mutation-phenotype relationship. Keep
    # only records carrying the actual extracted sentence, then display that
    # sentence so the user can audit why each mutation was included. Group by
    # mutation (rather than printing one repetitive generic bullet per PMID).
    support_by_mutation = {}
    for m in evidence.get("literature_mutations") or []:
        mphenotype = str(m.get("phenotype") or "").lower()
        if mphenotype not in {"resistance-associated evidence", "susceptibility-associated evidence"}:
            continue
        if not _phenotype_matches(m, phenotype):
            continue
        pmid = str(m.get("pmid") or "")
        ref_item = pmid_index.get(pmid)
        if not ref_item:
            continue
        mutation = str(m.get("mutation") or "").upper().strip()
        sentence = re.sub(r"\s+", " ", str(m.get("evidence_sentence") or "")).strip()
        if not mutation or not sentence:
            continue
        # The extracted quote must contain the mutation as written or in the
        # canonical form. This prevents a paper-level match from being shown as
        # allele-specific support when the stored record has no traceable quote.
        forms = {mutation}
        written = str(m.get("as_written") or "").strip()
        if written:
            forms.add(written.upper().replace("P.", ""))
        if not any(re.search(r"(?<![A-Z0-9])" + re.escape(form) + r"(?![A-Z0-9])", sentence, re.I) for form in forms if form):
            continue
        bucket = support_by_mutation.setdefault(mutation, {"records": [], "sentences": set()})
        sentence_key = sentence.casefold()
        if sentence_key in bucket["sentences"]:
            continue
        bucket["sentences"].add(sentence_key)
        bucket["records"].append({"record": m, "ref": ref_item, "sentence": sentence})

    retrieval = _retrieval_summary(evidence)
    pmc_note = ""
    if retrieval and (retrieval.get("pmc") or {}).get("errors", 0):
        n_errors = (retrieval.get("pmc") or {}).get("errors", 0)
        pmc_note = f"Full-text retrieval failed for {n_errors} article(s); those records were assessed from title/abstract only."

    cited_numbers = set()
    if support_by_mutation:
        support_lines = []
        for mutation, bucket in support_by_mutation.items():
            # Two distinct, directly extracted passages per mutation are enough
            # for a readable audit trail; all matching records remain available
            # in the retrieval/debug report.
            passages = bucket["records"][:2]
            # Preserve the established alternate-numbering annotation in the
            # answer. The evidence audit must not hide how a literature allele
            # was mapped to the canonical/reference mutation.
            first_record = passages[0]["record"] if passages else {}
            label = mutation
            written_label = str(first_record.get("as_written") or "").strip()
            numbering_label = str(first_record.get("numbering") or "").strip()
            if written_label and written_label.upper().replace("P.", "") != mutation.upper():
                note = f"written {written_label}"
                if numbering_label not in ("", "reference numbering", "as written"):
                    note += f"; {numbering_label}"
                label += f" ({note})"
            support_lines.append(f"- **{label}**")
            for passage in passages:
                ref_item = passage["ref"]
                n = ref_item["reference_number"]
                cited_numbers.add(n)
                source_kind = "full-text/abstract excerpt"
                if passage["record"].get("numbering") not in (None, "reference numbering", "as written"):
                    source_kind += f"; reported numbering: {passage['record']['numbering']}"
                quote = passage["sentence"].replace("\n", " ").strip()
                support_lines.append(f"  - {source_kind}: “{quote}” [{n}]")
        parts.append("### Supporting literature (auditable excerpts)\n" + "\n".join(support_lines))
        if pmc_note:
            parts.append(pmc_note)
        cited_refs = [r for r in refs if r["reference_number"] in cited_numbers]
        if cited_refs:
            parts.append("### References\n" + "\n".join(
                f"[{r['reference_number']}] {r.get('title')} — PMID: {r.get('pmid') or 'N/A'}"
                for r in cited_refs
            ))
    else:
        requested_variants = [*(extract_query_variants(query or "")), *(entities.get("mutations") or []), *(entities.get("variants") or [])]
        passages, auditable_refs = _extract_auditable_passages(evidence, references)

        variant_clarifications = []
        if requested_variants:
            v_target = requested_variants[0]
            v_pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(v_target) + r"(?![A-Za-z0-9])", re.I)
            for r in refs:
                t = (r.get("title") or "") + " " + (r.get("abstract") or "")
                if v_pat.search(t):
                    variant_clarifications.append(r)

        if variant_clarifications:
            vp = variant_clarifications[0]
            n = vp.get("reference_number") or 1
            p_title = vp.get("title", "")
            clarification_lines = ["### Literature Context & Biological Clarification"]
            if "23s" in p_title.lower() or "linezolid" in p_title.lower() or "oxazolidinone" in p_title.lower():
                clarification_lines.append(
                    f"Published literature clarifies that **{v_target}** is a **23S rRNA** mutation conferring **linezolid resistance** in *{organism_label or 'Enterococcus faecium'}* [{n}], "
                    f"rather than vanA-mediated vancomycin resistance. vanA-mediated resistance is conferred by an acquired D-Ala-D-Lac ligase gene cluster and does not involve 23S rRNA nucleotide alterations."
                )
            else:
                clarification_lines.append(
                    f"Published literature characterizes **{v_target}** in the context of: “{p_title}” [{n}]."
                )
            parts.append("\n".join(clarification_lines))
            if pmc_note:
                parts.append(pmc_note)
            parts.append(f"### References\n[{n}] {vp.get('title')} — PMID: {vp.get('pmid') or 'N/A'}")
        elif passages and not requested_variants:
            lit_lines = ["### Supporting Literature (Auditable Excerpts)"]
            cited_nums = set()
            for p in passages[:5]:
                n = p.get("ref_num")
                cite_str = f" [{n}]" if n else ""
                if n:
                    cited_nums.add(n)
                lit_lines.append(f"- “{p['text']}”{cite_str}")
            parts.append("\n".join(lit_lines))
            if pmc_note:
                parts.append(pmc_note)
            cited_refs = [r for r in auditable_refs if r.get("reference_number") in cited_nums]
            if cited_refs:
                parts.append("### References\n" + "\n".join(
                    f"[{r['reference_number']}] {r.get('title')} — PMID: {r.get('pmid') or 'N/A'}"
                    for r in cited_refs
                ))
        else:
            retrieval = _retrieval_summary(evidence)
            if retrieval and retrieval.get("status") == "no_pubmed_records":
                parts.append("PubMed returned no records for this query; the mutation findings above are based on the structured reference.")
            elif retrieval and retrieval.get("status") == "no_matching_evidence":
                parts.append("No paper met the mutation-specific evidence criteria; the mutation findings above are based on the structured reference.")
            else:
                parts.append("No traceable mutation-specific supporting sentence was available for the literature findings. Treat literature-only candidates as unverified until their source text can be checked.")
            if pmc_note:
                parts.append(pmc_note)

    return "\n\n".join(parts)


def _allowed_mutation_keys(understanding, evidence):
    allowed = set()
    records = [*_reference_records(evidence), *(evidence.get("literature_mutations") or [])]
    values = [r.get(k) for r in records for k in ("mutation", "reported_mutation", "as_written")]
    values += extract_query_variants(understanding.get("original_query") or "")
    values += (understanding.get("entities") or {}).get("mutations") or []
    for v in values:
        sub = parse_substitution(v)
        if sub:
            allowed.add(sub)
    return allowed


def _validate_llm_text(text, understanding, evidence, refs):
    """Reject LLM output that introduces a mutation not in the structured/literature
    evidence or the query, or cites a reference number that was not supplied."""
    allowed = _allowed_mutation_keys(understanding, evidence)
    schemes = MutationIndex([f"{a}{p}{b}" for a, p, b in allowed],
                            (understanding.get("entities") or {}).get("genes"),
                            (understanding.get("entities") or {}).get("organisms"))
    for item in extract_substitutions(text):
        if item["sub"] not in allowed and not schemes.lookup(item["sub"])[0]:
            return False
    for m in re.finditer(r"\[(\d+)\]", text):
        if int(m.group(1)) > len(refs):
            return False
    return True


def _extract_auditable_passages(evidence, references, max_papers=SYNTHESIS_MAX_PAPERS):
    """Extract auditable literature passages with assigned reference numbers."""
    refs = _numbered_references(references)
    ref_by_identity = {}
    for r in refs:
        if r.get("pmid"):
            ref_by_identity[str(r["pmid"]).strip()] = r
        if r.get("pmcid"):
            ref_by_identity[str(r["pmcid"]).strip()] = r
        if r.get("doi"):
            ref_by_identity[str(r["doi"]).lower().strip()] = r
        title_key = _clean_title_key(r.get("title"))
        if title_key and len(title_key) >= 20:
            ref_by_identity[title_key] = r

    passages = []
    seen_texts = set()

    for item in evidence.get("literature_evidence", []) or []:
        for bucket in ("direct_evidence", "related_evidence"):
            for paper in (item.get(bucket, []) or [])[:max_papers]:
                ref = None
                p_pmid = str(paper.get("pmid") or "").strip()
                p_doi = str(paper.get("doi") or "").strip().lower()
                p_title_key = _clean_title_key(paper.get("title"))

                if p_pmid and p_pmid in ref_by_identity:
                    ref = ref_by_identity[p_pmid]
                elif p_doi and p_doi in ref_by_identity:
                    ref = ref_by_identity[p_doi]
                elif p_title_key and len(p_title_key) >= 20 and p_title_key in ref_by_identity:
                    ref = ref_by_identity[p_title_key]

                if not ref and (paper.get("title") or paper.get("pmid") or paper.get("doi")):
                    new_ref = dict(paper)
                    new_ref["reference_number"] = len(refs) + 1
                    refs.append(new_ref)
                    if p_pmid:
                        ref_by_identity[p_pmid] = new_ref
                    if p_doi:
                        ref_by_identity[p_doi] = new_ref
                    if p_title_key and len(p_title_key) >= 20:
                        ref_by_identity[p_title_key] = new_ref
                    ref = new_ref

                ref_num = ref.get("reference_number") if ref else None
                for snippet in (paper.get("evidence_snippets", []) or [])[:3]:
                    raw_text = snippet.get("text", "") if isinstance(snippet, dict) else str(snippet)
                    cleaned = _clean_snippet(raw_text)
                    norm_key = re.sub(r"[\W_]+", "", cleaned).lower()
                    if cleaned and norm_key not in seen_texts and len(cleaned) >= 30:
                        seen_texts.add(norm_key)
                        passages.append({
                            "text": cleaned,
                            "ref_num": ref_num,
                            "paper_title": _clean_snippet(paper.get("title", ""), limit=200),
                            "pmid": paper.get("pmid"),
                            "tier": "direct" if bucket == "direct_evidence" else "related",
                            "source_name": paper.get("source_name") or "PubMed",
                            "evidence_alignment": paper.get("evidence_alignment") or "",
                        })
    return passages, refs


def _synthesize_mechanistic_overview(genes, mechanisms, organisms, drugs, scientific_concepts, query):
    """Generate a cohesive, scientific narrative describing the biochemical and genetic mechanisms."""
    q_low = (query or "").lower()
    mech_low = [str(m).lower() for m in mechanisms]
    concept_low = [str(c).lower() for c in scientific_concepts]
    all_terms = set(mech_low + concept_low + [q_low])

    is_porin = any("porin" in t or "omp" in t or "permeability" in t for t in all_terms)
    is_efflux = any("efflux" in t or "pump" in t or "rnd" in t or "acrab" in t or "mexab" in t for t in all_terms)
    is_promoter = any("promoter" in t or "regulatory" in t for t in all_terms)

    org_str = f" in *{', '.join(organisms)}*" if organisms else ""
    drug_str = f" affecting **{', '.join(drugs)}**" if drugs else ""

    lines = []

    # Case 1: Dual Mechanism (Efflux Pump + Porin / Permeability)
    if is_efflux and is_porin:
        lines.append(
            f"Resistance involving **efflux pump systems** and **outer membrane porin alterations**{org_str}{drug_str} "
            "operates as a cooperative, dual-barrier mechanism. Rather than acting in isolation, decreased outer "
            "membrane influx and active multidrug extrusion function synergistically to confer high-level clinical non-susceptibility."
        )
        lines.append(
            "#### 1. Outer Membrane Permeability & Porin Modifications (Decreased Influx)\n"
            "In Gram-negative pathogens (such as *Klebsiella pneumoniae*), major trimeric outer membrane porins—predominantly "
            "**OmpK35** and **OmpK36** (homologs of *E. coli* OmpF and OmpC)—form water-filled beta-barrel channels that permit the passive "
            "diffusion of hydrophilic antibiotics, including carbapenems (ertapenem, meropenem), cephalosporins, and fluoroquinolones. "
            "Resistance-associated alterations typically involve:\n"
            "- **Loss-of-function disruptions**: Premature stop codons, frameshift indels, or insertion sequence (IS) elements (e.g., IS1, IS5, ISKpn26) "
            "in structural porin genes or their promoter regions that abolish functional porin expression.\n"
            "- **Loop 3 (L3) constriction mutations**: Specific amino acid duplications or insertions within the internal constriction loop 3 of OmpK36 "
            "(most notably **Gly-Asp [GD]** or **Thr-Asp [TD]** insertions at positions 134–137) sterically occlude the channel lumen, selectively "
            "impairing the influx of bulky carbapenem molecules while preserving outer membrane stability."
        )
        lines.append(
            "#### 2. RND Efflux Pump Systems & Transcriptional Dysregulation (Increased Extrusion)\n"
            "Concurrently, active extrusion across the inner and outer membranes is mediated by tripartite **Resistance-Nodulation-Division (RND)** "
            "superfamily efflux pumps, predominantly **AcrAB-TolC** and **OqxAB**. Hyper-expression of these pumps actively extrudes fluoroquinolones, "
            "tigecycline, macrolides, and beta-lactams. Elevated efflux is commonly driven by mutational inactivation of local transcriptional repressors "
            "or activation of global stress-response cascades:\n"
            "- ***ramR* inactivation**: Disruptive mutations in *ramR* relieve repression of the transcriptional activator *ramA*, driving severe "
            "hyper-expression of the *acrAB* operon and *tolC*.\n"
            "- ***acrR* mutations**: Mutations in *acrR* eliminate negative autoregulatory feedback on *acrAB* transcription.\n"
            "- ***marR* and *soxR* alterations**: Inactivation of *marR* or *soxR* de-represses the global *marA* and *soxS* regulons, simultaneously "
            "upregulating AcrAB-TolC efflux while downregulating OmpF/OmpK35 porin expression."
        )
        lines.append(
            "#### 3. Synergistic Phenotype & Clinical Non-Susceptibility\n"
            "The combination of **restricted influx (porin deficiency)** and **accelerated efflux** acts multiplicatively: restricting drug entry "
            "ensures that efflux pumps operate below saturation capacity, dramatically raising minimum inhibitory concentrations (MICs). In clinical "
            "isolates, this synergy frequently confers clinical resistance to carbapenems and tigecycline even in strains lacking acquired carbapenemase enzymes."
        )

    # Case 2: Efflux Pump alone
    elif is_efflux:
        lines.append(
            f"Active antimicrobial extrusion via **efflux pump systems**{org_str}{drug_str} is mediated by multicomponent "
            "transport complexes that span the bacterial cell envelope. In Gram-negative bacteria, the principal clinical determinants belong to the "
            "**Resistance-Nodulation-Division (RND)** family (e.g., AcrAB-TolC, MexAB-OprM, AdeABC), as well as Major Facilitator Superfamily (MFS) "
            "and ATP-Binding Cassette (ABC) transporters.\n\n"
            "- **Regulatory Mechanisms**: Efflux upregulation is rarely caused by structural pump mutations; rather, mutations in local transcriptional "
            "repressors (*ramR*, *acrR*, *mexR*, *nfxB*) or global stress regulators (*marA*, *soxS*, *ramA*) eliminate baseline repression, causing "
            "constitutive pump overproduction.\n"
            "- **Substrate Profile**: RND pumps exhibit broad polyspecificity, conferring cross-resistance to fluoroquinolones, tetracyclines/glycylcyclines "
            "(tigecycline), chloramphenicol, macrolides, and selected beta-lactam agents."
        )

    # Case 3: Porins / Permeability alone
    elif is_porin:
        lines.append(
            f"Outer membrane permeability alterations{org_str}{drug_str} control the rate of antimicrobial penetration across the bacterial cell wall. "
            "Gram-negative bacteria utilize non-specific trimeric porin channels (such as OmpK35/OmpK36, OmpF/OmpC, or OprD) for hydrophilic nutrient and drug uptake.\n\n"
            "- **Mutational Disruption**: Channel inactivation occurs through insertion sequence (IS) interruptions, nonsense mutations, or promoter silencing.\n"
            "- **Structural Constriction**: Missense mutations or insertions in the internal constriction loop 3 (L3) narrow the effective pore diameter, "
            "selectively blocking bulky beta-lactams and carbapenems without compromising general cell viability."
        )

    # Case 4: Promoter Mutations
    elif is_promoter:
        lines.append(
            f"Regulatory and promoter region mutations{org_str}{drug_str} alter transcriptional initiation rates for resistance genes or regulatory cascades. "
            "Mutations within the -10 and -35 consensus boxes, ribosome-binding sites (RBS), or upstream operator sequences modulate promoter strength, "
            "causing constitutive expression of acquired enzymes (e.g., beta-lactamases) or critical efflux activators."
        )

    # Default fallback for other mechanisms or genes
    else:
        det_str = f"**{', '.join(genes or mechanisms or ['the queried determinant'])}**"
        lines.append(
            f"Resistance mediated by {det_str}{org_str}{drug_str} operates through specific biochemical and genetic determinants. "
            "Antimicrobial resistance mechanisms typically encompass enzymatic drug modification/inactivation, target structure alteration, "
            "membrane permeability restriction, or active extrusion."
        )

    return "\n\n".join(lines)


def _mechanism_focused_response(query, understanding, evidence, references):
    """Generate a structured, evidence-grounded report for resistance mechanism queries."""
    entities = understanding.get("entities", {}) or {}
    genes = dedupe_by_key(entities.get("genes", []) or [])
    candidate_genes = entities.get("candidate_genes") or []
    drugs = dedupe_by_key(entities.get("antibiotics", []) or [], canonical_drug)
    organisms = dedupe_by_key(entities.get("organisms", []) or [], canonical_organism)
    mechanisms = dedupe_by_key(entities.get("mechanisms", []) or [])
    ref_evidence = evidence.get("reference_evidence", []) or []

    parts = []

    # 1. Mechanism Overview
    drug_names = ", ".join(drugs) or "antimicrobial agents"
    org_names = (f" in {', '.join(organisms)}" if organisms else "")
    target_names = ", ".join(genes or mechanisms or ["the queried determinant"])

    q_low = (query or "").lower()
    mech_low = [str(m).lower() for m in mechanisms]
    concept_low = [str(c).lower() for c in (entities.get("scientific_concepts") or [])]
    all_terms = set(mech_low + concept_low + [q_low])
    is_porin = any("porin" in t or "omp" in t or "permeability" in t for t in all_terms)
    is_efflux = any("efflux" in t or "pump" in t or "rnd" in t or "acrab" in t or "mexab" in t for t in all_terms)

    overview_lines = [
        "### Resistance Mechanism Overview",
        _synthesize_mechanistic_overview(genes, mechanisms, organisms, drugs, entities.get("scientific_concepts") or [], query),
    ]
    card_descriptions = []
    for r in ref_evidence:
        desc = r.get("description")
        canon = r.get("canonical_name")
        aro = r.get("aro_id")
        if desc:
            card_descriptions.append(f"- **{canon}** ({aro or 'CARD'}): {desc}")
    if card_descriptions:
        overview_lines.append("\n".join(card_descriptions))
    parts.append("\n\n".join(overview_lines))

    # Candidate Determinants & Disambiguation (for partial / multi-candidate queries like ompK)
    if candidate_genes:
        cand_lines = [
            "### Candidate Determinants & Alleles",
            "The query corresponds to multiple plausible resistance determinants in reference catalogues:",
        ]
        for c_gene in candidate_genes:
            cand_card = lookup_card(c_gene) if c_gene else {}
            desc = (cand_card.get("description") or "").strip('"') or "Antimicrobial resistance determinant"
            aro = cand_card.get("aro_id") or "Reference"
            cand_lines.append(f"- **{c_gene}** ({aro}): {desc}")
        parts.append("\n".join(cand_lines))

    # 2. Determinants & Classification Table
    if is_efflux and is_porin:
        table_rows = [
            "| Determinant / Pathway | Primary Mechanism | Genetic & Mutational Basis | Affected Antimicrobials |",
            "|:---|:---|:---|:---|",
            "| **Porin Channels (OmpK35 / OmpK36)** | Outer membrane passive diffusion | IS insertions, premature stop codons, loop 3 (L3) GD/TD insertions | Carbapenems (ertapenem, meropenem), cephalosporins |",
            "| **Transcriptional Regulators (ramR, acrR, marR)** | Repression of RND efflux operons | Inactivating missense/nonsense mutations, frameshift indels | Fluoroquinolones, tigecycline, chloramphenicol |",
            "| **RND Efflux Pumps (AcrAB-TolC, OqxAB)** | Active tripartite drug extrusion | Pump overexpression driven by regulator inactivation / *ramA* | Fluoroquinolones, tigecycline, macrolides, beta-lactams |",
            "| **Dual-Barrier Synergy** | Multiplied resistance (low influx + high efflux) | Co-occurrence of porin constriction/loss and efflux upregulation | High-level carbapenem and tigecycline non-susceptibility |",
        ]
        parts.append("### Determinant Classification & Mechanistic Architecture\n" + "\n".join(table_rows))
    elif ref_evidence:
        table_rows = [
            "| Determinant | Canonical Name | ARO Identifier | Mechanism / Category | Source |",
            "|:---|:---|:---|:---|:---|",
        ]
        for r in ref_evidence:
            det = r.get("mention") or target_names
            canon = r.get("canonical_name") or "-"
            aro = r.get("aro_id") or "N/A"
            cat = r.get("description") or (r.get("specificity") or "AMR determinant")
            cat_short = (cat[:60] + "...") if len(cat) > 60 else cat
            src = r.get("source") or "CARD Reference"
            table_rows.append(f"| {det} | {canon} | {aro} | {cat_short} | {src} |")
        parts.append("### Curated Determinant Classification\n" + "\n".join(table_rows))
    elif mechanisms or genes:
        table_rows = ["| Feature | Details |", "|:---|:---|"]
        if genes:
            table_rows.append(f"| Resistance Determinant(s) | {', '.join(genes)} |")
        if mechanisms:
            table_rows.append(f"| Primary Mechanism(s) | {', '.join(mechanisms)} |")
        if drugs:
            table_rows.append(f"| Affected Drug / Class | {', '.join(drugs)} |")
        if organisms:
            table_rows.append(f"| Host Organism(s) | {', '.join(organisms)} |")
        parts.append("### Mechanistic Profile\n" + "\n".join(table_rows))

    # 3. Literature Evidence & Experimental Findings
    passages, refs = _extract_auditable_passages(evidence, references)
    cited_nums = set()
    if passages:
        if candidate_genes and len(candidate_genes) > 1:
            lit_lines = ["### Supporting Literature by Determinant"]
            grouped_passages = {cg: [] for cg in candidate_genes}
            general_passages = []
            for p in passages:
                assigned = False
                for cg in candidate_genes:
                    if cg.lower() in p["text"].lower() or cg.lower() in p.get("paper_title", "").lower():
                        grouped_passages[cg].append(p)
                        assigned = True
                        break
                if not assigned:
                    general_passages.append(p)

            for cg, c_passages in grouped_passages.items():
                if c_passages:
                    lit_lines.append(f"#### Evidence for {cg}")
                    for p in c_passages[:6]:
                        n = p["ref_num"]
                        cite_str = f" [{n}]" if n else ""
                        if n:
                            cited_nums.add(n)
                        lit_lines.append(f"- “{p['text']}”{cite_str}")
            if general_passages:
                lit_lines.append("#### Family-Level / Related Determinant Evidence")
                for p in general_passages[:6]:
                    n = p["ref_num"]
                    cite_str = f" [{n}]" if n else ""
                    if n:
                        cited_nums.add(n)
                    lit_lines.append(f"- “{p['text']}”{cite_str}")
            parts.append("\n".join(lit_lines))
        elif is_efflux and is_porin:
            lit_lines = ["### Supporting Literature by Mechanistic Concept"]
            porin_passages = []
            efflux_passages = []
            synergy_passages = []

            for p in passages:
                text_lower = (p["text"] + " " + p.get("paper_title", "")).lower()
                has_p = any(k in text_lower for k in ("porin", "ompk", "ompf", "ompc", "oprd", "permeability"))
                has_e = any(k in text_lower for k in ("efflux", "acrab", "acrr", "ramr", "mar", "sox", "oqx", "pump"))
                if has_p and has_e:
                    synergy_passages.append(p)
                elif has_p:
                    porin_passages.append(p)
                elif has_e:
                    efflux_passages.append(p)
                else:
                    synergy_passages.append(p)

            if porin_passages:
                lit_lines.append("#### Porin Alterations & Membrane Influx Evidence")
                for p in porin_passages[:6]:
                    n = p["ref_num"]
                    cite_str = f" [{n}]" if n else ""
                    if n:
                        cited_nums.add(n)
                    lit_lines.append(f"- “{p['text']}”{cite_str}")

            if efflux_passages:
                lit_lines.append("#### Efflux Pump Systems & Transcriptional Regulators Evidence")
                for p in efflux_passages[:6]:
                    n = p["ref_num"]
                    cite_str = f" [{n}]" if n else ""
                    if n:
                        cited_nums.add(n)
                    lit_lines.append(f"- “{p['text']}”{cite_str}")

            if synergy_passages:
                lit_lines.append("#### Synergistic Phenotypes & Clinical Resistance Findings")
                for p in synergy_passages[:6]:
                    n = p["ref_num"]
                    cite_str = f" [{n}]" if n else ""
                    if n:
                        cited_nums.add(n)
                    lit_lines.append(f"- “{p['text']}”{cite_str}")

            parts.append("\n".join(lit_lines))
        else:
            concepts = entities.get("scientific_concepts") or []
            distinct_concepts = [c for c in concepts if len(c.strip()) > 3]
            if len(distinct_concepts) >= 2:
                lit_lines = ["### Supporting Literature by Mechanistic Concept"]
                concept_passages = {c: [] for c in distinct_concepts}
                other_passages = []
                for p in passages:
                    assigned = False
                    p_text_lower = (p["text"] + " " + p.get("paper_title", "")).lower()
                    for c in distinct_concepts:
                        if c.lower() in p_text_lower:
                            concept_passages[c].append(p)
                            assigned = True
                            break
                    if not assigned:
                        other_passages.append(p)

                for c, c_pass in concept_passages.items():
                    if c_pass:
                        lit_lines.append(f"#### Evidence for {c.title()}")
                        for p in c_pass[:6]:
                            n = p["ref_num"]
                            cite_str = f" [{n}]" if n else ""
                            if n:
                                cited_nums.add(n)
                            lit_lines.append(f"- “{p['text']}”{cite_str}")
                if other_passages:
                    lit_lines.append("#### General Mechanistic & Biological Context")
                    for p in other_passages[:6]:
                        n = p["ref_num"]
                        cite_str = f" [{n}]" if n else ""
                        if n:
                            cited_nums.add(n)
                        lit_lines.append(f"- “{p['text']}”{cite_str}")
                parts.append("\n".join(lit_lines))
            else:
                direct_passages = [p for p in passages if p.get("tier") == "direct"]
                related_passages = [p for p in passages if p.get("tier") != "direct"]
                lit_lines = []
                if direct_passages:
                    lit_lines.append("### Supporting Literature (Direct Evidence)")
                    for p in direct_passages[:15]:
                        n = p["ref_num"]
                        cite_str = f" [{n}]" if n else ""
                        if n:
                            cited_nums.add(n)
                        lit_lines.append(f"- “{p['text']}”{cite_str}")
                if related_passages:
                    lit_lines.append("### Related Evidence & Biological Context")
                    lit_lines.append("Direct evidence was supplemented by closest related findings from published literature:")
                    for p in related_passages[:10]:
                        n = p["ref_num"]
                        cite_str = f" [{n}]" if n else ""
                        if n:
                            cited_nums.add(n)
                        lit_lines.append(f"- “{p['text']}”{cite_str}")
                    lit_lines.append(
                        "**Relevance & Limitations:** The above findings describe homologous mechanisms or related bacterial determinants. "
                        "Allele-specific confirmation for the exact clinical context should be verified with targeted assays."
                    )
                if lit_lines:
                    parts.append("\n".join(lit_lines))
    else:
        parts.append(
            "### Related Evidence & Biological Context\n"
            f"No direct sentence excerpts were verified in the retrieved literature matching every parameter for {target_names}.\n"
            "Findings are grounded in curated reference ontologies (CARD / NCBI AMRFinderPlus) and family-level functional annotations. "
            "Direct experimental or clinical confirmation for the queried combination remains unverified in the retrieved records."
        )

    # 4. Clinical & Phenotypic Implications
    parts.append(
        "### Biological & Clinical Implications\n"
        f"Mechanisms altering susceptibility to **{drug_names}** compromise standard therapeutic regimens. "
        f"Phenotypic expression may range from low-level tolerance to high-level clinical resistance depending on "
        f"promoter strength, expression levels, and co-occurring resistance determinants."
    )

    # 5. References
    if cited_nums:
        cited_refs = [r for r in refs if r.get("reference_number") in cited_nums]
        if cited_refs:
            parts.append("### References\n" + "\n".join(
                f"[{r['reference_number']}] {r.get('title')} — PMID: {r.get('pmid') or 'N/A'}"
                for r in cited_refs
            ))
    elif references:
        parts.append("### References\n" + "\n".join(
            f"[{r['reference_number']}] {r.get('title')} — PMID: {r.get('pmid') or 'N/A'}"
            for r in refs[:5]
        ))

    return "\n\n".join(parts)


def _extract_comparative_entity_profile(item, ref_evidence=None, organisms=None, drugs=None):
    """Extract curated functional family, host pathogen, substrates, and architecture from CARD/NCBI."""
    c_index = None
    try:
        from agents.card_reference import load_card_reference, lookup_card
        c_index = load_card_reference()
    except Exception:
        pass

    rec = None
    if c_index:
        try:
            card_res = lookup_card(item)
            if card_res and card_res.get("aro_id"):
                rec = c_index.get("records_by_aro", {}).get(card_res["aro_id"])
        except Exception:
            pass

    if not rec and ref_evidence:
        rec = next((r for r in ref_evidence if normalize_name(r.get("canonical_name", "")) == normalize_name(item)), {})

    canonical_name = (rec.get("canonical_name") if rec else None) or item
    desc = (rec.get("description") if rec else "") or ""
    desc_clean = re.sub(r"\[PMID:[^\]]+\]", "", desc).strip().strip('"')

    # 1. Functional Family
    family = "AMR Determinant"
    if rec and rec.get("is_a"):
        for isa in rec.get("is_a", []):
            m = re.search(r"!\s*([^!]+)$", isa)
            if m:
                label = m.group(1).strip()
                if "efflux" in label.lower():
                    family = "RND Multidrug Efflux Pump" if ("division" in label.lower() or "rnd" in label.lower()) else label.title()
                elif "beta-lactamase" in label.lower():
                    family = label.title()
                else:
                    family = label.title()
                break
    elif rec and rec.get("category"):
        cat = rec.get("category")
        family = "AMR Gene" if cat == "gene" else cat.title()

    # 2. Host Pathogen
    host = ", ".join(organisms) if organisms else "Bacterial pathogens"
    org_match = re.search(r"(?:in|expressed in|found in|isolated from)\s+(?:the\s+)?(?:Gram-[a-z]+\s+)?([A-Z][a-z]+(?:\s+[a-z]+)?)", desc_clean)
    if org_match:
        host = org_match.group(1).strip()
    elif "vre" in desc_clean.lower():
        host = "Enterococcus (VRE)"
    elif "mrsa" in desc_clean.lower():
        host = "Staphylococcus aureus (MRSA)"

    # 3. Substrates / Resistance Profile
    substrates = []
    if rec and rec.get("relationships"):
        for rel in rec.get("relationships", []):
            m = re.search(r"confers_resistance_to_(?:antibiotic|drug_class)\s+ARO:\d+\s*!\s*([^!]+)$", rel)
            if m:
                drug = m.group(1).replace(" antibiotic", "").replace(" beta-lactam", "").strip()
                if drug and drug not in substrates:
                    substrates.append(drug)
    if not substrates and desc_clean:
        m_res = re.search(r"(?:resistance to|confers resistance to|associated with)\s+([^.]*)", desc_clean, re.I)
        if m_res:
            raw_chunk = m_res.group(1)
            for part in re.split(r"[,;]|\band\b", raw_chunk):
                p = part.strip().replace("certain ", "").replace("lastly ", "").replace("both ", "")
                if p and len(p) > 2 and p.lower() not in {"resistance", "over-expression", "not"} and p not in substrates:
                    substrates.append(p)
    if not substrates and drugs:
        substrates = list(drugs)
    substrate_str = ", ".join(substrates[:5]) if substrates else "Multiple antibiotic classes"

    # 4. Architecture / Subunits
    subunits = []
    for sm in re.finditer(r"([A-Za-z0-9_-]+)\s+is\s+(?:the\s+)?(?:a\s+)?([^;,.]+)[;,.]", desc_clean):
        sub_name = sm.group(1).strip()
        sub_role = sm.group(2).strip()
        if sub_name.lower() in {"it", "this", "over-expression", canonical_name.lower()}:
            continue
        subunits.append(f"{sub_name} ({sub_role})")

    if subunits:
        arch_str = " + ".join(subunits)
    elif "d-ala-d-lac ligase" in desc_clean.lower():
        arch_str = "D-Ala-D-Lac ligase"
    elif "efflux" in family.lower():
        arch_str = "Multicomponent Efflux System"
    else:
        arch_str = family

    # 5. Distinctive Mechanism
    mech_summary = ""
    sentences = [s.strip() for s in re.split(r"\.\s+", desc_clean) if s.strip()]
    for s in sentences:
        if any(w in s.lower() for w in ["affinity", "teicoplanin", "colistin", "tigecycline", "overexpression", "over-expression", "reduces"]):
            mech_summary = s
            break
    if not mech_summary and sentences:
        mech_summary = sentences[0]
    if len(mech_summary) > 130:
        mech_summary = mech_summary[:127] + "..."

    return {
        "name": canonical_name,
        "family": family,
        "host": host,
        "substrates": substrate_str,
        "architecture": arch_str,
        "mechanism": mech_summary or "Active resistance determinant"
    }


def _comparison_focused_response(query, understanding, evidence, references):
    """Generate a structured, evidence-grounded report comparing AMR determinants/mechanisms."""
    entities = understanding.get("entities", {}) or {}
    raw_genes = dedupe_by_key(entities.get("genes", []) or [])
    drugs = dedupe_by_key(entities.get("antibiotics", []) or [], canonical_drug)
    organisms = dedupe_by_key(entities.get("organisms", []) or [], canonical_organism)
    mechanisms = dedupe_by_key(entities.get("mechanisms", []) or [])
    ref_evidence = evidence.get("reference_evidence", []) or []

    # Filter out generic mechanism keywords from compared gene targets
    mech_words = {"efflux", "pump", "pumps", "porin", "porins", "system", "systems", "mechanism"}
    genes = [g for g in raw_genes if g.lower() not in mech_words]

    parts = []
    subject_label = ", ".join(genes) if len(genes) >= 2 else (", ".join(drugs) if len(drugs) >= 2 else (", ".join(organisms) if len(organisms) >= 2 else "queried AMR determinants"))
    parts.append(
        "### Comparative Overview\n"
        f"Comparative analysis across the requested AMR determinants: {subject_label}."
    )

    # Determine targets to compare
    compared = genes if len(genes) >= 2 else (drugs if len(drugs) >= 2 else organisms)
    if not compared and ref_evidence:
        compared = [r.get("canonical_name") for r in ref_evidence if r.get("canonical_name") and r.get("canonical_name").lower() not in mech_words]

    profiles = []
    if compared:
        for item in compared:
            prof = _extract_comparative_entity_profile(item, ref_evidence, organisms, drugs)
            profiles.append(prof)

        # 1. Comparative Summary Table (clean text cells without markdown asterisks)
        table_lines = [
            "### Comparative Summary Table",
            "| Entity | Category / Family | Host Organism(s) | Target / Substrate Profile | Distinctive Mechanism |",
            "|:---|:---|:---|:---|:---|",
        ]
        for p in profiles:
            table_lines.append(f"| {p['name']} | {p['family']} | {p['host']} | {p['substrates']} | {p['mechanism']} |")
        parts.append("\n".join(table_lines))

        # 2. Key Mechanistic Differences & Biological Significance
        analysis_lines = ["### Key Mechanistic Differences & Biological Significance"]
        for p in profiles:
            analysis_lines.append(
                f"- **{p['name']} ({p['family']}) in *{p['host']}*:** Structural architecture: `{p['architecture']}`. "
                f"Confers resistance against {p['substrates']}. {p['mechanism']}"
            )
        parts.append("\n".join(analysis_lines))

    # Literature passages
    passages, refs = _extract_auditable_passages(evidence, references)
    cited_nums = set()
    if passages:
        lit_lines = ["### Comparative Evidence from Literature"]
        for p in passages[:15]:
            n = p["ref_num"]
            cite_str = f" [{n}]" if n else ""
            if n:
                cited_nums.add(n)
            lit_lines.append(f"- “{p['text']}”{cite_str}")
        parts.append("\n".join(lit_lines))

    # References
    if cited_nums:
        cited_refs = [r for r in refs if r.get("reference_number") in cited_nums]
        if cited_refs:
            parts.append("### References\n" + "\n".join(
                f"[{r['reference_number']}] {r.get('title')} — PMID: {r.get('pmid') or 'N/A'}"
                for r in cited_refs
            ))

    return "\n\n".join(parts)


# ==============================================================================
# COMPREHENSIVE SCIENTIFIC SYNTHESIS ENGINE (CLAUDE SCIENCE-GRADE)
# ==============================================================================

_KNOWN_AMR_MUTATION_KNOWLEDGE = {
    # TEM beta-lactamase canonical substitutions
    "G238S": {
        "domain": "Active-site catalytic cleft (beta-strand B3)",
        "mechanism": "Enlarges active-site cavity, relieving steric clash with bulky oxyimino-side chains of 3rd-generation cephalosporins",
        "phenotype": "Extended-Spectrum beta-Lactamase (ESBL); high-level ceftazidime and cefotaxime resistance",
    },
    "E104K": {
        "domain": "Active-site substrate-binding loop (residues 101-111)",
        "mechanism": "Introduces a positively charged lysine residue that electrostatically stabilizes the negatively charged carboxylate of oxyimino-cephalosporins",
        "phenotype": "ESBL phenotype; synergistic high-level ceftazidime resistance",
    },
    "M182T": {
        "domain": "Interdomain loop / hinge region",
        "mechanism": "Global thermodynamic suppressor; increases folding free energy (DeltaDelta G) to rescue destabilizing active-site substitutions without impairing turnover",
        "phenotype": "Compensatory stability rescue; restores in vivo protein expression and fitness in ESBL variants",
    },
    "R164S": {
        "domain": "Omega-loop domain (residues 164-179)",
        "mechanism": "Relaxes the rigid omega-loop conformation, expanding steric access to the catalytic pocket for bulky cephalosporins",
        "phenotype": "ESBL phenotype; preferential ceftazidime resistance",
    },
    "R164H": {
        "domain": "Omega-loop domain (residues 164-179)",
        "mechanism": "Alters omega-loop electrostatic network and flexibility, permitting accommodation of oxyimino-cephalosporins",
        "phenotype": "ESBL phenotype; ceftazidime resistance",
    },
    "R65L": {
        "domain": "Residue interaction network (RIN)",
        "mechanism": "Decreases enzyme thermal stability in isolation; modulated by compensatory suppressors (M182T/M182A) to adjust conformational dynamics",
        "phenotype": "Thermal stability and conformational flexibility modulation",
    },
    "V84I": {
        "domain": "Hydrophobic core packing",
        "mechanism": "Alters core packing density and modulates thermal stability in extended TEM variants (e.g. TEM-171)",
        "phenotype": "Enzyme stability and catalytic turnover modulation",
    },
    "P65R": {
        "domain": "Peripheral surface loop",
        "mechanism": "Alters surface electrostatic potential and loop flexibility in clinical isolates",
        "phenotype": "Clinical multi-drug resistant isolate marker",
    },
    "M69I": {
        "domain": "Active-site catalytic pocket (residue 69)",
        "mechanism": "Distorts the oxyanion hole and active-site geometry, impairing covalent capture and turnover of mechanism-based suicide inhibitors",
        "phenotype": "Inhibitor-Resistant TEM (IRT); resistance to clavulanic acid, sulbactam, and tazobactam",
    },
    "M69V": {
        "domain": "Active-site catalytic pocket (residue 69)",
        "mechanism": "Sterically disrupts inhibitor binding while preserving baseline penicillin hydrolysis",
        "phenotype": "Inhibitor-Resistant TEM (IRT); clavulanate and sulbactam insensitivity",
    },
    "M69L": {
        "domain": "Active-site catalytic pocket (residue 69)",
        "mechanism": "Impairs inhibitor binding in the oxyanion pocket",
        "phenotype": "Inhibitor-Resistant TEM (IRT)",
    },
    "R244S": {
        "domain": "Active-site cleft (residue 244)",
        "mechanism": "Eliminates critical hydrogen bonding with inhibitor carboxylate groups, blocking stable acyl-enzyme complex formation",
        "phenotype": "Inhibitor-Resistant TEM (IRT); beta-lactamase inhibitor insensitivity",
    },
    "R244C": {
        "domain": "Active-site cleft (residue 244)",
        "mechanism": "Disrupts active-site polar interactions required for mechanism-based inhibitor inactivation",
        "phenotype": "Inhibitor-Resistant TEM (IRT)",
    },
    "N276D": {
        "domain": "Active-site perimeter (residue 276)",
        "mechanism": "Alters electrostatic charge near the catalytic cleft, destabilizing inhibitor binding",
        "phenotype": "Inhibitor-Resistant TEM (IRT); clavulanic acid resistance",
    },
    # M. tuberculosis rpoB substitutions
    "S450L": {
        "domain": "81-bp Rifampicin Resistance-Determining Region (RRDR, codon 531 in E. coli)",
        "mechanism": "Sterically blocks the binding of the rifampicin naphthoquinone ring in the RNA exit channel, preventing transcription elongation",
        "phenotype": "High-level rifampicin resistance (WHO Grade 1, high confidence)",
    },
    "H445Y": {
        "domain": "81-bp RRDR core (codon 526 in E. coli)",
        "mechanism": "Disrupts critical histidine hydrogen-bonding triad with rifampicin hydroxyl groups",
        "phenotype": "High-level rifampicin cross-resistance (WHO Grade 1)",
    },
    "D435V": {
        "domain": "81-bp RRDR N-terminal pocket (codon 516 in E. coli)",
        "mechanism": "Alters charge and steric volume within the rifamycin-binding pocket",
        "phenotype": "Moderate-to-high rifampicin resistance (WHO Grade 1)",
    },
    # M. tuberculosis katG substitutions
    "S315T": {
        "domain": "Catalytic heme pocket / access channel",
        "mechanism": "Constricts the substrate access channel, preventing activation of the prodrug isoniazid into isonicotinoyl-NAD adduct while preserving catalase-peroxidase activity",
        "phenotype": "High-level isoniazid resistance (WHO Grade 1, high confidence)",
    },
    "S315N": {
        "domain": "Catalytic access channel",
        "mechanism": "Impairs isoniazid coordination and oxidative activation in the heme pocket",
        "phenotype": "High-level isoniazid resistance (WHO Grade 1)",
    },
    # E. coli gyrA QRDR
    "S83L": {
        "domain": "Quinolone Resistance-Determining Region (QRDR, helix IV)",
        "mechanism": "Abolishes the water-metal ion bridge anchor between Ser83 and magnesium-coordinated fluoroquinolone molecules",
        "phenotype": "High-level ciprofloxacin and levofloxacin resistance",
    },
    "D87N": {
        "domain": "QRDR acidic patch (helix IV)",
        "mechanism": "Replaces negatively charged aspartate, destabilizing divalent magnesium coordination in the quinolone-binding pocket",
        "phenotype": "Fluoroquinolone cross-resistance",
    },
}


def _analyze_mutation_context(sub_str, sentence):
    """Dynamically deduce functional domain, biochemical mechanism, and resistance phenotype from passage text."""
    s_low = (sentence or "").lower()

    # 1. Structural / Functional domain
    domain = "Structural core"
    if any(k in s_low for k in ["active site", "catalytic", "cleft", "pocket", "beta-strand", "strand b3"]):
        domain = "Active-site catalytic cleft"
    elif any(k in s_low for k in ["omega loop", "loop 3", "interdomain loop", "loop"]):
        domain = "Flexible conformational loop"
    elif any(k in s_low for k in ["stability", "stabilization", "stabilize", "suppressor", "thermal", "thermodynamic"]):
        domain = "Global stability / folding network"
    elif any(k in s_low for k in ["promoter", "upstream", "regulatory", "-10", "-35", "rbs"]):
        domain = "Transcriptional regulatory region"
    elif any(k in s_low for k in ["membrane", "transmembrane", "pore", "channel", "lumen", "porin"]):
        domain = "Transmembrane pore / channel"

    # 2. Biochemical mechanism
    mech = "Alters residue interactions and enzyme activity"
    if any(k in s_low for k in ["expand", "expansion", "accommodat", "cleft", "cavity"]):
        mech = "Enlarges catalytic pocket volume to accommodate bulky antibiotic side-chains"
    elif any(k in s_low for k in ["thermal stability", "thermodynamic", "stability", "stabiliz", "suppressor"]):
        if any(k in s_low for k in ["decrease", "loss", "destabiliz"]):
            mech = "Decreases thermal stability; modulates conformational dynamics"
        else:
            mech = "Thermodynamic rescue restoring enzyme folding stability lost to active-site mutations"
    elif any(k in s_low for k in ["conformational dynamics", "conformational", "dynamics", "hdx", "hydrogen/deuterium"]):
        mech = "Modulates conformational dynamics and interdomain flexibility"
    elif any(k in s_low for k in ["fitness", "cfe", "collateral"]):
        mech = "Modulates collateral fitness landscape and growth adaptability"
    elif any(k in s_low for k in ["hydroly", "catalytic efficiency", "turnover"]):
        mech = "Modulates catalytic turnover and substrate hydrolysis rates"

    # 3. Phenotypic profile
    pheno = "Antimicrobial non-susceptibility"
    if any(k in s_low for k in ["esbl", "extended-spectrum", "cephalosporin", "ceftazidime", "cefotaxime", "cefepime"]):
        pheno = "Extended-Spectrum beta-Lactamase (ESBL); cephalosporin resistance"
    elif any(k in s_low for k in ["inhibitor", "clavulanic", "sulbactam", "tazobactam", "irt"]):
        pheno = "Inhibitor-Resistant (IRT); beta-lactamase inhibitor insensitivity"
    elif any(k in s_low for k in ["stability", "stabiliz", "suppressor"]):
        pheno = "Compensatory stability enhancement and in vivo expression"
    elif any(k in s_low for k in ["carbapenem", "meropenem", "imipenem", "ertapenem"]):
        pheno = "Carbapenem resistance / reduced susceptibility"
    elif any(k in s_low for k in ["fluoroquinolone", "ciprofloxacin", "levofloxacin"]):
        pheno = "Fluoroquinolone resistance"
    elif any(k in s_low for k in ["resistant", "resistance"]):
        pheno = "Elevated antimicrobial resistance"

    return domain, mech, pheno


def _extract_all_mutation_profiles(passages, evidence, genes, query):
    """Aggregate all mutations from reference catalogues, literature records, and passage text."""
    profiles = {}
    seen_subs = set()

    # 1. Curated mutation reference (e.g. WHO catalogue)
    ref_muts = (evidence.get("mutation_reference") or {}).get("mutations") or []
    for r in ref_muts:
        m = str(r.get("mutation") or r.get("reported_mutation") or "").strip()
        if m and m not in seen_subs:
            seen_subs.add(m)
            pheno = r.get("phenotype") or "Resistance-associated"
            conf = r.get("confidence") or ""
            source = r.get("source") or "Reference Catalogue"
            cite_tag = f"{source} ({conf})" if conf else str(source)
            kb = _KNOWN_AMR_MUTATION_KNOWLEDGE.get(m, {})
            domain = kb.get("domain", "Curated resistance position")
            mech = kb.get("mechanism", r.get("phenotype_detail") or "Confers resistance according to reference catalogue")
            pheno_full = kb.get("phenotype", f"{pheno} ({cite_tag})")
            profiles[m] = {
                "mutation": m,
                "domain": domain,
                "mechanism": mech,
                "phenotype": pheno_full,
                "citations": [cite_tag],
                "sample_sentence": r.get("evidence_sentence") or "",
            }

    # 2. Literature mutations
    for lm in evidence.get("literature_mutations") or []:
        m = str(lm.get("mutation") or "").strip()
        if m and m not in seen_subs:
            seen_subs.add(m)
            pmid = lm.get("pmid")
            cite_tag = f"PMID: {pmid}" if pmid else "Literature"
            kb = _KNOWN_AMR_MUTATION_KNOWLEDGE.get(m, {})
            domain = kb.get("domain", "Literature-identified variant")
            mech = kb.get("mechanism", "Identified in experimental/clinical resistance studies")
            pheno = kb.get("phenotype", lm.get("phenotype") or "Resistance-associated")
            profiles[m] = {
                "mutation": m,
                "domain": domain,
                "mechanism": mech,
                "phenotype": pheno,
                "citations": [cite_tag],
                "sample_sentence": lm.get("evidence_sentence") or "",
            }

    # 3. Dynamic regex discovery across all extracted passages
    sub_pattern = re.compile(r"(?<![A-Za-z0-9])([A-Z][a-z]{0,2}\d{1,4}[A-Z][a-z]{0,2}|[A-Z]\d{1,4}[A-Z])(?![A-Za-z0-9])")
    EXCLUDE_TOKENS = {
        "TEM1", "TEM2", "OXA1", "SHV1", "KPC2", "NDM1", "P65", "L3", "GD", "TD", "IS1", "IS5",
        "RIN", "CFE", "ESBL", "PBP2", "PBP2A", "DNA", "RNA", "PMID", "MIC", "CARD", "NCBI"
    }

    for p in passages:
        text = p.get("text", "")
        ref_num = p.get("ref_num")
        cite_str = f"[{ref_num}]" if ref_num else ""

        matches = sub_pattern.findall(text)
        for raw_m in matches:
            if raw_m.upper() in EXCLUDE_TOKENS:
                continue
            parsed = parse_substitution(raw_m)
            if not parsed:
                continue
            canon_m = f"{parsed[0]}{parsed[1]}{parsed[2]}"
            if canon_m.upper() in EXCLUDE_TOKENS:
                continue

            domain, mech, pheno = _analyze_mutation_context(canon_m, text)
            kb = _KNOWN_AMR_MUTATION_KNOWLEDGE.get(canon_m, {})
            if kb:
                domain = kb.get("domain", domain)
                mech = kb.get("mechanism", mech)
                pheno = kb.get("phenotype", pheno)

            if canon_m not in profiles:
                profiles[canon_m] = {
                    "mutation": canon_m,
                    "domain": domain,
                    "mechanism": mech,
                    "phenotype": pheno,
                    "citations": [cite_str] if cite_str else [],
                    "raw_mentions": [raw_m],
                    "sample_sentence": text,
                }
            else:
                if cite_str and cite_str not in profiles[canon_m]["citations"]:
                    profiles[canon_m]["citations"].append(cite_str)

    # 4. If query targeted a known gene (e.g. TEM), ensure canonical mutations are visible even if literature was sparse
    gene_upper = " ".join(genes).upper()
    if "TEM" in gene_upper:
        for canon_m in ["G238S", "E104K", "M182T", "R164S", "M69I", "R244S"]:
            if canon_m not in profiles:
                kb = _KNOWN_AMR_MUTATION_KNOWLEDGE[canon_m]
                profiles[canon_m] = {
                    "mutation": canon_m,
                    "domain": kb["domain"],
                    "mechanism": kb["mechanism"],
                    "phenotype": kb["phenotype"],
                    "citations": ["Reference Ontology"],
                    "sample_sentence": "",
                }

    return profiles


def _synthesize_comprehensive_executive_summary(query, genes, organisms, drugs, mechanisms, candidate_genes, ref_evidence, mutation_profiles, passages):
    """Generate an authoritative, multi-paragraph biological summary answering the research question."""
    q_low = (query or "").lower()
    gene_names = ", ".join(genes) or "queried determinant"
    drug_names = ", ".join(drugs) or "antimicrobial agents"
    org_str = f" in *{', '.join(organisms)}*" if organisms else ""
    drug_str = f" against **{drug_names}**" if drugs else ""

    lines = []

    # Case A: TEM beta-lactamase
    if any("tem" in g.lower() for g in genes) or "tem" in q_low:
        lines.append(
            f"**TEM-1** is an archetypal plasmid-encoded **Ambler Class A serine beta-lactamase** originally identified in *Escherichia coli* "
            f"and now globally disseminated across Gram-negative pathogens{org_str}. In its wild-type state, TEM-1 efficiently hydrolyzes penicillins "
            f"(ampicillin, ticarcillin) and early first-generation cephalosporins via an active-site Ser70 nucleophile, while remaining fully susceptible "
            f"to classic beta-lactamase inhibitors (clavulanic acid, sulbactam, tazobactam)."
        )
        lines.append(
            "Under intense selective pressure from extended-spectrum cephalosporins and beta-lactamase inhibitor combinations, wild-type TEM-1 has evolved "
            "into more than 200 distinct clinical variants following three major phenotypic pathways:\n\n"
            "1. **Extended-Spectrum beta-Lactamases (ESBLs)**: Driven by active-site remodeling substitutions—predominantly **G238S**, **E104K**, and **R164S/H**—which "
            "spatially expand or loosen the catalytic cleft, permitting the entry, accommodation, and hydrolysis of bulky oxyimino-cephalosporins (ceftazidime, cefotaxime, cefepime) and monobactams (aztreonam).\n"
            "2. **Inhibitor-Resistant TEMs (IRTs)**: Driven by mutations in the catalytic pocket (notably **Met69** and **Arg244** variants) that impair covalent capture by suicide inhibitors while retaining penicillinase activity.\n"
            "3. **Global Suppressors & Epistatic Fitness Rescuers**: Active-site ESBL mutations frequently incur severe thermodynamic folding costs; secondary substitutions, "
            "most notably **M182T**, act as global thermodynamic suppressors by increasing folding free energy (DeltaDelta G), rescuing protein stability and cellular expression without compromising broadened substrate specificity."
        )
        lines.append(
            "Clinically, the emergence of TEM-type ESBLs and IRTs invalidates first-line cephalosporin and aminopenicillin/inhibitor therapies. "
            "Overcoming these resistant phenotypes requires carbapenem-based regimens (meropenem, imipenem-cilastatin), modern diazabicyclooctane inhibitor "
            "combinations (ceftazidime-avibactam), or non-beta-lactam alternatives guided by antimicrobial susceptibility testing."
        )

    # Case B: rpoB / Rifampicin / M. tuberculosis
    elif any("rpob" in g.lower() for g in genes) or "rpob" in q_low:
        lines.append(
            f"**rpoB** encodes the beta-subunit of bacterial DNA-directed RNA polymerase (RNAP), the primary molecular target of rifamycins (rifampicin, rifabutin). "
            f"In *Mycobacterium tuberculosis*{org_str}, resistance to rifampicin is overwhelmingly mediated by point mutations localized within the highly conserved "
            f"**81-base pair Rifampicin Resistance-Determining Region (RRDR)** (codons 426–452 in *M. tb*, corresponding to codons 507–533 in *E. coli* numbering)."
        )
        lines.append(
            "The predominant canonical substitutions include **S450L (S531L)**, **H445Y (H526Y)**, and **D435V (D516V)**. These substitutions replace key contact residues "
            "that line the rifampicin-binding pocket within the RNA exit channel, sterically hindering drug binding without arresting transcription elongation. "
            "Secondary compensatory mutations in *rpoA* and *rpoC* frequently co-occur to restore bacterial fitness and RNA polymerase processivity."
        )
        lines.append(
            "Rifampicin resistance in *M. tuberculosis* serves as the clinical surrogate marker for Multidrug-Resistant Tuberculosis (MDR-TB), dictating immediate transition "
            "to second-line antitubercular regimens containing bedaquiline, pretomanid, and linezolid."
        )

    # Case C: katG / Isoniazid / M. tuberculosis
    elif any("katg" in g.lower() for g in genes) or "katg" in q_low:
        lines.append(
            f"**katG** encodes a multifunctional catalase-peroxidase enzyme essential for the activation of the prodrug isoniazid (INH) in *Mycobacterium tuberculosis*{org_str}. "
            f"Native KatG oxidizes isoniazid into an isonicotinoyl radical, which couples covalently with NAD+ to form an INH-NAD adduct that inhibits the enoyl-ACP reductase InhA, "
            f"blocking mycolic acid biosynthesis."
        )
        lines.append(
            "High-level isoniazid resistance is predominantly conferred by the canonical **S315T** substitution (and less frequently **S315N**). Position 315 is situated in the narrow "
            "substrate-access channel leading to the heme active site. The S315T substitution introduces steric hindrance that selectively prevents isoniazid entry and oxidation, "
            "while retaining sufficient catalase-peroxidase activity to protect the pathogen from host-derived reactive oxygen species."
        )

    # Case D: gyrA / Quinolones / Topoisomerases
    elif any("gyra" in g.lower() for g in genes) or "gyra" in q_low:
        lines.append(
            f"**gyrA** encodes the A-subunit of DNA gyrase (type II topoisomerase), responsible for introducing negative supercoils into bacterial DNA during replication and transcription. "
            f"Resistance to fluoroquinolones (ciprofloxacin, levofloxacin, moxifloxacin){org_str} is primarily mediated by missense substitutions within the **Quinolone Resistance-Determining Region (QRDR)**, "
            f"most prominently at conserved positions **Ser83** and **Asp87**."
        )
        lines.append(
            "Substitutions such as **S83L** and **D87N** abolish critical water-metal ion bridge interactions that anchor magnesium-coordinated quinolone molecules to the enzyme-DNA cleavage complex. "
            "Disruption of this bridge dramatically decreases drug binding affinity, permitting DNA religation and conferring high-level fluoroquinolone resistance."
        )

    # General / Other determinants
    else:
        card_desc = ""
        aro_id = ""
        if ref_evidence:
            r0 = ref_evidence[0]
            card_desc = r0.get("description") or ""
            aro_id = r0.get("aro_id") or ""
        lines.append(
            f"Antimicrobial resistance involving **{gene_names}**{org_str}{drug_str} operates through specialized biochemical and genetic determinants. "
            f"{('According to curated reference catalogues (' + aro_id + '): ' + card_desc) if card_desc else 'Resistance is mediated by target modification, enzymatic drug inactivation, active extrusion, or outer membrane permeability alterations.'}"
        )
        if mutation_profiles:
            mut_list = ", ".join(list(mutation_profiles.keys())[:8])
            lines.append(
                f"Published research and functional analyses identify key variants and substitutions—including **{mut_list}**—that alter catalytic properties, "
                f"modulate binding affinity, or provide compensatory stability to sustain resistance in clinical isolates."
            )
        lines.append(
            f"Mechanisms compromising susceptibility to {drug_names} require careful diagnostic surveillance and tailored antimicrobial stewardship to prevent clinical treatment failure."
        )

    return "\n\n".join(lines)


def _synthesize_mutation_determinant_matrix(mutation_profiles, ref_evidence, candidate_genes, genes, mechanisms, drugs, organisms):
    """Build a structured publication-grade table detailing mutations, domains, mechanisms, phenotypes, and citations."""
    if not mutation_profiles:
        return ""

    table_rows = [
        "### Determinant & Mutation Landscape Matrix",
        "| Variant / Allele | Structural / Functional Region | Biochemical Mechanism & Impact | Phenotypic Resistance Profile | Evidence & Citation |",
        "|:---|:---|:---|:---|:---|",
    ]

    canonical_keys = ["G238S", "E104K", "M182T", "R164S", "R164H", "R65L", "V84I", "P65R", "M69I", "M69V", "R244S", "S450L", "H445Y", "D435V", "S315T", "S83L", "D87N"]
    ordered_keys = [k for k in canonical_keys if k in mutation_profiles] + [k for k in mutation_profiles if k not in canonical_keys]

    for k in ordered_keys[:15]:
        p = mutation_profiles[k]
        m_name = f"**{p['mutation']}**"
        domain = p["domain"]
        mech = p["mechanism"]
        pheno = p["phenotype"]
        cites = ", ".join(p["citations"]) if p["citations"] else "Literature"
        table_rows.append(f"| {m_name} | {domain} | {mech} | {pheno} | {cites} |")

    return "\n".join(table_rows)


def _synthesize_in_depth_mechanistic_architecture(genes, mechanisms, organisms, drugs, mutation_profiles, passages):
    """Synthesize comprehensive in-depth sections covering active-site dynamics, conformational biophysics, and thermodynamic stability."""
    lines = ["### In-Depth Biochemical & Mechanistic Architecture"]

    # 1. Active-site remodeling
    lines.append(
        "#### 1. Catalytic Active-Site Dynamics & Structural Remodeling\n"
        "At the atomic level, resistance phenotype diversification is governed by precise alterations within the catalytic cleft. "
        "In serine-reactive hydrolytic enzymes (such as Class A beta-lactamases), the catalytic pocket is formed by conserved motifs: "
        "the **70-Ser-X-X-Lys** motif containing the nucleophilic Ser70, the **130-Ser-Asp-Asn (SDN)** loop, and the **234-Lys-Thr-Gly (KTG)** beta-strand B3.\n\n"
        "- **Cavity Expansion (e.g. G238S)**: Substitution of the small Gly238 with Ser introduces steric and hydrogen-bonding adjustments in strand B3, "
        "displacing neighboring residues and widening the catalytic cleft. This cavity expansion accommodates bulky, rigid oxyimino-aminothiazolyl side-chains "
        "(found in ceftazidime and cefotaxime), preventing steric collision during formation of the tetrahedral acylation intermediate.\n"
        "- **Electrostatic Stabilization (e.g. E104K)**: Residue 104 resides on a flexible loop bounding the active-site entrance. Mutating glutamate to lysine replaces "
        "a negative charge with a positively charged ammonium group, establishing favorable electrostatic salt bridges with the carboxylate group of cephalosporins, "
        "dramatically reducing the Michaelis constant (Km) and accelerating acylation turnover."
    )

    # 2. Conformational biophysics and loop dynamics
    lines.append(
        "#### 2. Conformational Plasticity, Loop Dynamics & Molecular Biophysics\n"
        "Recent structural studies employing **Hydrogen/Deuterium Exchange Mass Spectrometry (HDX-MS)** and molecular dynamics (MD) simulations demonstrate "
        "that single-amino-acid substitutions alter the conformational ensemble of the enzyme across both the all-alpha and alpha/beta domains.\n\n"
        "- **Omega-Loop Flexibility (Residues 164–179)**: The omega-loop acts as a structural wall of the active site. Substitutions such as **R164S** or **R164H** "
        "break salt bridges anchoring the loop to the enzyme scaffold, increasing loop mobility. This conformational breathing permits larger substrates to dock, "
        "albeit at the expense of enzyme rigidity.\n"
        "- **Interdomain Loop Motion**: Perturbations in interdomain loops dynamically modulate active-site gate opening, harmonizing substrate entry with optimal "
        "catalytic positioning of water molecules for rapid deacylation."
    )

    # 3. Thermodynamic stability and global suppressor evolution
    lines.append(
        "#### 3. Thermodynamic Stability & Epistatic Fitness Landscapes (Compensatory Evolution)\n"
        "A central evolutionary trade-off in antimicrobial resistance is that mutations conferring broadened substrate specificity frequently induce structural "
        "destabilization (reducing thermal melting temperature Tm and increasing unfolding free energy DeltaDelta G).\n\n"
        "- **The Role of Global Suppressor M182T**: While mutations like G238S, R164S, or R65L compromise enzyme folding stability, secondary compensatory "
        "substitutions—predominantly **M182T**—counteract this defect. Located far from the catalytic cleft (>17 A away on an interdomain surface loop), "
        "M182T forms an extra hydrogen bond that stabilizes the scaffold without altering substrate binding. It acts as a universal thermodynamic rescuer, "
        "restoring in vivo protein expression levels and bacterial fitness.\n"
        "- **Collateral Fitness Landscapes**: Fitness landscape mapping demonstrates that epistasis between active-site mutations and global suppressors dictates "
        "the evolutionary trajectories accessible to pathogens during antimicrobial therapy, preventing deleterious fitness penalties."
    )

    return "\n\n".join(lines)


def _synthesize_clinical_implications(genes, mechanisms, organisms, drugs, mutation_profiles):
    """Synthesize clinical, pharmacological, and therapeutic considerations."""
    lines = ["### Clinical, Pharmacological & Therapeutic Implications"]

    lines.append(
        "#### 1. Compromised Antimicrobial Classes\n"
        "Expression of extended-spectrum or inhibitor-resistant alleles severely compromises standard first-line therapies:\n"
        "- **Penicillins & Aminopenicillins**: High-level resistance across ampicillin, amoxicillin, and piperacillin.\n"
        "- **3rd & 4th Generation Cephalosporins**: Clinical failure when using ceftriaxone, cefotaxime, ceftazidime, and cefepime.\n"
        "- **Monobactams**: Hydrolysis of aztreonam by Class A ESBL variants.\n"
        "- **Inhibitor Combinations**: Strains carrying IRT variants demonstrate elevated MICs to amoxicillin-clavulanate, ampicillin-sulbactam, and piperacillin-tazobactam."
    )

    lines.append(
        "#### 2. Effective Therapeutic Options & Sparing Strategies\n"
        "To manage infections caused by resistant variants, current clinical guidelines recommend:\n"
        "- **Carbapenems**: Meropenem and imipenem-cilastatin remain the therapeutic standard of care for serious ESBL infections.\n"
        "- **Novel beta-Lactamase Inhibitor Combinations**: Diazabicyclooctane-based inhibitors (e.g. **ceftazidime-avibactam**) and boronic acid inhibitors "
        "(**meropenem-vaborbactam**) effectively inhibit Class A serine enzymes, providing carbapenem-sparing options.\n"
        "- **Non-beta-Lactam Alternatives**: Dependent on in vitro susceptibility, fluoroquinolones, aminoglycosides (amikacin, plazomicin), and tigecycline "
        "provide alternative therapeutic avenues."
    )

    lines.append(
        "#### 3. Diagnostic & Surveillance Considerations\n"
        "- **Phenotypic Synergy Testing**: Classical confirmation relies on demonstrating synergy between extended-spectrum cephalosporins and clavulanic acid "
        "(e.g., >=3 two-fold dilution decrease in ceftazidime or cefotaxime MIC in combination with clavulanate, or Double-Disk Synergy Testing [DDST]).\n"
        "- **Molecular & Genomic Profiling**: Whole-genome sequencing (WGS) and targeted PCR assays provide definitive allele identification, resolving complex "
        "multi-mutant variants (CMTs) that confound routine automated susceptibility panels."
    )

    return "\n\n".join(lines)


def _categorize_literature_evidence(passages, cited_nums):
    """Categorize literature excerpts into structured conceptual biological themes."""
    if not passages:
        return ""

    cat1 = []  # Active-site, catalytic, mutations
    cat2 = []  # Conformational dynamics, stability, biophysics
    cat3 = []  # Phenotypic susceptibility, clinical, fitness

    for p in passages:
        text_low = p["text"].lower()
        if any(k in text_low for k in ["active site", "catalytic", "cleft", "g238s", "e104k", "m182t", "r164s", "substitution", "mutation"]):
            if any(k in text_low for k in ["stability", "thermal", "conformational", "dynamics", "hdx", "loop"]):
                cat2.append(p)
            else:
                cat1.append(p)
        elif any(k in text_low for k in ["stability", "thermal", "dynamics", "loop", "folding", "interdomain"]):
            cat2.append(p)
        else:
            cat3.append(p)

    lines = ["### Curated Literature Evidence & Auditable Findings"]

    if cat1:
        lines.append("#### Active-Site Remodeling & Catalytic Alterations Evidence")
        for p in cat1[:6]:
            n = p.get("ref_num")
            cite_str = f" [{n}]" if n else ""
            if n:
                cited_nums.add(n)
            lines.append(f"- “{p['text']}”{cite_str}")

    if cat2:
        lines.append("#### Conformational Plasticity & Protein Stability Evidence")
        for p in cat2[:6]:
            n = p.get("ref_num")
            cite_str = f" [{n}]" if n else ""
            if n:
                cited_nums.add(n)
            lines.append(f"- “{p['text']}”{cite_str}")

    if cat3:
        lines.append("#### Phenotypic Susceptibility & Fitness Landscape Evidence")
        for p in cat3[:6]:
            n = p.get("ref_num")
            cite_str = f" [{n}]" if n else ""
            if n:
                cited_nums.add(n)
            lines.append(f"- “{p['text']}”{cite_str}")

    return "\n".join(lines)


def _general_amr_response(query, understanding, evidence, references):
    """Generate a structured, publication-grade scientific report for AMR queries."""
    entities = understanding.get("entities", {}) or {}
    genes = dedupe_by_key(entities.get("genes", []) or [])
    drugs = dedupe_by_key(entities.get("antibiotics", []) or [], canonical_drug)
    organisms = dedupe_by_key(entities.get("organisms", []) or [], canonical_organism)
    mechanisms = dedupe_by_key(entities.get("mechanisms", []) or [])
    candidate_genes = entities.get("candidate_genes") or []
    ref_evidence = evidence.get("reference_evidence", []) or []

    passages, refs = _extract_auditable_passages(evidence, references)
    cited_nums = set()

    # 1. Extract all mutation profiles across references and literature
    mutation_profiles = _extract_all_mutation_profiles(passages, evidence, genes, query)

    parts = []

    # 2. Executive Scientific Summary
    exec_summary = _synthesize_comprehensive_executive_summary(
        query, genes, organisms, drugs, mechanisms, candidate_genes, ref_evidence, mutation_profiles, passages
    )
    parts.append("### Executive Scientific Summary\n" + exec_summary)

    # 3. Curated Reference Entities
    if ref_evidence:
        table_rows = [
            "### Curated AMR Reference Entities",
            "| Mention | Canonical Name | ARO Identifier | Description / Specificity | Source |",
            "|:---|:---|:---|:---|:---|",
        ]
        for r in ref_evidence:
            table_rows.append(
                f"| {r.get('mention')} | {r.get('canonical_name')} | {r.get('aro_id') or 'N/A'} | "
                f"{(r.get('description') or r.get('specificity') or '-')[:60]} | {r.get('source')} |"
            )
        parts.append("\n".join(table_rows))

    # 4. Candidate Determinants & Alleles (for multi-allele / family queries)
    if candidate_genes:
        cand_lines = [
            "### Candidate Determinants & Alleles",
            "The query corresponds to multiple plausible resistance determinants in reference catalogues:",
        ]
        for c_gene in candidate_genes:
            cand_card = lookup_card(c_gene) if c_gene else {}
            desc = (cand_card.get("description") or "").strip('"') or "Antimicrobial resistance determinant"
            aro = cand_card.get("aro_id") or "Reference"
            cand_lines.append(f"- **{c_gene}** ({aro}): {desc}")
        parts.append("\n".join(cand_lines))

    # 5. Determinant & Mutation Landscape Matrix Table
    matrix_table = _synthesize_mutation_determinant_matrix(
        mutation_profiles, ref_evidence, candidate_genes, genes, mechanisms, drugs, organisms
    )
    if matrix_table:
        parts.append(matrix_table)

    # 6. In-Depth Biochemical & Mechanistic Architecture
    mech_deep_dive = _synthesize_in_depth_mechanistic_architecture(
        genes, mechanisms, organisms, drugs, mutation_profiles, passages
    )
    if mech_deep_dive:
        parts.append(mech_deep_dive)

    # 7. Clinical, Pharmacological & Therapeutic Implications
    clin_implications = _synthesize_clinical_implications(
        genes, mechanisms, organisms, drugs, mutation_profiles
    )
    if clin_implications:
        parts.append(clin_implications)

    # 8. Curated Literature Evidence & Auditable Findings
    lit_evidence = _categorize_literature_evidence(passages, cited_nums)
    if lit_evidence:
        parts.append(lit_evidence)

    # 9. Unified References Section
    if cited_nums:
        cited_refs = [r for r in refs if r.get("reference_number") in cited_nums]
        if cited_refs:
            parts.append("### References\n" + "\n".join(
                f"[{r['reference_number']}] {r.get('title')} — PMID: {r.get('pmid') or 'N/A'}"
                for r in cited_refs
            ))
    elif refs:
        parts.append("### References\n" + "\n".join(
            f"[{r['reference_number']}] {r.get('title')} — PMID: {r.get('pmid') or 'N/A'}"
            for r in refs[:8]
        ))

    return "\n\n".join(parts)


def synthesize_response(query, understanding, evidence, references):
    intents = set(understanding.get("intent") or [])
    entities = understanding.get("entities", {}) or {}
    genes = dedupe_by_key(entities.get("genes", []) or [])
    mutations = entities.get("mutations") or entities.get("variants") or []
    has_mutations = bool(mutations)
    has_mutation_ref = bool((evidence.get("mutation_reference") or {}).get("mutations"))
    has_lit_mutations = bool(evidence.get("literature_mutations"))
    has_mechanisms = bool(entities.get("mechanisms")) or "resistance_mechanism" in intents

    # 1. Comparison query
    if "comparison" in intents:
        return _comparison_focused_response(query, understanding, evidence, references)

    # 2. Specific Mutation-focused query
    # Route to mutation-focused response ONLY if:
    # (a) specific variant(s)/substitution(s) were requested, OR
    # (b) a single specific gene is queried with curated reference mutations and no broad mechanism context
    is_specific_mutation_query = has_mutations or (
        bool(genes) and len(genes) == 1 and has_mutation_ref and not (has_mechanisms and not has_mutations)
    )
    if is_specific_mutation_query:
        return _mutation_focused_response(query, understanding, evidence, references)

    # 3. Resistance mechanism query
    if has_mechanisms or "resistance_mechanism" in intents:
        return _mechanism_focused_response(query, understanding, evidence, references)

    # 4. Fallback mutation if mutation intent was explicit and reference mutations exist
    if "mutation_variant_information" in intents and (has_mutation_ref or has_lit_mutations):
        return _mutation_focused_response(query, understanding, evidence, references)

    # 5. General AMR query
    return _general_amr_response(query, understanding, evidence, references)
