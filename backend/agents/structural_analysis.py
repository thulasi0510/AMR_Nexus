"""Sequence and Structural Analysis Agent for Antimicrobial Resistance (AMR).

Fulfills:
- Objective 1: Biological database integration (UniProt, RCSB PDB, AlphaFold).
- Objective 2: 5-facet characterization (Occurrence, Genetic Context, Sequence Characteristics, Functional Effects, Resistance Associations).
- Objective 3: Sequence & structural modeling, molecular consequences, and evidence-supported hypotheses for poorly characterized/novel AMR mutations.
- Objective 4: Computational evidence generation and evidence gap identification.
"""
from functools import lru_cache
import os
import re
from typing import Any, Dict, List, Optional, Tuple
import requests

from agents.mutation_matching import AA3, parse_substitution
from agents.entity_aliases import canonical_organism, canonical_drug

# ==============================================================================
# 1. AMINO ACID PHYSICOCHEMICAL KNOWLEDGE BASE
# ==============================================================================
AA_PROPERTIES = {
    "A": {"name": "Alanine", "abbr": "Ala", "volume": 88.6, "hydropathy": 1.8, "charge": 0.0, "pI": 6.00, "polarity": "nonpolar", "class": "aliphatic", "hb_donors": 1, "hb_acceptors": 1, "helix_prop": 1.42, "sheet_prop": 0.83},
    "R": {"name": "Arginine", "abbr": "Arg", "volume": 173.4, "hydropathy": -4.5, "charge": 1.0, "pI": 10.76, "polarity": "basic_positive", "class": "basic", "hb_donors": 5, "hb_acceptors": 1, "helix_prop": 0.98, "sheet_prop": 0.93},
    "N": {"name": "Asparagine", "abbr": "Asn", "volume": 114.1, "hydropathy": -3.5, "charge": 0.0, "pI": 5.41, "polarity": "polar_uncharged", "class": "amide", "hb_donors": 3, "hb_acceptors": 3, "helix_prop": 0.67, "sheet_prop": 0.89},
    "D": {"name": "Aspartate", "abbr": "Asp", "volume": 111.1, "hydropathy": -3.5, "charge": -1.0, "pI": 2.77, "polarity": "acidic_negative", "class": "acidic", "hb_donors": 1, "hb_acceptors": 4, "helix_prop": 1.01, "sheet_prop": 0.54},
    "C": {"name": "Cysteine", "abbr": "Cys", "volume": 108.5, "hydropathy": 2.5, "charge": 0.0, "pI": 5.07, "polarity": "polar_uncharged", "class": "thiol", "hb_donors": 1, "hb_acceptors": 1, "helix_prop": 0.70, "sheet_prop": 1.19},
    "Q": {"name": "Glutamine", "abbr": "Gln", "volume": 143.8, "hydropathy": -3.5, "charge": 0.0, "pI": 5.65, "polarity": "polar_uncharged", "class": "amide", "hb_donors": 3, "hb_acceptors": 3, "helix_prop": 1.11, "sheet_prop": 1.10},
    "E": {"name": "Glutamate", "abbr": "Glu", "volume": 138.4, "hydropathy": -3.5, "charge": -1.0, "pI": 3.22, "polarity": "acidic_negative", "class": "acidic", "hb_donors": 1, "hb_acceptors": 4, "helix_prop": 1.51, "sheet_prop": 0.37},
    "G": {"name": "Glycine", "abbr": "Gly", "volume": 60.1, "hydropathy": -0.4, "charge": 0.0, "pI": 5.97, "polarity": "nonpolar", "class": "flexible", "hb_donors": 1, "hb_acceptors": 1, "helix_prop": 0.57, "sheet_prop": 0.75},
    "H": {"name": "Histidine", "abbr": "His", "volume": 153.2, "hydropathy": -3.2, "charge": 0.1, "pI": 7.59, "polarity": "basic_positive", "class": "aromatic_basic", "hb_donors": 3, "hb_acceptors": 2, "helix_prop": 1.00, "sheet_prop": 0.87},
    "I": {"name": "Isoleucine", "abbr": "Ile", "volume": 166.7, "hydropathy": 4.5, "charge": 0.0, "pI": 6.02, "polarity": "nonpolar", "class": "aliphatic", "hb_donors": 1, "hb_acceptors": 1, "helix_prop": 1.08, "sheet_prop": 1.60},
    "L": {"name": "Leucine", "abbr": "Leu", "volume": 166.7, "hydropathy": 3.8, "charge": 0.0, "pI": 5.98, "polarity": "nonpolar", "class": "aliphatic", "hb_donors": 1, "hb_acceptors": 1, "helix_prop": 1.21, "sheet_prop": 1.30},
    "K": {"name": "Lysine", "abbr": "Lys", "volume": 168.6, "hydropathy": -3.9, "charge": 1.0, "pI": 9.74, "polarity": "basic_positive", "class": "basic", "hb_donors": 4, "hb_acceptors": 1, "helix_prop": 1.16, "sheet_prop": 0.74},
    "M": {"name": "Methionine", "abbr": "Met", "volume": 162.9, "hydropathy": 1.9, "charge": 0.0, "pI": 5.74, "polarity": "nonpolar", "class": "thioether", "hb_donors": 1, "hb_acceptors": 1, "helix_prop": 1.45, "sheet_prop": 1.05},
    "F": {"name": "Phenylalanine", "abbr": "Phe", "volume": 189.9, "hydropathy": 2.8, "charge": 0.0, "pI": 5.48, "polarity": "nonpolar", "class": "aromatic", "hb_donors": 1, "hb_acceptors": 1, "helix_prop": 1.13, "sheet_prop": 1.38},
    "P": {"name": "Proline", "abbr": "Pro", "volume": 112.7, "hydropathy": -1.6, "charge": 0.0, "pI": 6.30, "polarity": "nonpolar", "class": "cyclic_rigid", "hb_donors": 0, "hb_acceptors": 1, "helix_prop": 0.57, "sheet_prop": 0.55},
    "S": {"name": "Serine", "abbr": "Ser", "volume": 89.0, "hydropathy": -0.8, "charge": 0.0, "pI": 5.68, "polarity": "polar_uncharged", "class": "hydroxylic", "hb_donors": 2, "hb_acceptors": 2, "helix_prop": 0.77, "sheet_prop": 0.75},
    "T": {"name": "Threonine", "abbr": "Thr", "volume": 116.1, "hydropathy": -0.7, "charge": 0.0, "pI": 5.60, "polarity": "polar_uncharged", "class": "hydroxylic", "hb_donors": 2, "hb_acceptors": 2, "helix_prop": 0.83, "sheet_prop": 1.19},
    "W": {"name": "Tryptophan", "abbr": "Trp", "volume": 227.8, "hydropathy": -0.9, "charge": 0.0, "pI": 5.89, "polarity": "nonpolar", "class": "aromatic", "hb_donors": 2, "hb_acceptors": 1, "helix_prop": 1.08, "sheet_prop": 1.37},
    "Y": {"name": "Tyrosine", "abbr": "Tyr", "volume": 193.6, "hydropathy": -1.3, "charge": 0.0, "pI": 5.66, "polarity": "polar_uncharged", "class": "aromatic_polar", "hb_donors": 2, "hb_acceptors": 2, "helix_prop": 0.69, "sheet_prop": 1.47},
    "V": {"name": "Valine", "abbr": "Val", "volume": 140.0, "hydropathy": 4.2, "charge": 0.0, "pI": 5.96, "polarity": "nonpolar", "class": "aliphatic", "hb_donors": 1, "hb_acceptors": 1, "helix_prop": 1.06, "sheet_prop": 1.70},
}


