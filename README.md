# Mutation-focused AMR Research Assistant

This application keeps the existing Flask, LangGraph, reference-data, and React architecture while adding a dedicated multi-source Literature Research Agent and a prompt-compliant research interface. The frontend opens with an empty query field and no sample-query menu. The five validation queries remain in `queries_and_outputs/` and `FIVE_QUERY_ASSESSMENT.md`; they are not hardcoded into the UI.

## Architecture

The LangGraph workflow resolves the question and entities first, then passes a structured `LiteratureResearchRequest` to `backend/agents/literature_research_agent.py`. That agent owns literature discovery, provider normalization, deduplication, and preliminary filtering through the existing PubMed/PMC path and optional Europe PMC, OpenAlex, Crossref, and Semantic Scholar adapters. PMC full-text enrichment remains a distinct downstream evidence stage. The main workflow then applies mutation-specific evidence extraction, integrates curated CARD/NCBI/WHO evidence without conflating it with papers, and generates the public report.

ResearchGate is handled honestly: the project does not scrape it, bypass authentication, or claim automated full-text access. Source metadata reports that no unrestricted supported public API is used; a legitimate manual search link is provided only as a discovery limitation. Bibliographic metadata alone never establishes a mutation–phenotype claim.

`G944C` is rejected as an amino-acid position for *M. tuberculosis* KatG because KatG is approximately 740 amino acids. The system does not silently convert it to S315T. Nucleotide-level and protein-level notation remain separate unless a supported reference sequence establishes equivalence.

## Latency and evidence controls

The default fast path uses deterministic query/entity agents and bounded network calls. `AMR_PUBMED_TIMEOUT=1`, `AMR_PUBMED_RETRIES=1`, and a small number of query formulations prevent slow or unavailable providers from blocking the request. Set `AMR_PUBMED_QUERY_VARIANTS=3` and `AMR_PUBMED_FETCH_COUNTS=1` for a broader reporting run when latency is less important. Every API response includes measured `latency.total_seconds`, `budget_seconds`, and `within_budget`.

Set `AMR_ADDITIONAL_SOURCES=1` to run Europe PMC, OpenAlex, Crossref, and Semantic Scholar (when configured) in parallel with provenance-preserving deduplication. ResearchGate is reported as manual-only; it is not scraped. Literature-only named variants are accepted only when the exact substitution is present in gene context and the same paper supports the requested drug/organism. Evidence rows distinguish curated classification from direct experimental, clinical/observational, diagnostic, computational, and general association evidence.

## Windows PowerShell setup

Open PowerShell terminal 1:

```powershell
cd "C:\path\to\amr_backend_frontend\backend"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
$env:PYTHONPATH="."
$env:AMR_CORS_ORIGINS="http://localhost:5173,http://127.0.0.1:5173"
# Optional multi-source adapters:
# $env:AMR_ADDITIONAL_SOURCES="1"
python -m flask --app app run --host 0.0.0.0 --port 5000
```

Use `python -m flask`, rather than a possibly global `flask` executable, so the command uses the active virtual environment. Test it in another terminal:

```powershell
Invoke-WebRequest http://localhost:5000/health
```

Open PowerShell terminal 2:

```powershell
cd "C:\path\to\amr_backend_frontend\frontend"
npm install
# frontend/.env.local should contain VITE_API_URL=http://localhost:5000
npm run dev
```

Open the Vite URL, normally `http://localhost:5173`. If the copied archive contains `backend/.venv`, delete it and recreate it; virtual environments are machine-specific and must not be reused across computers.

## Linux/macOS setup

```bash
cd /path/to/amr_backend_frontend/backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
export PYTHONPATH=.
export AMR_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
python -m flask --app app run --host 0.0.0.0 --port 5000
```

In another terminal:

```bash
cd /path/to/amr_backend_frontend/frontend
npm install
npm run dev
```

For supplemental providers, set `AMR_ADDITIONAL_SOURCES=1`. Semantic Scholar additionally requires `AMR_SEMANTIC_SCHOLAR_API_KEY`. NCBI identification settings may be supplied through `AMR_NCBI_EMAIL` and `AMR_NCBI_API_KEY`. Never place credentials in React files or commit them. Use `.env.example` as the secret-free template.

## API test

```bash
curl -X POST http://localhost:5000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"Is rpoB S450L associated with rifampicin resistance in Mycobacterium tuberculosis?"}'
```

The frontend submits only the user-entered query. It does not reinterpret or invent scientific conclusions. It renders loading, ambiguity, empty evidence, partial source failure, complete failure, tables, excerpts, and traceable publication links from the backend response.

## Tests

From `backend/` with the environment active:

```bash
python -m pytest -q
python test_final_scope.py
python test_v5_precision.py
python tests/test_mutation_scope_regressions.py
```

From `frontend/`:

```bash
npm install
npm run build
```

The validation report must state only tests actually run. External-provider reachability, full-text licensing, and scientific interpretation of individual papers remain live limitations; an unavailable provider is reported as a provider error rather than converted into a false no-evidence result.

## Deliverables and changed files

- `backend/agents/literature_research_agent.py`: dedicated structured Literature Research Agent boundary.
- `backend/agents/source_adapters.py`: multi-source adapters, provenance, deduplication, source text type, and honest ResearchGate limitation metadata.
- `backend/services/orchestrator.py`: LangGraph integration through the dedicated agent.
- `backend/agents/report_generator.py`: safe retrieval/source status fields for the public response.
- `frontend/src/App.jsx` and `frontend/src/styles.css`: empty initial query, no sample selector, structured evidence/table rendering, and failure states.
- `.env.example`: secret-free configuration template.
- `FIVE_QUERY_ASSESSMENT.md`: assessment of all five original query/PDF cases.
- `AUDIT_REPORT.md`: root causes and verification record.
