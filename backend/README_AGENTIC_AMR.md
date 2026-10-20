# Agentic AI Platform for Evidence-Driven AMR Mutation Analysis

## Final project scope

This release is intentionally restricted to **mutation-focused antimicrobial-resistance research**.

Primary question:

> Which mutations in this gene, in this organism, are associated with which resistance/susceptibility phenotype for the requested antibiotic?

Supported entities:
- Gene / AMR determinant
- Mutation / variant
- Antibiotic
- Organism
- Phenotype (resistant, susceptible, low-level/interim, variable, uncertain)

**Phenotype is the second layer. Mutation identification for a specific gene + organism is the primary target.**

The system should not behave as a general-purpose biology chatbot.

---

## Example query

> Which mutations in rpoB are associated with rifampicin resistance in Mycobacterium tuberculosis?

The intended route is:

```text
User Query
   ↓
Query Understanding Agent
   ↓
Database-backed Entity Normalization
   ├── Gene / AMR determinant → CARD + NCBI AMRFinderPlus reference
   ├── Antibiotic → ChEBI 2.0 REST API
   └── Organism → NCBI Taxonomy E-utilities
   ↓
Agentic Retrieval Planner
   ↓
Mutation Reference Agent
   ├── NCBI AMRFinderPlus reference
   └── WHO TB Mutation Catalogue (conditional for M. tuberculosis)
   ↓
PubMed literature retrieval
   ↓
PMC BioC full-text evidence (conditional, relevant records only)
   ↓
Evidence Analysis Agent
   ↓
Response Synthesis Agent
   ↓
Mutation table + phenotype interpretation + scientific references
```

The router is conditional: the WHO TB catalogue is **not** called for an E. coli query, while an M. tuberculosis + rifampicin query can activate it.

---

## Scientific evidence policy

### Reference databases
CARD and NCBI AMRFinderPlus are used primarily for identity, nomenclature and known AMR determinant/point-mutation annotation.

### Phenotype evidence
Published literature and specialized phenotype catalogues are used for biological claims.

### WHO TB catalogue
The bundled WHO 2023 second-edition catalogue is used conditionally for M. tuberculosis drug-resistance mutation interpretation. Its grading categories are preserved. The system does **not** convert:
- `Uncertain significance` → susceptible
- `Not associated with resistance` → susceptible

A mutation is called phenotypically susceptible only when the supporting evidence explicitly establishes susceptibility.

### Literature
PubMed is used through NCBI E-utilities. For a small number of already-relevant records, PMC Open Access full text may be retrieved through the NCBI BioC API.

---

## External APIs

- NCBI E-utilities: PubMed + Taxonomy
- NCBI PMC BioC API: full-text evidence where available
- ChEBI 2.0 REST API: antibiotic/chemical normalization
- PubChem can be added later as a supplementary chemical cross-reference service

API credentials are optional for NCBI but can be supplied through environment variables.

---

## Important local resources

```text
backend/data/
├── CARD_AMR.obo
├── NCBI_AMR.csv
├── chebi.obo
├── CHEBI_AMR_INDEX.json
├── WHO-UCN-TB-2023.7-eng.xlsx
└── WHO_TB_MUTATION_INDEX.json
```

The two `*_INDEX.json` files are derived runtime indexes. The supplied CARD/NCBI/ChEBI/WHO source resources remain unchanged.

---

## Output design

For a mutation-focused query, the primary response should contain:

```text
Short direct answer

Mutation findings

| Mutation | Phenotype / resistance status | Evidence source |
|----------|-------------------------------|-----------------|
| ...      | ...                           | ...             |

Phenotype caveats
References
```

The system must distinguish:
- resistance-associated
- interim / low-level or variable evidence where explicitly supported
- uncertain significance
- not associated with resistance
- explicitly demonstrated susceptibility

It must never invent a mutation, phenotype, paper, PMID or database record.

---

## Running

From `backend/`:

```bash
python app.py
```

Install dependencies first:

```bash
pip install -r requirements.txt
```

Optional environment variables:

```text
AMR_OLLAMA_URL
AMR_OLLAMA_MODEL
AMR_NCBI_API_KEY
AMR_NCBI_EMAIL
AMR_NCBI_TOOL
AMR_API_TIMEOUT
AMR_CHEBI_API_URL
AMR_PMC_BIOC_URL
AMR_DEBUG
```

The default LLM path uses Ollama, but the system retains a deterministic evidence-grounded fallback.

---

## Development principle

Do not replace the project with a generic chatbot or a new standalone implementation.

The architecture is deliberately modular and agentic so additional specialized AMR resources can be added later without changing the core query-understanding layer.


## Final-scope hardening (6 October 2026)

The release now enforces the requested narrow scope at query-understanding time:
queries must be mutation/variant-focused and ask about resistance, susceptibility,
or phenotype. General gene definitions, mechanism-only questions, and general AMR
questions are rejected rather than routed to the research pipeline.

For organism-specific mutation queries, the specialized reference route preserves
WHO confidence wording. In particular, `Not assoc w R` is reported as **not associated
with resistance**, not as susceptible. Explicit susceptibility is only claimed when
supported by evidence.

The WHO mutation route is conditional on `Mycobacterium tuberculosis` + rifampicin/rifampin;
other organisms do not invoke the TB catalogue.

The backend also includes `openpyxl` in the install requirements because the WHO workbook
is the authoritative local source from which the runtime mutation index can be rebuilt.
