"""Small deterministic AMR vocabulary used to stabilize query understanding.

The LLM remains responsible for interpreting the user's question.  This file
only provides high-confidence lexical aliases for common antibiotics and AMR
mechanism terms so a missing LLM extraction cannot silently remove a key
concept from a PubMed query.
"""

ANTIBIOTIC_ALIASES = {
    "colistin": ["colistin", "polymyxin E"],
    "polymyxin": ["polymyxin", "colistin", "polymyxin E"],
    "rifampicin": ["rifampicin", "rifampin"],
    "rifampin": ["rifampin", "rifampicin"],
    "isoniazid": ["isoniazid"],
    "vancomycin": ["vancomycin"],
    "linezolid": ["linezolid"],
    "daptomycin": ["daptomycin"],
    "methicillin": ["methicillin"],
    "oxacillin": ["oxacillin"],
    "penicillin": ["penicillin"],
    "ampicillin": ["ampicillin"],
    "amoxicillin": ["amoxicillin"],
    "piperacillin": ["piperacillin"],
    "ticarcillin": ["ticarcillin"],
    "cefotaxime": ["cefotaxime"],
    "ceftazidime": ["ceftazidime"],
    "ceftriaxone": ["ceftriaxone"],
    "cefepime": ["cefepime"],
    "cefoxitin": ["cefoxitin"],
    "ceftaroline": ["ceftaroline"],
    "meropenem": ["meropenem"],
    "imipenem": ["imipenem"],
    "ertapenem": ["ertapenem"],
    "doripenem": ["doripenem"],
    "ciprofloxacin": ["ciprofloxacin"],
    "levofloxacin": ["levofloxacin"],
    "moxifloxacin": ["moxifloxacin"],
    "norfloxacin": ["norfloxacin"],
    "gentamicin": ["gentamicin"],
    "amikacin": ["amikacin"],
    "tobramycin": ["tobramycin"],
    "streptomycin": ["streptomycin"],
    "tetracycline": ["tetracycline"],
    "doxycycline": ["doxycycline"],
    "minocycline": ["minocycline"],
    "tigecycline": ["tigecycline"],
    "azithromycin": ["azithromycin"],
    "clarithromycin": ["clarithromycin"],
    "erythromycin": ["erythromycin"],
    "chloramphenicol": ["chloramphenicol"],
    "trimethoprim": ["trimethoprim"],
    "sulfamethoxazole": ["sulfamethoxazole", "co-trimoxazole", "cotrimoxazole"],
    "fosfomycin": ["fosfomycin"],
    "nitrofurantoin": ["nitrofurantoin"],
    "aztreonam": ["aztreonam"],
    "ceftolozane": ["ceftolozane"],
    "avibactam": ["avibactam"],
    # Antibiotic *class* terms. A question about "fluoroquinolone resistance"
    # names a drug class, not one of the specific drugs above -- without
    # these, a query naming only a class (not a specific drug) extracted no
    # antibiotic entity at all whenever the LLM call also failed, which
    # silently removed the drug constraint from PubMed retrieval and from
    # evidence-tier scoring (both require a drug/antibiotic match to reach
    # the "direct evidence" tier).
    "fluoroquinolone": ["fluoroquinolone", "fluoroquinolones", "quinolone", "quinolones"],
    "carbapenem": ["carbapenem", "carbapenems"],
    "cephalosporin": ["cephalosporin", "cephalosporins"],
    "aminoglycoside": ["aminoglycoside", "aminoglycosides"],
    "glycopeptide": ["glycopeptide", "glycopeptides"],
    "macrolide": ["macrolide", "macrolides"],
    "beta-lactam": ["beta-lactam", "beta-lactams", "\u03b2-lactam", "\u03b2-lactams"],
    "sulfonamide": ["sulfonamide", "sulfonamides"],
}
ANTIBIOTIC_ALIASES["polymyxin"] = ["polymyxin", "colistin", "polymyxin E", "polymyxins"]
ANTIBIOTIC_ALIASES["penicillin"] = ["penicillin", "penicillins"]
ANTIBIOTIC_ALIASES["tetracycline"] = ["tetracycline", "tetracyclines"]

