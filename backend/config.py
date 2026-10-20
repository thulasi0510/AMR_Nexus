"""Central configuration for the AMR Agentic AI backend."""
import os

OLLAMA_URL = os.getenv("AMR_OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("AMR_OLLAMA_MODEL", "llama3.2:latest")
OLLAMA_QUERY_TIMEOUT = int(os.getenv("AMR_OLLAMA_QUERY_TIMEOUT", "3"))
# A structured, multi-section, claim-cited answer takes longer to generate
# than a short intent-classification call, especially on CPU. This was 90s;
# combined with the much smaller, pre-digested prompt now built in
# response_synthesis.py (evidence snippets/tiers instead of raw JSON dumps
# of full abstracts), 150s gives the real synthesis path a fair chance to
# finish instead of silently falling back to the thin fallback answer.
OLLAMA_RESPONSE_TIMEOUT = int(os.getenv("AMR_OLLAMA_RESPONSE_TIMEOUT", "4"))
AMR_LATENCY_BUDGET_SECONDS = float(os.getenv("AMR_LATENCY_BUDGET_SECONDS", "7"))
AMR_LLM_QUERY_ENABLED = os.getenv("AMR_LLM_QUERY_ENABLED", "0") == "1"
AMR_PMC_ENRICHMENT_ENABLED = os.getenv("AMR_PMC_ENRICHMENT_ENABLED", "0") == "1"

PUBMED_EUTILS_URL = os.getenv(
    "AMR_PUBMED_EUTILS_URL",
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
)
# PubMed search covers the indexed result set.
# Setting PUBMED_CANDIDATES_PER_QUERY=0 means all valid retrieved papers are
# retained in the candidate pool without artificial truncation.
PUBMED_CANDIDATES_PER_QUERY = int(os.getenv("AMR_PUBMED_CANDIDATES", "0"))
PUBMED_OUTPUT_REFERENCES = int(os.getenv("AMR_PUBMED_OUTPUT_REFERENCES", "25"))
PUBMED_TIMEOUT = int(os.getenv("AMR_PUBMED_TIMEOUT", "25"))
PUBMED_RETRIES = int(os.getenv("AMR_PUBMED_RETRIES", "3"))
PUBMED_FETCH_COUNTS = os.getenv("AMR_PUBMED_FETCH_COUNTS", "1") == "1"
PUBMED_QUERY_VARIANTS = int(os.getenv("AMR_PUBMED_QUERY_VARIANTS", "5"))
# PubMed is retrieved in batches so large searches do not rely on a single
# ESearch response. 500 per query formulation with 5 complementary formulations
# allows traversing up to ~2500 unique candidate records across PubMed.
PUBMED_BATCH_SIZE = int(os.getenv("AMR_PUBMED_BATCH_SIZE", "100"))
PUBMED_MAX_RECORDS = int(os.getenv("AMR_PUBMED_MAX_RECORDS", "500"))
# How many evidence candidates are described with extracted sentence snippets
# inside synthesis prompts. Setting to 40 allows deep, multi-paper research synthesis.
SYNTHESIS_MAX_PAPERS = int(os.getenv("AMR_SYNTHESIS_MAX_PAPERS", "40"))
SYNTHESIS_MAX_SNIPPET_CHARS = int(os.getenv("AMR_SYNTHESIS_MAX_SNIPPET_CHARS", "300"))

AMR_DEBUG = os.getenv("AMR_DEBUG", "0") == "1"
# Supplemental scholarly providers (Europe PMC, OpenAlex, Crossref) use free
# public REST APIs and run in parallel with PubMed to broaden coverage. Enabled
# by default. Set AMR_ADDITIONAL_SOURCES=0 to disable for offline/test runs.
AMR_ADDITIONAL_SOURCES = os.getenv("AMR_ADDITIONAL_SOURCES", "1") == "1"
AMR_SEMANTIC_SCHOLAR_API_KEY = os.getenv("AMR_SEMANTIC_SCHOLAR_API_KEY", "")


# External scientific resource APIs / local reference paths
NCBI_EUTILS_URL = os.getenv(
    "AMR_NCBI_EUTILS_URL",
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
)
NCBI_API_KEY = os.getenv("AMR_NCBI_API_KEY", "")
NCBI_TOOL = os.getenv("AMR_NCBI_TOOL", "agentic-amr-research")
NCBI_EMAIL = os.getenv("AMR_NCBI_EMAIL", "")
API_TIMEOUT = int(os.getenv("AMR_API_TIMEOUT", "10"))
PMC_BIOC_URL = os.getenv(
    "AMR_PMC_BIOC_URL",
    "https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi",
)
CHEBI_API_URL = os.getenv(
    "AMR_CHEBI_API_URL",
    "https://www.ebi.ac.uk/chebi/backend/api/public",
)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NCBI_AMR_PATH = os.getenv("AMR_NCBI_AMR_PATH", os.path.join(BASE_DIR, "data", "NCBI_AMR.csv"))
WHO_TB_PATH = os.getenv("AMR_WHO_TB_PATH", os.path.join(BASE_DIR, "data", "WHO-UCN-TB-2023.7-eng.xlsx"))
CHEBI_OBO_PATH = os.getenv("AMR_CHEBI_OBO_PATH", os.path.join(BASE_DIR, "data", "chebi.obo"))

WHO_TB_INDEX_PATH = os.getenv("AMR_WHO_TB_INDEX_PATH", os.path.join(BASE_DIR, "data", "WHO_TB_MUTATION_INDEX.json"))

CHEBI_AMR_INDEX_PATH = os.getenv("AMR_CHEBI_AMR_INDEX_PATH", os.path.join(BASE_DIR, "data", "CHEBI_AMR_INDEX.json"))
