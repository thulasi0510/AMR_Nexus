# AMR Agentic AI — retrieval fix

## What changed

- `agents/amr_lexicon.py`: deterministic fallback vocabulary for common antibiotics and AMR mechanisms. This prevents a missing LLM extraction (for example, `colistin`) from disappearing from the literature query.
- `agents/query_understanding.py`: merges LLM extraction, CARD/NCBI grounding, and deterministic AMR vocabulary.
- `agents/literature.py`: builds complementary PubMed searches from the whole information need, including exact allele, antibiotic relationship, mechanism, review/primary-study passes, and conservative parent-family expansion (for example, `MCR-1.1` -> `MCR-1`).
- PubMed retrieval now uses ESearch history plus paginated EFetch. With `AMR_PUBMED_MAX_RECORDS=0` (default), the matching result set is traversed rather than stopping at an arbitrary top-10 list.
- All retrieved records are deduplicated and relevance-ranked before evidence analysis. Only the strongest candidates are passed to the LLM to keep context manageable.
- `agents/evidence_analysis.py`: distinguishes exact allele/entity evidence from parent-family context. Family-level evidence can explain a mechanism but is not presented as allele-specific proof.
- `agents/response_synthesis.py`: prompts the LLM to answer the complete biological question in a scientific narrative with inline PubMed citations and a reference section.
- `config.py`: adds PubMed batch/max-record controls.

## Important scientific behavior

For a query such as `What is MCR-1.1 and how does it confer colistin resistance?`, the system should understand at minimum:

- gene: `MCR-1.1`
- antibiotic: `colistin`
- intent: resistance mechanism

It then searches both exact-allele and parent-family literature. A paper about MCR-1 can be used as family-level mechanistic context, but the final answer must not imply that the experiment was specifically performed on MCR-1.1 unless the paper actually says so.

## PubMed coverage vs answer references

The system can search the complete matching PubMed result set, but it should not dump every paper into the final answer. All retrieved records are ranked first; a configurable evidence-candidate subset is sent to the LLM, and only papers actually cited in the generated answer are shown in the final reference list.

Environment variables:

- `AMR_PUBMED_MAX_RECORDS=0` — complete retrieval (default)
- `AMR_PUBMED_BATCH_SIZE=200` — PubMed fetch batch size
- `AMR_PUBMED_CANDIDATES=100` — maximum strongest candidates sent to evidence/synthesis
- `AMR_PUBMED_OUTPUT_REFERENCES=12` — concise displayed reference set

The last two are answer-context controls, not claims that PubMed contains only that many papers.


## 27 Sep retrieval patch

The PubMed query builder was hardened for hyphenated/allelic AMR names such as `MCR-1.1`. PubMed documents that hyphenated phrases can return no results when the phrase is not in its phrase index, so gene/allele terms are now field-qualified instead of being sent as quoted phrases. The query-understanding layer also normalizes malformed LLM intent output and removes null-like placeholders such as `null` from entity lists.

## 27 Sep synthesis-quality patch

Retrieval, ranking, and evidence classification were already working (159 main-query matches / 2044 unique records / 100 evidence candidates / 12 concise displayed, for the MCR-1.1 query). The remaining problem was in `agents/response_synthesis.py`: the LLM prompt handed the model a raw `json.dumps()` of the full evidence structure -- including every candidate's full abstract -- truncated at tens of thousands of characters. That is what produced a single-paper, copy-like answer instead of a multi-paper synthesis, and made an Ollama timeout (silently falling back to the thin fallback answer) more likely on a large evidence set.

