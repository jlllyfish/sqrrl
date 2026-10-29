"""Nettoyage des identifiants pour Grist (tables et colonnes)."""
import unicodedata

# Identifiants réservés par Grist (comparés en minuscules)
RESERVED_COL_IDS = {"id", "manualsort"}


def _ascii_id(value) -> str:
    s = unicodedata.normalize("NFD", str(value))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.strip().replace(" ", "_").replace("-", "_")
    return "".join(c for c in s if (c.isascii() and c.isalnum()) or c == "_")


def sanitize_col(col) -> str:
    col_id = _ascii_id(col)
    if col_id and col_id[0].isdigit():
        col_id = "col_" + col_id
    return col_id or "col_vide"


def unique_col_ids(cols) -> list[str]:
    """Nettoie une liste d'en-têtes en garantissant des IDs uniques (insensible à la casse)."""
    used = set(RESERVED_COL_IDS)
    out = []
    for c in cols:
        base = sanitize_col(c)
        cid, i = base, 2
        while cid.lower() in used:
            cid = f"{base}_{i}"
            i += 1
        used.add(cid.lower())
        out.append(cid)
    return out


def sanitize_table_name(name) -> str:
    """Nom de table Grist valide. Grist impose une majuscule initiale : on l'applique d'emblée."""
    n = _ascii_id(name)
    if n and n[0].isdigit():
        n = "T_" + n
    n = n[:50] or "Import"
    return n[0].upper() + n[1:]


def unique_table_names(labels) -> dict[str, str]:
    """Associe chaque label à un nom de table, sans collision entre labels."""
    used, out = set(), {}
    for label in labels:
        base = sanitize_table_name(label)
        name, i = base, 2
        while name.lower() in used:
            name = f"{base}_{i}"
            i += 1
        used.add(name.lower())
        out[label] = name
    return out
