# Five Original Query/Output Assessments

These assessments preserve the original query wording and evaluate the corresponding PDF output. They are scientific and architectural findings from the supplied snapshots; they are not claims that the live providers were revalidated for every cited paper.

## Query 1

**Original query:** `Is gyrA S83L associated with ciprofloxacin resistance in Escherichia coli?`

The PDF returned a concise negative answer and did not fabricate a paper or reference. This is preferable to presenting an irrelevant mutation list. However, the answer says “available evidence” without exposing the retrieval status, provider coverage, or whether the result means no qualifying evidence versus a failed search. The corrected application now keeps those states separate and reports source failures explicitly.

## Query 2

**Original query:** `Which mutations in katG are associated with isoniazid resistance in Mycobacterium tuberculosis?`

The output contains several plausible variants, including S315T, S315N, L333V, K414N, A162E, W107R, and T275P. The main problem is not the number of rows; it is evidence granularity. Some excerpts mention a group of mutations or a resistance isolate without establishing that each displayed allele independently causes or predicts isoniazid resistance. The output also mixes abstract/title-level evidence and stronger experimental evidence without a visible classification. The corrected pipeline keeps the exact supporting sentence, distinguishes literature from curated evidence, and excludes a row when the mutation–drug relationship is only implied by generic context.

## Query 3

**Original query:** `Which rpoB mutations are associated with rifampicin resistance in Mycobacterium tuberculosis?`

The output combines WHO catalogue rows with primary-literature rows, which can be useful, but the evidence types are not clearly separated. Alternative numbering is shown for H445Y, while other papers use different notation; those mappings require an organism, gene, and numbering basis rather than visual similarity. The displayed top-20 limit is acceptable only if the complete reference set is still used for matching. The corrected workflow preserves full-reference matching, labels curated classifications separately, retains paper provenance, and keeps alternative notation attached to its source evidence.

## Query 4

**Original query:** `Which mutations in mecA are associated with methicillin resistance in Staphylococcus aureus?`

The single G246E result is potentially relevant, but the excerpt says the mutation was identified across multiple staphylococcal species. That does not by itself establish a specific mecA–methicillin relationship in *S. aureus*. The organism constraint must be checked in the source sentence or full text, and a paper discussing mecA sequence diversity must not automatically be treated as direct causal evidence. The corrected evidence filter retains organism-specific support requirements and classifies indirect or multi-species findings conservatively.

## Query 5

**Original query:** `Are there published experimental studies directly linking katG G944C to isoniazid resistance?`

The PDF incorrectly reports G944C as a resistance-associated KatG mutation based on a detection assay sentence. This is a compound error: the sentence describes detection in resistant isolates, not experimental proof that the mutation causes resistance, and position 944 is outside the approximately 740-aa *M. tuberculosis* KatG protein. The corrected system rejects G944C as a protein substitution for this gene/organism unless a supported nucleotide-level notation and reference sequence are supplied. It must return invalid/unsupported or no qualifying direct experimental evidence—not convert G944C into another mutation and not infer causality from a diagnostic assay.

## Cross-case root causes

The snapshots show five recurring problems: the interface exposed a prefilled test query and sample selector; literature evidence was not consistently separated from curated reference classifications; abstract/title mentions were sometimes promoted to direct mutation–phenotype claims; alternative numbering needed explicit organism/reference-sequence validation; and nucleotide/protein notation was conflated in the G944C case. The updated application addresses these at the entity-resolution, literature-agent, evidence-filter, report-schema, and frontend-rendering layers rather than hardcoding the five examples.