- `agents/response_synthesis.py`: rewritten to build a compact, pre-digested brief instead of a raw JSON dump -- each of the top `SYNTHESIS_MAX_PAPERS` candidates is shown with its already-computed evidence tier (direct / family-context / related / weak, from `evidence_analysis.py`'s alignment classification), article type, and its 2-3 highest-scoring extracted sentences (already computed by `literature.py`/`evidence_analysis.py`), instead of the full abstract. The prompt explicitly requires claim-level interleaved citations (not one citation block at the end of a paragraph), section structure driven by `information_needed`, and forbids internal-pipeline phrasing ("the available PubMed literature relevant to...", etc.). The fallback path (used only if Ollama is unavailable/errors/times out) was also rewritten to be tier-aware and multi-paper rather than a single concatenated abstract, and is clearly labelled as a degraded response.
- `config.py`: added `SYNTHESIS_MAX_PAPERS` (25) and `SYNTHESIS_MAX_SNIPPET_CHARS` (280) to bound prompt size; raised `OLLAMA_RESPONSE_TIMEOUT` default from 90s to 150s now that the prompt is much smaller.
- `agents/report_generator.py`: added `public_view()` -- returns only `{status, query, title, answer, references}` (or the candidate list for `pending_selection`), with no internal fields. Citation reconciliation logic (`reconcile_citations`) is unchanged.
- `routes/query.py`: the API now calls `public_view()` by default, and returns the full internal report only when `config.AMR_DEBUG` is set or `?debug=1` is passed per-request. Previously every field (`execution_trace`, `understanding`, `plan`, `literature_results`, etc.) was always returned to the frontend.

Retrieval, CARD/NCBI loading, the LangGraph workflow, and `test_py_file.py` were not modified.


## v5.1 evidence-integrity fix (mutation-scope retrieval)

Root causes found in v5 and what changed:

| Problem | Root cause | Fix |
|---|---|---|
| Relevant papers dropped | Paper filter allowlist was the 20 *displayed* mutations (`retrieve_mutations` truncated before returning); organism/drug/mutation were raw substring matches on a few literal forms (no `Mtb`, no `Ser450Leu`, no other numbering) | `retrieve_mutations` keeps the full set in `all_mutations` (display list `mutations` is still capped at 20); new `agents/mutation_matching.py` matches any accepted notation against the complete reference |
| Other-organism papers labelled direct | Organism "hit" counted any mention incl. full text and passing comparisons; no competing-organism check | `agents/entity_aliases.organism_assessment`: organism comes from title/abstract/MeSH only; a different organism in the title, or more prominent in the abstract, makes the paper `other_organism` / `mixed_organism` and never direct |
| No supporting literature shown for generic queries | `mutation_aligned` required the user-*requested* mutation, which a generic query lacks | One shared `assess_paper()` used by the PubMed filter, evidence tiers and literature mutation extraction |
| Duplicate / malformed entities (`rifampicin, rifampin`; `m. tuberculosis[Title/Abstract]`) | Aliases were stored in the entity lists and fed straight into PubMed queries | Entity lists hold one canonical name; aliases live in `understanding["entity_aliases"]` and are expanded only for matching and query building |
| LLM-invented entities | LLM organisms/antibiotics/mutations/mechanisms were accepted without checking the query | Each must be supported by the user's own query (any accepted alias/notation) |
| "No evidence" vs "retrieval failed" indistinguishable | Errors were collected but never reached the answer; NCBI error bodies (HTTP 200) looked like 0 records | `literature["retrieval"]["status"]` = `failed` / `partial_failure` / `no_pubmed_records` / `no_matching_evidence` / `matches_found`, reflected in the answer; PMC distinguishes `not_available` from `error` |

Accepted notations: `S450L`, `p.S450L`, `Ser450Leu`, `p.Ser450Leu`, `Ser-450-Leu`, `S450->L`, and where a numbering scheme is declared in `agents/amr_lexicon.NUMBERING_SCHEMES`, the alternative position (currently *M. tuberculosis* rpoB, offset +81 = *E. coli* numbering, e.g. S450L = S531L). Add schemes there; do not hard-code them elsewhere.

Evidence rules (all from the paper's own text): a paper is *direct* (`mutation_aligned` / `exact`) only if its title/abstract names the gene and the drug (any alias, `RIF`/`RMP` abbreviations case-sensitive, never `MTB/RIF`), establishes the queried organism as its subject, and a mutation appears in gene context (same or adjacent sentence). Full text may add mutation mentions but never establishes the organism. Each analysed paper carries an `evidence_basis` explaining its tier.

Source classifications are never rewritten: a literature mention of a WHO-graded mutation is shown with the WHO grade next to the paper's own wording; it does not add a second table row.

### Running the tests
```
cd backend
python test_final_scope.py
python test_v5_precision.py
python test_regression_retrieval.py      # 24 tests, fully offline (fake PubMed/PMC/Taxonomy/ChEBI/LLM)
```
The regression tests prove the logic against fake PubMed records. They do not prove live PubMed/PMC retrieval; check that separately with `AMR_DEBUG=1`.

### v5.1 follow-up (found in a live PubMed run)
* Each substitution is now attributed to a gene: mutations that belong to another gene named in the same sentence (parC, gyrB, parE ...) are no longer reported for the queried gene (`mutation_matching._attributed_gene`).
* A title naming both the queried organism and another organism is `mixed_organism` and never direct evidence.
* PMC: only records with a PMCID are requested; HTTP 4xx and "No result can be found" replies count as *not available*, only network errors / 429 / 5xx count as *errors* (sample reasons in `pmc_error_samples`).
* Specific-mutation questions ("Does rpoB D435V confer ...") now open with the reference classification of that mutation; a variant returned by a resolver as a "gene" is never displayed as one.