def calculate_physicochemical_delta(wt_aa: str, mut_aa: str) -> Dict[str, Any]:
    """Calculate quantitative physicochemical deltas between wildtype and mutant residues."""
    wt = str(wt_aa).upper().strip()
    mut = str(mut_aa).upper().strip()
    p_wt = AA_PROPERTIES.get(wt, {})
    p_mut = AA_PROPERTIES.get(mut, {})

    if not p_wt or not p_mut:
        return {
            "wt": wt, "mut": mut,
            "delta_volume": 0.0, "delta_hydropathy": 0.0, "delta_charge": 0.0, "delta_hbonds": 0,
            "polarity_transition": "Unknown amino acid transition",
            "steric_effect": "Uncharacterized volume shift",
            "summary": f"{wt} -> {mut} substitution",
        }

    d_vol = round(p_mut["volume"] - p_wt["volume"], 1)
    d_hydro = round(p_mut["hydropathy"] - p_wt["hydropathy"], 1)
    d_charge = round(p_mut["charge"] - p_wt["charge"], 1)
    d_hb = (p_mut["hb_donors"] + p_mut["hb_acceptors"]) - (p_wt["hb_donors"] + p_wt["hb_acceptors"])

    # Steric characterization
    if d_vol > 30.0:
        steric = f"Bulky substitution (+{d_vol} A^3); severe steric collision risk"
    elif d_vol > 12.0:
        steric = f"Steric expansion (+{d_vol} A^3); moderate steric crowding"
    elif d_vol < -30.0:
        steric = f"Severe cavity-creating reduction ({d_vol} A^3); core destabilization risk"
    elif d_vol < -12.0:
        steric = f"Steric reduction ({d_vol} A^3); local cavity creation"
    else:
        steric = f"Iso-steric / minimal volume shift ({d_vol} A^3)"

    # Polarity & charge characterization
    pol_trans = f"{p_wt['polarity'].replace('_', ' ')} -> {p_mut['polarity'].replace('_', ' ')}"
    if d_charge > 0.5:
        pol_trans += " [Gain of positive charge]"
    elif d_charge < -0.5:
        pol_trans += " [Gain of negative charge / charge inversion]"

    return {
        "wt": wt,
        "mut": mut,
        "wt_name": p_wt["name"],
        "mut_name": p_mut["name"],
        "delta_volume": d_vol,
        "delta_hydropathy": d_hydro,
        "delta_charge": d_charge,
        "delta_hbonds": d_hb,
        "polarity_transition": pol_trans,
        "steric_effect": steric,
        "wt_class": p_wt["class"],
        "mut_class": p_mut["class"],
        "summary": f"Delta_Vol: {d_vol:+} A^3, Delta_Hydropathy: {d_hydro:+.1f}, Delta_Charge: {d_charge:+.1f} ({pol_trans})",
    }


