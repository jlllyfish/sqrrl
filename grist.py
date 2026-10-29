import datetime as dt
import json
import math
import os

import pandas as pd
import requests
from dotenv import load_dotenv
from pandas.api import types as pt

load_dotenv()

GRIST_BASE_URL = os.getenv("GRIST_BASE_URL", "https://grist.numerique.gouv.fr").rstrip("/").removesuffix("/api")
GRIST_TZ = os.getenv("GRIST_TZ", "Europe/Paris")
BATCH_SIZE = 500
TIMEOUT = 60


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _url(doc_id: str, *parts: str) -> str:
    return f"{GRIST_BASE_URL}/api/docs/{doc_id}/" + "/".join(parts)


def _chunks(items: list, size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ── Document ──────────────────────────────────────────────────────────────────

def get_doc(doc_id: str, api_key: str) -> dict:
    """Nom du document et liste de ses tables (vérifie au passage token et ID)."""
    r = requests.get(f"{GRIST_BASE_URL}/api/docs/{doc_id}", headers=_headers(api_key), timeout=TIMEOUT)
    if r.status_code in (401, 403):
        raise PermissionError("Token Grist refusé ou sans accès à ce document")
    if r.status_code == 404:
        raise LookupError("Document introuvable : vérifiez l'ID")
    r.raise_for_status()
    return {"name": r.json().get("name", ""), "tables": list_tables(doc_id, api_key)}


# ── Tables ────────────────────────────────────────────────────────────────────

def list_tables(doc_id: str, api_key: str) -> list[str]:
    r = requests.get(_url(doc_id, "tables"), headers=_headers(api_key), timeout=TIMEOUT)
    r.raise_for_status()
    return [t["id"] for t in r.json().get("tables", [])]


def find_table(table_name: str, existing: list[str]) -> str | None:
    """Retrouve l'ID réel d'une table, sans tenir compte de la casse."""
    for t in existing:
        if t.lower() == table_name.lower():
            return t
    return None


def create_table(doc_id: str, table_name: str, columns: list[dict], api_key: str) -> str:
    """Crée une table et retourne son ID réel (Grist peut le modifier)."""
    payload = {"tables": [{"id": table_name, "columns": columns}]}
    r = requests.post(_url(doc_id, "tables"), json=payload, headers=_headers(api_key), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["tables"][0]["id"]


# ── Colonnes ──────────────────────────────────────────────────────────────────

def list_columns(doc_id: str, table_id: str, api_key: str) -> list[dict]:
    r = requests.get(_url(doc_id, "tables", table_id, "columns"), headers=_headers(api_key), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("columns", [])


def ensure_columns(doc_id: str, table_id: str, columns: list[dict], api_key: str) -> dict[str, str]:
    """
    Vérifie que les colonnes existent dans la table, crée les manquantes.
    Retourne {id voulu: id réel Grist}.
    """
    existing = list_columns(doc_id, table_id, api_key)
    by_id = {c["id"]: c["id"] for c in existing}
    by_label = {(c.get("fields") or {}).get("label"): c["id"] for c in existing}

    mapping, missing = {}, []
    for col in columns:
        if col["id"] in by_id:
            mapping[col["id"]] = col["id"]
        elif col["fields"]["label"] in by_label:
            mapping[col["id"]] = by_label[col["fields"]["label"]]
        else:
            missing.append(col)

    if missing:
        r = requests.post(
            _url(doc_id, "tables", table_id, "columns"),
            json={"columns": missing}, headers=_headers(api_key), timeout=TIMEOUT,
        )
        r.raise_for_status()
        for col, created in zip(missing, r.json().get("columns", []), strict=False):
            mapping[col["id"]] = created["id"]
    return mapping


# ── Lignes ────────────────────────────────────────────────────────────────────

def check_existing_dossier(doc_id: str, table_id: str, dossier_number: int, api_key: str) -> list[int]:
    """Retourne les row IDs déjà présents pour ce numéro de dossier."""
    params = {"filter": json.dumps({"dossier_number": [dossier_number]})}
    r = requests.get(_url(doc_id, "tables", table_id, "records"), headers=_headers(api_key),
                     params=params, timeout=TIMEOUT)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return [rec["id"] for rec in r.json().get("records", [])]


def delete_rows(doc_id: str, table_id: str, row_ids: list[int], api_key: str) -> None:
    for chunk in _chunks(row_ids):
        r = requests.post(_url(doc_id, "tables", table_id, "data", "delete"), json=chunk,
                          headers=_headers(api_key), timeout=TIMEOUT)
        r.raise_for_status()


def add_rows(doc_id: str, table_id: str, records: list[dict], api_key: str) -> None:
    """Insère des lignes par lots."""
    for chunk in _chunks(records):
        payload = {"records": [{"fields": row} for row in chunk]}
        r = requests.post(_url(doc_id, "tables", table_id, "records"), json=payload,
                          headers=_headers(api_key), timeout=TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"Grist {r.status_code} : {r.text[:300]}")


# ── Types et valeurs ──────────────────────────────────────────────────────────

def infer_grist_type(series: pd.Series) -> str:
    if pt.is_bool_dtype(series):
        return "Bool"
    if pt.is_integer_dtype(series):
        return "Int"
    if pt.is_float_dtype(series):
        return "Numeric"
    if pt.is_datetime64_any_dtype(series):
        non_null = series.dropna()
        if non_null.empty or (non_null.dt.normalize() == non_null).all():
            return "Date"
        return f"DateTime:{GRIST_TZ}"
    return "Text"


def to_cell(v, grist_type: str = "Text"):
    """Convertit une valeur pandas en valeur acceptée par l'API Grist."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)):
        ts = pd.Timestamp(v)
        if grist_type == "Date":
            # Grist : secondes depuis l'epoch, minuit UTC
            ts = ts.tz_convert(None) if ts.tz is not None else ts
            return int(ts.normalize().tz_localize("UTC").timestamp())
        if grist_type.startswith("DateTime"):
            if ts.tz is None:
                ts = ts.tz_localize(GRIST_TZ, ambiguous=False, nonexistent="shift_forward")
            return ts.timestamp()
        return ts.isoformat()

    if hasattr(v, "item"):  # types numpy
        v = v.item()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "isoformat"):  # datetime.time…
        return v.isoformat()
    if grist_type == "Text" and not isinstance(v, str):
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return str(v)
    return v


def build_columns(df: pd.DataFrame, labels: dict[str, str]) -> list[dict]:
    """Colonnes Grist depuis un DataFrame dont les colonnes sont déjà des IDs nettoyés."""
    return [
        {"id": col, "fields": {"label": labels.get(col, col), "type": infer_grist_type(df[col])}}
        for col in df.columns
    ]


def df_to_records(df: pd.DataFrame, colmap: dict[str, str] | None = None,
                  all_cols=()) -> list[dict]:
    """
    Lignes prêtes pour Grist. Les colonnes de la table absentes du fichier (`all_cols`) sont
    envoyées vides : sans cela, Grist y met la valeur par défaut (0 pour une colonne numérique).
    """
    types = {c: infer_grist_type(df[c]) for c in df.columns}
    keys = [(colmap or {}).get(c, c) for c in df.columns]
    empty = {c: None for c in all_cols if c not in keys and c not in ("id", "manualSort")}
    return [
        {**empty, **{k: to_cell(v, types[c]) for k, c, v in zip(keys, df.columns, row, strict=True)}}
        for row in df.itertuples(index=False, name=None)
    ]