# Deterministic organism gazetteer. Organisms previously came ONLY from the
# LLM call -- when that call fails (see query_understanding._llm_understand),
# organisms silently ended up as an empty list with no fallback at all,
# which is the confirmed root cause of evidence from unrelated species
# dominating an organism-specific question. This mirrors the existing
# antibiotic/mechanism lexicon pattern: a small, high-confidence list of
# common AMR-relevant organisms and their standard abbreviations, so a named
# organism is never lost even when the LLM is unavailable. When a form is
# detected, every known surface form (full name + abbreviations) for that
# organism is returned, since downstream evidence matching does plain
# substring matching against paper titles/abstracts and a paper may use
# either the full name or the abbreviation.
ORGANISM_ALIASES = {
    "Escherichia coli": ["escherichia coli", "e. coli", "e coli"],
    "Klebsiella pneumoniae": ["klebsiella pneumoniae", "k. pneumoniae", "k pneumoniae"],
    "Acinetobacter baumannii": ["acinetobacter baumannii", "a. baumannii", "a baumannii"],
    "Pseudomonas aeruginosa": ["pseudomonas aeruginosa", "p. aeruginosa", "p aeruginosa"],
    "Mycobacterium tuberculosis": [
        "mycobacterium tuberculosis", "m. tuberculosis", "m tuberculosis", "m.tuberculosis",
        "mycobacterium tuberculosis complex", "m. tuberculosis complex",
        "mtb", "mtbc", "m. tb", "m.tb",
    ],
    "Staphylococcus aureus": ["staphylococcus aureus", "s. aureus", "s aureus"],
    "Enterococcus faecium": ["enterococcus faecium", "e. faecium", "e faecium"],
    "Enterococcus faecalis": ["enterococcus faecalis", "e. faecalis", "e faecalis"],
    "Streptococcus pneumoniae": ["streptococcus pneumoniae", "s. pneumoniae", "s pneumoniae"],
    "Neisseria gonorrhoeae": ["neisseria gonorrhoeae", "n. gonorrhoeae", "n gonorrhoeae"],
    "Helicobacter pylori": ["helicobacter pylori", "h. pylori", "h pylori"],
    "Salmonella enterica": ["salmonella enterica", "salmonella"],
    "Enterobacter cloacae": ["enterobacter cloacae", "e. cloacae"],
    "Campylobacter jejuni": ["campylobacter jejuni", "c. jejuni"],
    "Clostridioides difficile": ["clostridioides difficile", "clostridium difficile", "c. difficile", "c difficile"],
}

MECHANISM_ALIASES = {
    "beta-lactamase": ["beta-lactamase", "beta lactamase", "β-lactamase", "betalactamase",
                       "extended-spectrum beta-lactamase", "esbl", "AmpC", "carbapenemase"],
    "efflux pump": ["efflux pump", "efflux pumps", "efflux", "efflux system", "active efflux",
                    "RND efflux", "MexAB-OprM", "MexCD-OprJ", "MexXY-OprM", "AcrAB-TolC",
                    "MexAB", "MexCD", "MexXY", "AcrAB", "TolC", "OprM",
                    "resistance-nodulation-division", "RND transporter"],
    "porin": ["porin", "porins", "porin loss", "porin mutation", "porin deficiency",
              "outer membrane protein", "OmpC", "OmpF", "OmpK35", "OmpK36", "OprD",
              "porin alteration", "porin channel", "membrane channel"],
    "target modification": ["target modification", "target alteration", "target mutation",
                            "ribosomal modification", "penicillin-binding protein", "PBP",
                            "altered PBP", "modified PBP"],
    "target protection": ["target protection", "ribosomal protection"],
    "permeability": ["permeability", "outer membrane permeability", "membrane impermeability",
                     "reduced permeability", "decreased permeability"],
    "promoter mutation": ["promoter mutation", "promoter region", "promoter region mutation",
                          "upstream mutation", "promoter alteration", "regulatory mutation",
                          "overexpression", "-35 region", "-10 region"],
    "enzymatic inactivation": ["enzymatic inactivation", "drug inactivation",
                               "aminoglycoside-modifying enzyme", "acetyltransferase",
                               "phosphotransferase", "nucleotidyltransferase",
                               "chloramphenicol acetyltransferase", "CAT"],
    "lipid A modification": ["lipid A modification", "lipid A", "LPS modification",
                             "polymyxin resistance mechanism"],
    "phosphoethanolamine transfer": ["phosphoethanolamine", "phosphoethanolamine transferase",
                                     "MCR", "colistin resistance mechanism"],
    "methyltransferase": ["methyltransferase", "rRNA methyltransferase", "16S methyltransferase",
                          "23S rRNA methylation"],
}


# Lay terms that identify an organism only by the disease it causes. These are a
# WEAKER basis than a species name and are only used when no other organism is
# named in the same title/abstract (see agents/entity_aliases.py).
ORGANISM_DISEASE_TERMS = {
    "Mycobacterium tuberculosis": ["tuberculosis", "tubercle bacilli", "tubercle bacillus"],
}

