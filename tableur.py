"""
Lecture robuste des tableurs déposés par les usagers.

Cas gérés :
- en-têtes écrits différemment d'un fichier à l'autre (retours à la ligne, espaces insécables,
  « envoi\\nFR + 10 chiffres » vs « envoiFR + 10 chiffres ») → rapprochés par une clé canonique ;
- colonnes répétées côte à côte (l'usager recopie le bloc d'en-têtes au lieu d'ajouter des lignes)
  → les blocs sont empilés les uns sous les autres ;
- colonnes et lignes vides, lignes vides en tête de feuille ;
- nombres entiers devenus flottants à cause des cellules vides (64120051.0 → 64120051).
"""
import io
import re
import unicodedata

import pandas as pd
from pandas.api import types as pt

from utils import RESERVED_COL_IDS, sanitize_col

MAX_ID_LEN = 40


def clean_header(h) -> str:
    """Normalise les espaces (retours à la ligne, insécables…) d'un en-tête."""
    if h is None or (isinstance(h, float) and pd.isna(h)):
        return ""
    return re.sub(r"\s+", " ", str(h).replace(" ", " ")).strip()


def canon(h) -> str:
    """Clé de rapprochement : minuscules, sans accents ni ponctuation ni espaces."""
    s = unicodedata.normalize("NFD", clean_header(h))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return "".join(c for c in s.lower() if c.isascii() and c.isalnum())


def short_name(raw_header: str) -> str:
    """Nom court pour l'ID : 1re ligne de l'en-tête, sans la partie entre parenthèses."""
    first = str(raw_header).split("\n")[0].split("(")[0]
    return clean_header(first)


def _compact(v):
    return re.sub(r"\s+", "", v) if isinstance(v, str) else v


def _strip(v):
    return re.sub(r"[  ]+", " ", v).strip() if isinstance(v, str) else v


def read_table(content: bytes, filename: str, compact_text: bool = False,
               require_first_col: bool = True) -> pd.DataFrame:
    """
    Lit la première feuille et retourne un DataFrame aux en-têtes nettoyés, blocs répétés empilés.
    `require_first_col` : ignore les lignes dont la 1re colonne est vide (totaux, notes…).
    """
    ext = filename.rsplit(".", 1)[-1].lower()
    raw = pd.read_excel(io.BytesIO(content), header=None, engine="odf" if ext == "ods" else None)
    raw = raw.dropna(how="all").dropna(axis=1, how="all")
    if raw.empty:
        return pd.DataFrame()

    raw_headers = ["" if pd.isna(v) else str(v) for v in raw.iloc[0]]
    headers = [clean_header(h) or f"Colonne {i + 1}" for i, h in enumerate(raw_headers)]
    body = raw.iloc[1:].reset_index(drop=True)
    body.columns = range(len(headers))

    # Occurrence de chaque en-tête : 0 pour la 1re fois, 1 pour la 2e…
    seen, occ = {}, []
    for h in headers:
        k = canon(h)
        occ.append(seen.get(k, 0))
        seen[k] = seen.get(k, 0) + 1

    first_header, short = {}, {}  # clé canonique → libellé / nom court de la 1re occurrence
    for h, rh in zip(headers, raw_headers):
        first_header.setdefault(canon(h), h)
        short.setdefault(h, short_name(rh) or h)

    blocks = []
    for b in range(max(occ) + 1):
        idx = [i for i, o in enumerate(occ) if o == b]
        part = body[idx].copy()
        part.columns = [first_header[canon(headers[i])] for i in idx]
        blocks.append(part)
    df = pd.concat(blocks, ignore_index=True) if len(blocks) > 1 else blocks[0]
    df = df.dropna(how="all")
    if require_first_col and len(df.columns):
        df = df[df.iloc[:, 0].notna()]
    df = df.reset_index(drop=True)

    # Nettoyage des textes puis typage
    df = df.apply(lambda s: s.map(_compact if compact_text else _strip))
    df = df.replace("", None).infer_objects()
    for col in df.columns:
        s = df[col]
        # Texte entièrement numérique → nombre, sauf codes à zéro initial (ex. EDE « 01234567 »)
        if pt.is_object_dtype(s) and not s.map(lambda v: isinstance(v, str) and bool(re.match(r"^0\d", v))).any():
            num = pd.to_numeric(s, errors="coerce")
            if num.notna().sum() == s.notna().sum() and s.notna().any():
                s = num
        if pt.is_float_dtype(s):
            non_null = s.dropna()
            if not non_null.empty and (non_null == non_null.round()).all() and non_null.abs().max() < 2**53:
                s = s.astype("Int64")
        df[col] = s
    df.attrs["short_names"] = short
    return df


class ColumnRegistry:
    """
    Attribue un ID de colonne Grist stable à chaque en-tête, par table.
    Deux en-têtes de même clé canonique reçoivent le même ID, quel que soit le fichier.
    """

    def __init__(self):
        self.by_key: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self.used = set(RESERVED_COL_IDS)

    def seed(self, grist_columns: list[dict]):
        """Pré-remplit avec les colonnes existantes d'une table Grist."""
        for c in grist_columns:
            cid = c["id"]
            label = (c.get("fields") or {}).get("label") or cid
            self.by_key.setdefault(canon(label), cid)
            self.by_key.setdefault(canon(cid), cid)
            self.labels.setdefault(cid, label)
            self.used.add(cid.lower())

    def id_for(self, header: str, short: str | None = None) -> str:
        key = canon(header) or "colonne"
        if key in self.by_key:
            return self.by_key[key]
        base = re.sub(r"_+", "_", sanitize_col(short or header))
        if len(base) > MAX_ID_LEN:
            cut = base[:MAX_ID_LEN]
            base = cut.rsplit("_", 1)[0] if "_" in cut[10:] else cut
        base = base.strip("_") or "col"
        cid, i = base, 2
        while cid.lower() in self.used:
            cid = f"{base}_{i}"
            i += 1
        self.used.add(cid.lower())
        self.by_key[key] = cid
        self.labels[cid] = clean_header(header)
        return cid

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        """Renomme les colonnes du DataFrame avec leurs IDs (fusionne les éventuels doublons)."""
        short = df.attrs.get("short_names", {})
        ids = [self.id_for(c, short.get(c)) for c in df.columns]
        out = pd.DataFrame(index=df.index)
        for cid, col in zip(ids, df.columns):
            out[cid] = df[col] if cid not in out else out[cid].combine_first(df[col])
        return out
