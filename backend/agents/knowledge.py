import requests

def search_knowledge(plan):

    query = plan.get("gene")

    if not query:
        return {
            "title": "Knowledge Report",
            "summary": (
                "No validated gene was available; knowledge search "
                "was not performed."
            ),
            "direct_evidence": [],
            "related_evidence": [],
            "requires_verification": [],
            "references": []
        }

    url = "https://rest.uniprot.org/uniprotkb/search"

    params = {
        "query": f'(gene:{query}) OR (protein_name:"{query}")',
        "format": "json",
        "size": 10,
        "fields": "accession,id,protein_name,gene_names,organism_name,length"
    }

    try:
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()

        data = response.json()
        results = data.get("results", [])

        if not results:
            return {
                "title": "Knowledge Report",
                "summary": f"No UniProt records were retrieved for '{query}'.",
                "direct_evidence": [],
                "related_evidence": [],
                "requires_verification": [],
                "references": []
            }

        direct_evidence = []
        related_evidence = []
        requires_verification = []

        query_lower = query.lower()

        for result in results:

            accession = result.get("primaryAccession", "N/A")

            protein_description = (
                result.get("proteinDescription", {})
                .get("recommendedName", {})
                .get("fullName", {})
                .get("value", "N/A")
            )

            organism = (
                result.get("organism", {})
                .get("scientificName", "N/A")
            )

            genes = result.get("genes", [])
            gene_name = "N/A"

            if genes:
                gene_name = (
                    genes[0]
                    .get("geneName", {})
                    .get("value", "N/A")
                )

            length = result.get("sequence", {}).get("length", "N/A")

            record = {
                "accession": accession,
                "protein": protein_description,
                "gene": gene_name,
                "organism": organism,
                "length": length
            }

            protein_lower = protein_description.lower()
            gene_lower = gene_name.lower()

            if (
                query_lower == gene_lower
                or query_lower in protein_lower
            ):
                direct_evidence.append(record)

            elif (
                "oxa" in gene_lower
                or "oxa" in protein_lower
            ):
                related_evidence.append(record)

            else:
                requires_verification.append(record)

        references = [
            f"UniProt: {record['accession']}"
            for record in (
                direct_evidence
                + related_evidence
                + requires_verification
            )
        ]

        return {
            "title": "Knowledge Report",
            "summary": (
                f"UniProt retrieval completed for '{query}': "
                f"{len(direct_evidence)} direct, "
                f"{len(related_evidence)} related, "
                f"{len(requires_verification)} requiring verification."
            ),
            "direct_evidence": direct_evidence,
            "related_evidence": related_evidence,
            "requires_verification": requires_verification,
            "references": references
        }

    except requests.RequestException as error:

        return {
            "title": "Knowledge Report",
            "summary": f"UniProt retrieval failed: {error}",
            "direct_evidence": [],
            "related_evidence": [],
            "requires_verification": [],
            "references": []
        }