# Genera used ONLY to recognise that a paper is about some OTHER organism
# (e.g. "Mycobacterium leprae", "Staphylococcus aureus"). A species outside the
# gazetteer above can then still be detected as a competing organism.
ORGANISM_GENERA = [
    "Acinetobacter", "Actinomyces", "Aeromonas", "Bacillus", "Bacteroides", "Bartonella",
    "Bordetella", "Borrelia", "Brucella", "Burkholderia", "Campylobacter", "Chlamydia",
    "Chlamydophila", "Citrobacter", "Clostridioides", "Clostridium", "Corynebacterium",
    "Coxiella", "Cutibacterium", "Edwardsiella", "Enterobacter", "Enterococcus", "Escherichia",
    "Francisella", "Fusobacterium", "Gardnerella", "Haemophilus", "Helicobacter", "Klebsiella",
    "Lactobacillus", "Legionella", "Leptospira", "Listeria", "Moraxella", "Morganella",
    "Mycobacterium", "Mycobacteroides", "Mycolicibacterium", "Mycoplasma", "Neisseria",
    "Nocardia", "Pantoea", "Pasteurella", "Peptostreptococcus", "Prevotella", "Proteus",
    "Providencia", "Pseudomonas", "Raoultella", "Rickettsia", "Salmonella", "Serratia",
    "Shigella", "Staphylococcus", "Stenotrophomonas", "Streptococcus", "Treponema",
    "Ureaplasma", "Vibrio", "Yersinia",
]

# True nomenclature synonyms (INN / USAN / BAN) -> one canonical name. These are
# the SAME drug, so they are merged into a single entity.
DRUG_SYNONYMS = {
    "rifampin": "rifampicin",
    "rifampicine": "rifampicin",
    "polymyxin e": "colistin",
}

# Standard abbreviations. Matched case-sensitively and never from "MTB/RIF"
# (the Xpert assay name) -- see entity_aliases.drug_hits.
DRUG_ABBREVIATIONS = {
    "rifampicin": ["RIF", "RMP", "RFP"],
    "isoniazid": ["INH"],
}

# Class term -> member drugs. A paper on ciprofloxacin and gyrA is evidence for a
# question about fluoroquinolone resistance.
DRUG_CLASS_MEMBERS = {
    "fluoroquinolone": ["ciprofloxacin", "levofloxacin", "moxifloxacin", "norfloxacin", "ofloxacin", "gatifloxacin"],
    "carbapenem": ["meropenem", "imipenem", "ertapenem", "doripenem"],
    "aminoglycoside": ["gentamicin", "amikacin", "tobramycin", "streptomycin", "kanamycin"],
    "macrolide": ["azithromycin", "clarithromycin", "erythromycin"],
    "cephalosporin": ["ceftriaxone", "cefotaxime", "ceftazidime", "cefepime"],
    "glycopeptide": ["vancomycin", "teicoplanin"],
}

# Residue-numbering schemes used in the literature for the same protein.
# E.g. M. tuberculosis rpoB S450L is reported as S531L in E. coli numbering.
# offset = (alternative-scheme position) - (reference position).
NUMBERING_SCHEMES = [
    {"gene": "rpob", "organism": "Mycobacterium tuberculosis", "scheme": "E. coli numbering", "offset": 81},
]


def _ordered_matches(query, aliases):
    q = (query or "").lower()
    matches = []
    seen = set()
    for canonical, forms in sorted(aliases.items(), key=lambda item: -max(len(x) for x in item[1])):
        if any(form.lower() in q for form in forms):
            if canonical not in seen:
                matches.append(canonical)
                seen.add(canonical)
    return matches


def extract_antibiotics(query):
    return _ordered_matches(query, ANTIBIOTIC_ALIASES)


def extract_mechanisms(query):
    return _ordered_matches(query, MECHANISM_ALIASES)


def extract_organisms(query):
    """Return every known surface form (full name + abbreviations) for each
    organism mentioned in the query, in any of its forms. Unlike
    _ordered_matches (which returns only the canonical key), this returns
    the full alias set so downstream literal substring matching against
    paper text catches both "Escherichia coli" and "E. coli"."""
    import re as _re
    q = (query or "").lower()
    found = []
    seen = set()
    for canonical, forms in ORGANISM_ALIASES.items():
        if any(_re.search(r"(?<![a-z0-9])" + _re.escape(form) + r"(?![a-z0-9])", q) for form in forms):
            for variant in (canonical, *forms):
                key = variant.lower()
                if key not in seen:
                    found.append(variant)
                    seen.add(key)
    return found


def antibiotic_search_terms(name):
    forms = ANTIBIOTIC_ALIASES.get(str(name).lower(), [name])
    return list(dict.fromkeys(forms))
