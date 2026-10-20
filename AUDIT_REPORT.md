# AMR Research Assistant Audit Report

## Scope

The supplied archive was inspected together with its embedded `prompt_to_manus.txt`, five query/output PDFs, backend source, frontend source, dependency files, and tests. The five PDF assessments are recorded separately in `FIVE_QUERY_ASSESSMENT.md`.

## Root causes

The original frontend opened with a populated test query and exposed a sample-query dropdown, directly violating the prompt's internal-validation-only requirement. Its answer renderer treated backend Markdown as display text instead of rendering structured tables. The backend had multi-source adapter code, but the LangGraph workflow still called the existing router directly and did not present a distinct Literature Research Agent contract. ResearchGate had no legitimate unrestricted public API path and therefore could not be presented as an automated provider. The evidence snapshots also showed broad mutation rows promoted from weak or group-level excerpts, mixed curated classifications with literature findings, and a diagnostic `katG G944C` mention treated as a causal protein mutation.

## Corrections

`backend/agents/literature_research_agent.py` now defines a separate request/report boundary and is the LangGraph literature node's direct integration point. PubMed/PMC remain available, while Europe PMC, OpenAlex, Crossref, and optional Semantic Scholar are normalized and deduplicated by PMID, PMCID, DOI, then title. Source provenance and text type are retained. Provider failures are returned as structured errors. ResearchGate is explicitly reported as metadata/manual-discovery-only; no scraping, authentication bypass, or unsupported full-text retrieval is attempted.

The mutation-evidence layer keeps the KatG length sanity guard and rejects `G944C` as a protein position for KatG even when the organism is omitted. Literature extraction requires the exact gene/organism/drug context and an explicit phenotype-bearing sentence; exact named variants can also be supported from literature when no curated catalogue row exists. Curated WHO/CARD/NCBI classifications remain distinct from primary-paper findings. Evidence rows now distinguish direct experimental, clinical/observational, diagnostic, computational, and general association evidence. The public report exposes only safe answer, reference, retrieval, source-status, and measured-latency fields.

The frontend now starts with an empty textarea, has no sample dropdown, does not hardcode the five queries, renders tables and links, and handles ambiguity, empty evidence, partial provider failure, complete failure, loading, timeout, and invalid requests.

## Verification record

The following checks were run during the original implementation and remain the baseline regression record: Python compilation passed; the standalone final-scope and mutation-precision scripts passed; 20 mutation-scope regression tests passed; the full backend suite passed with 44 tests; the Vite production build passed; Flask `/health` passed; empty-query validation returned HTTP 400; and one live API smoke query completed with an honest no-qualifying-evidence result and no `G944C` leakage.

For this prompt-compliance update, the frontend source was rebuilt around the empty-query contract, the dedicated Literature Research Agent import was added to the LangGraph workflow, ResearchGate limitation metadata was added, and `.env.example` plus the five-query assessment were created. In this pass, Python compilation and the complete backend suite passed with **46 tests**, the Vite production build passed, and focused checks confirmed that literature-only `gyrA S83L` is eligible when explicitly present in gene context while impossible `katG G944C` is rejected. A live `gyrA S83L` API smoke request completed in **2.083 seconds** and returned a partial-failure status because the sandbox's PubMed request timed out; it did not convert provider failure into a false scientific conclusion. A live G944C request completed in **1.766 seconds** and returned the explicit protein-length validation message. The five original PDFs remain assessment fixtures; rerun them on the target Windows machine after `pip install -r requirements.txt` and `npm install` to validate current provider reachability.

## Limitations

The supplied PDFs are snapshots, not authoritative proof of every cited paper's current content. Live source availability, provider rate limits, full-text licensing, and Semantic Scholar credentials affect retrieval. ResearchGate is not automatically scraped. A metadata record or title/abstract mention is not treated as direct experimental proof without a mutation-specific evidence sentence. The five sample queries are preserved for testing and assessment only, not exposed as frontend defaults.