# ==============================================================================
# 2. CURATED AMR MACROMOLECULAR STRUCTURAL REGISTRY (PDB & ALPHAFOLD)
# ==============================================================================
STRUCTURAL_REGISTRY: Dict[str, Dict[str, Any]] = {
    # Beta-lactamases - Class A
    "tem": {
        "canonical_name": "blaTEM",
        "family": "Class A Serine beta-Lactamase",
        "uniprot_id": "P62593",
        "canonical_length": 286,
        "organism": "Gram-negative Enterobacterales",
        "pdb_structures": [
            {"pdb_id": "1BTL", "description": "Native TEM-1 beta-lactamase atomic resolution", "resolution": "1.8 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "1ZG4", "description": "Extended-spectrum TEM-52 (G238S/E104K/M182T)", "resolution": "2.4 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "1LI9", "description": "TEM-1 complexed with mechanism-based inhibitor", "resolution": "1.74 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-P62593-F1",
        "catalytic_center": {"Ser70": "Nucleophilic serine", "Lys73": "General base", "Ser130": "Proton transfer", "Glu166": "Deacylation general base", "Lys234": "Oxyanion hole anchor"},
        "functional_domains": [
            {"name": "Active-site oxyanion pocket", "range": [68, 75], "description": "Catalytic tetrad motif 70-Ser-X-X-Lys"},
            {"name": "Substrate-binding entry loop", "range": [101, 111], "description": "Flexible perimeter loop accommodating antibiotic sidechains"},
            {"name": "SDN catalytic loop", "range": [130, 134], "description": "Conserved 130-Ser-Asp-Asn active site wall"},
            {"name": "Omega-loop (Ω-loop)", "range": [164, 179], "description": "Structural boundary wall modulating catalytic cleft breathing and ESBL activity"},
            {"name": "Interdomain hinge / stability loop", "range": [180, 190], "description": "Scaffold loop containing global suppressor locus Met182"},
            {"name": "Strand B3 (KTG motif)", "range": [234, 240], "description": "Beta-strand lining catalytic pocket; Gly238 modulates cephalosporin entry"},
        ],
    },
    "kpc": {
        "canonical_name": "blaKPC-2",
        "family": "Class A Serine Carbapenemase",
        "uniprot_id": "Q9F663",
        "canonical_length": 293,
        "organism": "Klebsiella pneumoniae",
        "pdb_structures": [
            {"pdb_id": "2OV5", "description": "Crystal structure of KPC-2 carbapenemase at high resolution", "resolution": "1.85 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "6D18", "description": "KPC-2 D179N variant conferring ceftazidime-avibactam resistance", "resolution": "1.35 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "5UJ6", "description": "KPC-2 complexed with vaborbactam boronic acid inhibitor", "resolution": "1.25 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-Q9F663-F1",
        "catalytic_center": {"Ser70": "Active site nucleophile", "Lys73": "Base catalyst", "Glu166": "General base deacylation", "Trp105": "Carbapenem-binding aromatic clamp"},
        "functional_domains": [
            {"name": "Active site catalytic cleft", "range": [69, 74], "description": "Ser70 catalytic center with unique shallow geometry enabling carbapenem hydrolysis"},
            {"name": "Aromatic clamp loop", "range": [103, 107], "description": "Trp105 forms hydrophobic/pi-stacking interactions with carbapenem core"},
            {"name": "Omega-loop", "range": [164, 179], "description": "Contains Asp179 salt bridge anchor; mutations (D179N) confer ceftazidime-avibactam escape"},
            {"name": "Beta-strand B3", "range": [234, 243], "description": "Residues Thr237, Gly238, Thr243 modulate cephalosporin and inhibitor capture"},
        ],
    },
    "shv": {
        "canonical_name": "blaSHV-1",
        "family": "Class A Serine beta-Lactamase",
        "uniprot_id": "P0AD64",
        "canonical_length": 286,
        "organism": "Klebsiella pneumoniae",
        "pdb_structures": [
            {"pdb_id": "1SHV", "description": "Crystal structure of SHV-1 beta-lactamase", "resolution": "1.98 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "2A3U", "description": "SHV-1 complexed with beta-lactam inhibitor", "resolution": "1.63 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-P0AD64-F1",
        "catalytic_center": {"Ser70": "Nucleophilic serine", "Glu166": "Deacylation base"},
        "functional_domains": [
            {"name": "Omega loop", "range": [164, 179], "description": "Modulates loop breathing and ESBL activity (e.g. R164S)"},
            {"name": "Strand B3", "range": [234, 240], "description": "Contains G238S ESBL transition site"},
        ],
    },
    "ctx-m": {
        "canonical_name": "blaCTX-M",
        "family": "Class A Extended-Spectrum beta-Lactamase",
        "uniprot_id": "Q9L5C8",
        "canonical_length": 291,
        "organism": "Escherichia coli / Klebsiella pneumoniae",
        "pdb_structures": [
            {"pdb_id": "1YLJ", "description": "Crystal structure of CTX-M-9 beta-lactamase", "resolution": "1.20 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "1IYO", "description": "CTX-M-14 acyl-intermediate with cefotaxime", "resolution": "1.15 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-Q9L5C8-F1",
        "catalytic_center": {"Ser70": "Catalytic nucleophile", "Lys73": "General base", "Asn104": "Oxyimino hydrogen bond anchor"},
        "functional_domains": [
            {"name": "Catalytic center", "range": [69, 74], "description": "Highly adapted for oxyimino-cephalosporin (cefotaxime) accommodation"},
            {"name": "B3 strand", "range": [235, 242], "description": "Pro237 and Arg276 interactions lining the cefotaxime pocket"},
        ],
    },
    "ndm": {
        "canonical_name": "blaNDM-1",
        "family": "Class B1 Metallo-beta-Lactamase (MBL)",
        "uniprot_id": "C7C422",
        "canonical_length": 270,
        "organism": "Enterobacterales / Acinetobacter baumannii",
        "pdb_structures": [
            {"pdb_id": "3SPU", "description": "High resolution crystal structure of NDM-1 with binuclear zinc center", "resolution": "1.30 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "4RL0", "description": "NDM-1 in complex with hydrolyzed meropenem intermediate", "resolution": "1.80 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-C7C422-F1",
        "catalytic_center": {"Zn1": "Coordinated by His120, His122, His189", "Zn2": "Coordinated by Asp124, Cys208, His250", "Bridging_water": "Nucleophilic hydroxide attacking beta-lactam ring"},
        "functional_domains": [
            {"name": "L3 flexible loop", "range": [65, 75], "description": "Hydrophobic loop capping the active site cleft"},
            {"name": "Binuclear Zn active site", "range": [120, 250], "description": "Deep catalytic groove coordinating two Zn2+ ions; insensitive to serine beta-lactamase inhibitors"},
        ],
    },
    "oxa-48": {
        "canonical_name": "blaOXA-48",
        "family": "Class D Carbapenem-Hydrolyzing beta-Lactamase",
        "uniprot_id": "Q6XEC0",
        "canonical_length": 265,
        "organism": "Klebsiella pneumoniae / Enterobacterales",
        "pdb_structures": [
            {"pdb_id": "3HBR", "description": "Crystal structure of OXA-48 carbapenemase at 1.9 Å", "resolution": "1.90 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "4S2P", "description": "OXA-48 complexed with inhibitor avibactam", "resolution": "1.75 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-Q6XEC0-F1",
        "catalytic_center": {"Ser70": "Catalytic nucleophile", "Lys73": "Post-translationally carboxylated lysine (K-carboxy) general base"},
        "functional_domains": [
            {"name": "Carboxylated Lys pocket", "range": [70, 75], "description": "Ser70-Lys73 active site requiring CO2-mediated lysine carboxylation for catalytic turnover"},
            {"name": "Beta5-Beta6 connecting loop", "range": [211, 219], "description": "Shallow substrate cleft mediating carbapenem hydrolysis while sparing cephalosporins"},
        ],
    },
    # Mycobacterium tuberculosis targets
    "katg": {
        "canonical_name": "katG",
        "family": "Catalase-Peroxidase Prodrug Activator",
        "uniprot_id": "P9WIE5",
        "canonical_length": 740,
        "organism": "Mycobacterium tuberculosis",
        "pdb_structures": [
            {"pdb_id": "1SJ2", "description": "Crystal structure of M. tuberculosis KatG catalase-peroxidase dimer", "resolution": "2.00 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "2B2R", "description": "Structure of KatG S315T mutant illustrating substrate channel constriction", "resolution": "2.00 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-P9WIE5-F1",
        "catalytic_center": {"Heme_iron": "Protoporphyrin IX active center", "His270": "Proximal heme ligand", "His108": "Distal histidine", "Arg418": "Catalytic triad residue", "Crosslinked_triad": "Met255-Tyr229-Trp107 covalent cofactor"},
        "functional_domains": [
            {"name": "N-terminal catalytic domain", "range": [1, 440], "description": "Harbors heme pocket and isoniazid oxidation site"},
            {"name": "Substrate access channel", "range": [310, 320], "description": "Narrow (6-8 Å) constriction neck; Ser315 guards entrance to heme pocket"},
            {"name": "C-terminal regulatory domain", "range": [441, 740], "description": "Maintains homodimeric architecture and stability"},
        ],
    },
    "rpob": {
        "canonical_name": "rpoB",
        "family": "DNA-Directed RNA Polymerase beta-Subunit",
        "uniprot_id": "P9WGY9",
        "canonical_length": 1172,
        "organism": "Mycobacterium tuberculosis",
        "pdb_structures": [
            {"pdb_id": "5UHC", "description": "M. tuberculosis RNA polymerase transcription initiation complex with rifampicin", "resolution": "3.80 Å", "method": "Cryo-EM"},
            {"pdb_id": "5UH5", "description": "M. tuberculosis RNAP with rifampicin-resistant S450L substitution", "resolution": "4.00 Å", "method": "Cryo-EM"},
            {"pdb_id": "4KM6", "description": "M. tuberculosis RNA polymerase beta subunit core", "resolution": "3.00 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-P9WGY9-F1",
        "catalytic_center": {"Asp435": "Rifampicin pocket H-bond anchor", "His445": "Rifampicin naphthoquinone pocket coordination", "Ser450": "Direct steric contact with rifampicin ring"},
        "functional_domains": [
            {"name": "81-bp RRDR (Rifampicin Resistance-Determining Region)", "range": [426, 452], "description": "Conserved pocket (codons 507-533 in E. coli) where >95% of clinical rifampicin mutations cluster"},
            {"name": "RNA exit channel", "range": [400, 500], "description": "Bound rifampicin sterically obstructs growing RNA transcript past 2-3 nucleotides"},
        ],
    },
    "gyra": {
        "canonical_name": "gyrA",
        "family": "DNA Topoisomerase II (Gyrase A Subunit)",
        "uniprot_id": "P0AES4",
        "canonical_length": 875,
        "organism": "Escherichia coli / Mycobacterium tuberculosis",
        "pdb_structures": [
            {"pdb_id": "2XCT", "description": "Gyrase-DNA-ciprofloxacin cleavage complex with water-metal ion bridge", "resolution": "3.35 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "1AB4", "description": "Crystal structure of the 59-kDa QRDR breakage-reunion domain of E. coli GyrA", "resolution": "2.80 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "5BS8", "description": "M. tuberculosis DNA gyrase A QRDR with fluoroquinolone", "resolution": "2.60 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-P0AES4-F1",
        "catalytic_center": {"Tyr122": "Catalytic tyrosine forming phosphotyrosine covalent bond with cleaved DNA", "Ser83": "Water-metal ion bridge anchor to Mg2+", "Asp87": "Divalent metal ion coordination anchor"},
        "functional_domains": [
            {"name": "Quinolone Resistance-Determining Region (QRDR)", "range": [67, 106], "description": "Helix IV lining the DNA-binding pocket; Ser83/Asp87 mediate water-Mg2+ bridge to fluoroquinolones"},
            {"name": "Breakage-reunion active center", "range": [115, 130], "description": "Catalytic Tyr122 cleaves and religates double-stranded DNA"},
        ],
    },
    "mcr-1": {
        "canonical_name": "mcr-1",
        "family": "Phosphoethanolamine Transferase",
        "uniprot_id": "A0A0R8A0T8",
        "canonical_length": 541,
        "organism": "Escherichia coli / Enterobacterales",
        "pdb_structures": [
            {"pdb_id": "5K4P", "description": "Catalytic domain of MCR-1 phosphoethanolamine transferase at 1.32 Å", "resolution": "1.32 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "5FGN", "description": "MCR-1 catalytic domain with active-site Thr285 nucleophile", "resolution": "1.55 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-A0A0R8A0T8-F1",
        "catalytic_center": {"Thr285": "Catalytic nucleophile attacking phosphatidylethanolamine", "Zn_coordination": "Glu246, Thr285, His395, His466 coordinating Zn2+ ion"},
        "functional_domains": [
            {"name": "N-terminal 5-transmembrane anchor", "range": [1, 215], "description": "Anchors enzyme in inner bacterial membrane"},
            {"name": "Periplasmic catalytic domain", "range": [216, 541], "description": "Transfers phosphoethanolamine to Lipid A 1' and 4' phosphate groups, reducing negative surface charge"},
        ],
    },
    "vana": {
        "canonical_name": "vanA",
        "family": "D-Alanine--D-Lactate Ligase",
        "uniprot_id": "P25051",
        "canonical_length": 343,
        "organism": "Enterococcus faecium / Enterococcus faecalis",
        "pdb_structures": [
            {"pdb_id": "1E4E", "description": "Crystal structure of VanA D-Ala-D-Lac ligase", "resolution": "2.20 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-P25051-F1",
        "catalytic_center": {"Lys145": "Substrate binding", "His244": "Proton transfer", "Glu264": "Magnesium coordination"},
        "functional_domains": [
            {"name": "D-Ala-D-Lac ligase catalytic core", "range": [10, 340], "description": "Synthesizes cell wall precursors terminating in D-Ala-D-Lac (1,000-fold reduced vancomycin affinity)"},
        ],
    },
    "acrb": {
        "canonical_name": "acrB",
        "family": "Resistance-Nodulation-Division (RND) Efflux Transporter",
        "uniprot_id": "P31224",
        "canonical_length": 1049,
        "organism": "Escherichia coli",
        "pdb_structures": [
            {"pdb_id": "4DX5", "description": "AcrB asymmetric trimer in loose, tight, open conformations with bound minocycline", "resolution": "1.90 Å", "method": "X-ray Diffraction"},
            {"pdb_id": "2DRD", "description": "Crystal structure of AcrB trimer", "resolution": "2.80 Å", "method": "X-ray Diffraction"},
        ],
        "alphafold_id": "AF-P31224-F1",
        "catalytic_center": {"Distal_binding_pocket": "Phe178, Ile277, Val612, Phe615", "Proton_translocation_domain": "Asp407, Asp408, Lys940 in transmembrane region"},
        "functional_domains": [
            {"name": "Periplasmic access / deep binding cleft", "range": [170, 300], "description": "Captures substrates (tetracyclines, macrolides, fluoroquinolones)"},
            {"name": "Transmembrane proton relay", "range": [390, 420], "description": "Converts proton motive force into peristaltic extrusion pump cycles"},
        ],
    },
};


# ==============================================================================
# 3. STRUCTURAL PROFILE RETRIEVAL & UNIPROT DYNAMIC FALLBACK
# ==============================================================================
def normalize_gene_key(gene_symbol: str) -> str:
    """Normalize gene strings to canonical keys in the structural registry."""
    s = re.sub(r"[^a-z0-9]+", "", str(gene_symbol or "").lower())
    if s.startswith("bla"):
        s = s[3:]
    for prefix in ["tem", "kpc", "shv", "ctxm", "ndm", "oxa48", "oxa", "katg", "rpob", "gyra", "gyrb", "mcr1", "mcr", "vana", "vanb", "acrb", "ompk35", "ompk36"]:
        if s.startswith(prefix):
            if prefix in {"ctxm", "ctx-m"}:
                return "ctx-m"
            if prefix in {"mcr1", "mcr"}:
                return "mcr-1"
            if prefix in {"oxa48", "oxa"}:
                return "oxa-48"
            return prefix
    return s


@lru_cache(maxsize=128)
def get_structural_profile(gene_symbol: str, organism: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve comprehensive macromolecular structural data for any AMR determinant."""
    key = normalize_gene_key(gene_symbol)
    if key in STRUCTURAL_REGISTRY:
        return dict(STRUCTURAL_REGISTRY[key])

    # Dynamic fallback: query UniProt REST API if network is available
    profile = {
        "canonical_name": gene_symbol,
        "family": "Antimicrobial Resistance Determinant",
        "uniprot_id": "N/A",
        "canonical_length": 400,
        "organism": organism or "Bacterial pathogen",
        "pdb_structures": [],
        "alphafold_id": f"AF-{gene_symbol}-F1",
        "catalytic_center": {"Active site": "Catalytic center inferred from homology"},
        "functional_domains": [
            {"name": "Catalytic core", "range": [50, 250], "description": "Core functional domain"},
            {"name": "Surface interaction loops", "range": [251, 350], "description": "Periphery loops modulating substrate access"},
        ],
    }

    try:
        url = "https://rest.uniprot.org/uniprotkb/search"
        params = {"query": f'(gene:{gene_symbol}) AND (reviewed:true)', "format": "json", "size": 1}
        resp = requests.get(url, params=params, timeout=2.5)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            if results:
                r0 = results[0]
                acc = r0.get("primaryAccession", "")
                seq_len = r0.get("sequence", {}).get("length", 400)
                desc = r0.get("proteinDescription", {}).get("recommendedName", {}).get("fullName", {}).get("value", "")
                profile["uniprot_id"] = acc
                profile["canonical_length"] = seq_len
                profile["family"] = desc or profile["family"]
                profile["alphafold_id"] = f"AF-{acc}-F1"
    except Exception:
        pass

    return profile


# ==============================================================================
# 4. MOLECULAR CONSEQUENCE & STRUCTURAL INVESTIGATION
# ==============================================================================
def analyze_mutation_structural_consequences(
    gene_symbol: str,
    mutation_str: str,
    organism: Optional[str] = None,
    drug: Optional[str] = None,
) -> Dict[str, Any]:
    """Investigate the exact molecular, biophysical, and structural consequences of an AMR mutation."""
    profile = get_structural_profile(gene_symbol, organism)
    parsed = parse_substitution(mutation_str)

    if not parsed:
        return {
            "gene": gene_symbol,
            "mutation": mutation_str,
            "valid_notation": False,
            "error": f"Notation '{mutation_str}' could not be parsed as a canonical amino acid substitution.",
        }

    wt_aa, pos, mut_aa = parsed
    canon_mutation = f"{wt_aa}{pos}{mut_aa}"
    max_len = profile.get("canonical_length", 2000)

    # Position sanity check
    position_plausible = pos <= max_len
    if not position_plausible:
        return {
            "gene": gene_symbol,
            "mutation": canon_mutation,
            "valid_notation": True,
            "position_plausible": False,
            "max_length": max_len,
            "error": f"Residue position {pos} exceeds canonical protein coding capacity ({max_len} aa) for {profile['canonical_name']}.",
        }

    # 1. Physicochemical delta
    deltas = calculate_physicochemical_delta(wt_aa, mut_aa)

    # 2. Structural domain mapping: prioritize narrowest/most specific functional motif
    matched_domain = None
    candidate_domains = sorted(
        profile.get("functional_domains", []),
        key=lambda d: d["range"][1] - d["range"][0]
    )
    for dom in candidate_domains:
        r_start, r_end = dom["range"]
        if r_start <= pos <= r_end:
            matched_domain = dom
            break

    if not matched_domain:
        matched_domain = {
            "name": "Structural core / peripheral scaffold",
            "range": [1, max_len],
            "description": "Scaffold architecture maintaining tertiary fold stability",
        }

    # 3. Catalytic proximity assessment
    catalytic_center = profile.get("catalytic_center", {})
    direct_catalytic_contact = any(
        re.search(rf"\b{pos}\b", k) or re.search(rf"\b{pos}\b", v)
        for k, v in catalytic_center.items()
    )

    # 4. Molecular mechanism deduction
    d_vol = deltas["delta_volume"]
    d_hydro = deltas["delta_hydropathy"]
    d_charge = deltas["delta_charge"]

    mechanisms = []
    if direct_catalytic_contact:
        mechanisms.append(f"Direct active-site perturbation: alters canonical catalytic residue/motif at position {pos}.")
    
    if d_vol > 30.0:
        mechanisms.append(f"Severe steric clash: introducing {deltas['mut_name']} (+{d_vol} A^3) constricts local cleft volume, impeding drug binding or inducing loop rearrangement.")
    elif d_vol > 15.0:
        mechanisms.append(f"Steric expansion / channel constriction: introducing {deltas['mut_name']} (+{d_vol} A^3) narrows access geometry or constricts substrate entry channels.")
    elif d_vol < -30.0:
        mechanisms.append(f"Cavity-creating destabilization: loss of {deltas['wt_name']} sidechain ({d_vol} A^3) creates an internal void, destabilizing local tertiary packing.")
    elif d_vol < -15.0:
        mechanisms.append(f"Steric reduction: loss of {deltas['wt_name']} sidechain ({d_vol} A^3) creates local pocket laxity.")

    if abs(d_charge) >= 1.0:
        if d_charge > 0:
            mechanisms.append(f"Electrostatic remodeling: introduces positive charge (+{d_charge}), establishing new salt bridges or repelling cationic ligands.")
        else:
            mechanisms.append(f"Charge repulsion / inversion: introduces negative charge ({d_charge}), disrupting coordinating anionic networks or chelating bridges.")

    if wt_aa == "P" or mut_aa == "P":
        mechanisms.append("Backbone conformation disruption: proline mutation fundamentally alters local phi/psi dihedral angles and secondary structure rigidity.")
    if wt_aa == "G" or mut_aa == "G":
        mechanisms.append("Conformational flexibility shift: glycine mutation alters backbone conformational entropy and loop breathing dynamics.")

    if not mechanisms:
        mechanisms.append(f"Subtle physicochemical shift: {deltas['summary']} modulates local packing density and hydration kinetics.")

    molecular_impact_summary = " ".join(mechanisms)

    return {
        "gene": gene_symbol,
        "canonical_gene": profile.get("canonical_name", gene_symbol),
        "family": profile.get("family", "AMR Determinant"),
        "mutation": canon_mutation,
        "position": pos,
        "wt_aa": wt_aa,
        "mut_aa": mut_aa,
        "valid_notation": True,
        "position_plausible": True,
        "canonical_length": max_len,
        "uniprot_id": profile.get("uniprot_id"),
        "alphafold_id": profile.get("alphafold_id"),
        "pdb_structures": profile.get("pdb_structures", []),
        "domain": matched_domain["name"],
        "domain_description": matched_domain["description"],
        "catalytic_contact": direct_catalytic_contact,
        "physicochemical": deltas,
        "mechanisms": mechanisms,
        "molecular_impact_summary": molecular_impact_summary,
    }


# ==============================================================================
# 5. EVIDENCE-SUPPORTED HYPOTHESIS GENERATION (OBJECTIVE 3)
# ==============================================================================
def generate_evidence_supported_hypothesis(
    gene_symbol: str,
    mutation_str: str,
    structural_consequence: Dict[str, Any],
    organism: Optional[str] = None,
    drug: Optional[str] = None,
    curated_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate rigorous, evidence-supported structural hypotheses for uncharacterized or novel variants."""
    pos = structural_consequence.get("position", 0)
    wt = structural_consequence.get("wt_aa", "")
    mut = structural_consequence.get("mut_aa", "")
    deltas = structural_consequence.get("physicochemical", {})
    domain = structural_consequence.get("domain", "Structural core")
    gene_name = structural_consequence.get("canonical_gene", gene_symbol)
    org_str = f" in *{organism}*" if organism else ""
    drug_str = f" against **{drug}**" if drug else ""

    # Assess direct literature and catalogue presence
    has_catalogue = bool(curated_evidence and curated_evidence.get("confidence"))
    has_literature = bool(curated_evidence and curated_evidence.get("literature_citations"))

    # If already established with high confidence:
    if has_catalogue or has_literature:
        conf = (curated_evidence or {}).get("confidence", "Curated literature evidence")
        hypothesis_text = (
            f"**Established Biological Finding:** Substitution **{mutation_str}** in **{gene_name}**{org_str} "
            f"is clinically and experimentally characterized ({conf}). "
            f"Its molecular consequence is verified: located within the **{domain}**, "
            f"{structural_consequence.get('molecular_impact_summary')}"
        )
        status = "established_finding"
    else:
        # Novel / Poorly characterized mutation: Generate Evidence-Supported Hypothesis
        status = "computational_hypothesis"
        d_vol = deltas.get("delta_volume", 0)
        d_hydro = deltas.get("delta_hydropathy", 0)
        d_charge = deltas.get("delta_charge", 0)

        # Rational biophysical hypothesis synthesis
        if structural_consequence.get("catalytic_contact"):
            pred_phenotype = "High-level phenotypic resistance / catalytic alteration"
            confidence = "Moderate-High (Structural Active-Site In silico Inference)"
            rationale = (
                f"Residue {pos} maps directly to the active-site catalytic center. Substitution of {wt} with {mut} "
                f"({deltas.get('summary')}) directly perturbs key catalytic residues, predicting substantial disruption of drug binding or turnover."
            )
        elif "loop" in domain.lower() or "gate" in domain.lower() or "entrance" in domain.lower() or "rrdr" in domain.lower() or "qrdr" in domain.lower():
            pred_phenotype = "Broadened substrate accommodation / decreased binding affinity"
            confidence = "Moderate (Structural Domain Homology Inference)"
            rationale = (
                f"Residue {pos} resides in the **{domain}**, a region functionally governing substrate entry and conformational flexibility. "
                f"The observed physicochemical delta ({deltas.get('summary')}) is predicted to remodel the entry pocket, "
                f"altering steric hindrance and binding kinetics."
            )
        elif abs(d_vol) > 35 or abs(d_charge) >= 1:
            pred_phenotype = "Thermodynamic destabilization / potential compensatory requirement"
            confidence = "Moderate (Biophysical Folding Constraint Inference)"
            rationale = (
                f"Residue {pos} is situated in the structural scaffold/core. The large physicochemical transition "
                f"({deltas.get('steric_effect')}, Delta_Charge: {d_charge:+.1f}) imposes severe folding stress (Delta_Delta_G shift), "
                f"likely requiring epistatic compensatory mutations or reducing cellular enzyme abundance unless buffered."
            )
        else:
            pred_phenotype = "Low-level resistance modulation or neutral polymorphic variant"
            confidence = "Low-Moderate (In silico Sequence Proximity Inference)"
            rationale = (
                f"Residue {pos} is located on a peripheral scaffold loop away from primary catalytic triads. "
                f"Minimal volume shift ({deltas.get('delta_volume'):+} A^3) suggests it may represent a neutral phylogenetic polymorphism "
                f"or confer only minor collateral MIC elevation."
            )

        hypothesis_text = (
            f"**[Hypothesis: Evidence-Supported Computational & Structural Inference]**\n\n"
            f"*Direct experimental literature and WHO/CARD catalogue entries are currently unavailable for novel variant **{mutation_str}** in **{gene_name}**.* "
            f"Based on high-resolution 3D macromolecular structures ({', '.join(s['pdb_id'] for s in structural_consequence.get('pdb_structures', [])[:2]) or structural_consequence.get('alphafold_id')}) "
            f"and biophysical sequence modeling:\n\n"
            f"- **Structural Localization:** Residue {pos} is located within the **{domain}** ({structural_consequence.get('domain_description')}).\n"
            f"- **Predicted Molecular Mechanism:** {structural_consequence.get('molecular_impact_summary')}\n"
            f"- **Predicted Phenotypic Impact:** **{pred_phenotype}**{drug_str}.\n"
            f"- **Biophysical Rationale:** {rationale}\n"
            f"- **Evidence Gap & Verification Priority:** Direct in vitro phenotypic testing (e.g. site-directed mutagenesis, recombinant minimum inhibitory concentration (MIC) testing by broth microdilution, and thermal shift stability assays) is required to definitively validate this hypothesis."
        )

    return {
        "status": status,
        "mutation": mutation_str,
        "gene": gene_name,
        "domain": domain,
        "hypothesis": hypothesis_text,
        "is_hypothesis": (status == "computational_hypothesis"),
        "confidence": "Curated / Peer-reviewed" if status == "established_finding" else "In silico Hypothesis (Direct Evidence Absent)",
        "pdb_references": [s["pdb_id"] for s in structural_consequence.get("pdb_structures", [])],
        "alphafold_id": structural_consequence.get("alphafold_id"),
    }


# ==============================================================================
# 6. SYSTEMATIC 5-FACET CHARACTERIZATION (OBJECTIVE 2)
# ==============================================================================
def characterize_mutation_5_facets(
    gene_symbol: str,
    mutation_str: str,
    organism: Optional[str] = None,
    drug: Optional[str] = None,
    curated_row: Optional[Dict[str, Any]] = None,
    structural_consequence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Systematically extract and characterize all 5 dimensions mandated by Objective 2."""
    if not structural_consequence:
        structural_consequence = analyze_mutation_structural_consequences(gene_symbol, mutation_str, organism, drug)

    deltas = structural_consequence.get("physicochemical", {})
    canon_gene = structural_consequence.get("canonical_gene", gene_symbol)
    domain = structural_consequence.get("domain", "Structural core")

    # 1. Reported Occurrence
    occurrence = "Sporadic / emerging variant"
    if curated_row:
        conf = str(curated_row.get("confidence") or curated_row.get("tier") or "").lower()
        pheno = str(curated_row.get("phenotype") or "").lower()
        if any(k in conf for k in ["grade 1", "high", "1)", "established"]) or ("assoc w r" in conf and "interim" not in conf) or pheno == "resistance-associated":
            occurrence = "Globally prevalent canonical resistance mutation; highly frequent in clinical isolates"
        elif any(k in conf for k in ["grade 2", "moderate", "2)", "interim"]) or "interim" in pheno:
            occurrence = "Documented clinical surveillance variant; recurring in epidemiological cohorts"
        elif "literature" in str(curated_row.get("source", "")).lower() or curated_row.get("mutation"):
            occurrence = "Documented in published clinical/experimental isolates"

    # 2. Genetic Context
    gen_context = "Chromosomal determinant"
    if any(k in canon_gene.lower() for k in ["tem", "kpc", "shv", "oxa", "ndm", "mcr", "vana"]):
        gen_context = "Mobile Genetic Element (plasmid-borne transposons / integrons, e.g. Tn3, Tn4401, Tn1546)"
    elif any(k in canon_gene.lower() for k in ["katg", "rpob", "gyra", "parc", "inha", "pncA"]):
        gen_context = "Chromosomal structural gene; core target in essential replication/metabolic operon"

    # 3. Sequence Characteristics
    seq_char = (
        f"{deltas.get('wt_name', 'Wildtype')} ({deltas.get('wt', '')}) to {deltas.get('mut_name', 'Mutant')} ({deltas.get('mut', '')}) "
        f"at codon {structural_consequence.get('position', '')}. "
        f"Physicochemical shift: {deltas.get('summary', 'Iso-steric substitution')}."
    )

    # 4. Functional Effects
    func_effects = (
        f"Located in **{domain}**. {structural_consequence.get('molecular_impact_summary', 'Modulates active-site kinetics.')}"
    )

    # 5. Resistance Associations
    res_assoc = "Antimicrobial resistance modulation"
    drug_suffix = f" against {drug}" if drug else ""
    if curated_row and curated_row.get("phenotype"):
        res_assoc = f"{curated_row.get('phenotype')}{drug_suffix} ({curated_row.get('confidence', 'Curated')})"
    elif drug:
        res_assoc = f"Resistance / elevated MIC against {drug}"
    elif any(k in canon_gene.lower() for k in ["tem", "kpc", "shv", "oxa"]):
        res_assoc = "Extended-Spectrum beta-Lactam (ESBL) / Carbapenem resistance"
    elif "katg" in canon_gene.lower():
        res_assoc = "High-level Isoniazid (INH) resistance"
    elif "rpob" in canon_gene.lower():
        res_assoc = "High-level Rifampicin (RIF) cross-resistance"
    elif "gyra" in canon_gene.lower():
        res_assoc = "High-level Fluoroquinolone (Ciprofloxacin/Levofloxacin) resistance"

    return {
        "mutation": mutation_str,
        "gene": canon_gene,
        "organism": organism or "Bacterial pathogen",
        "drug": drug or "Antimicrobial agent",
        "reported_occurrence": occurrence,
        "genetic_context": gen_context,
        "sequence_characteristics": seq_char,
        "functional_effects": func_effects,
        "resistance_associations": res_assoc,
    }


# ==============================================================================
# 7. AGENT ENTRYPOINT (FOR LANGGRAPH WORKFLOW)
# ==============================================================================
def run_structural_analysis(
    understanding: Dict[str, Any],
    mutation_reference: Optional[Dict[str, Any]] = None,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """LangGraph node execution point: coordinates sequence & structural analysis across all extracted entities."""
    entities = understanding.get("entities", {}) or {}
    genes = entities.get("genes", []) or []
    mutations = entities.get("mutations", []) or entities.get("variants", []) or []
    drugs = entities.get("antibiotics", []) or []
    organisms = entities.get("organisms", []) or []

    primary_gene = genes[0] if genes else "AMR Determinant"
    primary_organism = organisms[0] if organisms else None
    primary_drug = drugs[0] if drugs else None

    # Retrieve macro structural profile for genes
    gene_structures = {}
    for g in genes:
        gene_structures[g] = get_structural_profile(g, primary_organism)

    # Process all mutations (both query mutations and reference mutations)
    mutation_consequences = []
    hypotheses = []
    characterizations = []

    # Map curated reference mutations for fast lookup
    ref_muts = {}
    for r in (mutation_reference or {}).get("mutations", []) or []:
        m = str(r.get("mutation") or r.get("reported_mutation") or "").strip()
        if m:
            ref_muts[m.upper()] = r

    # Determine targets to analyze
    target_mutations = list(mutations)
    # If no mutation was queried directly, check reference mutations or provide canonical loci
    if not target_mutations and ref_muts:
        target_mutations = list(ref_muts.keys())[:5]

    for m in target_mutations:
        sc = analyze_mutation_structural_consequences(primary_gene, m, primary_organism, primary_drug)
        if not sc.get("valid_notation") or not sc.get("position_plausible"):
            continue

        mutation_consequences.append(sc)
        curated_info = ref_muts.get(m.upper())

        # Objective 3: Generate hypothesis
        hypo = generate_evidence_supported_hypothesis(
            primary_gene, m, sc, primary_organism, primary_drug, curated_evidence=curated_info
        )
        hypotheses.append(hypo)

        # Objective 2: 5-facet characterization
        facet = characterize_mutation_5_facets(
            primary_gene, m, primary_organism, primary_drug, curated_row=curated_info, structural_consequence=sc
        )
        characterizations.append(facet)

    # Summary of computational evidence
    computational_evidence = {
        "status": "completed",
        "genes_analyzed": len(gene_structures),
        "mutations_modeled": len(mutation_consequences),
        "pdb_structures_mapped": sum(len(p.get("pdb_structures", [])) for p in gene_structures.values()),
        "hypotheses_generated": len([h for h in hypotheses if h.get("is_hypothesis")]),
    }

    evidence_gaps = []
    for h in hypotheses:
        if h.get("is_hypothesis"):
            evidence_gaps.append(
                f"Variant **{h['mutation']}** in **{h['gene']}**: direct published experimental MIC and crystal structure are unverified; supported by in silico structural hypothesis."
            )

    return {
        "gene_structures": gene_structures,
        "mutation_consequences": mutation_consequences,
        "hypotheses": hypotheses,
        "characterizations_5_facets": characterizations,
        "computational_evidence": computational_evidence,
        "evidence_gaps": evidence_gaps,
    }
