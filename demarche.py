import os

import requests
from dotenv import load_dotenv

load_dotenv()

DS_API_URL = os.getenv("DS_API_URL", "https://demarche.numerique.gouv.fr/api/v2/graphql")
TIMEOUT = 60
# Formats tableur reconnus : par extension, ou à défaut par type MIME
SPREADSHEET_EXT = {"xlsx": "xlsx", "xlsm": "xlsx", "xls": "xls", "ods": "ods", "csv": "csv"}
SPREADSHEET_MIME = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "text/csv": "csv",
}

DOSSIER_STATE_LABELS = {
    "en_construction": "En construction",
    "en_instruction": "En instruction",
    "accepte": "Accepté",
    "refuse": "Refusé",
    "sans_suite": "Sans suite",
}

QUERY_META = """
query GetDemarcheMeta($demarcheNumber: Int!) {
  demarche(number: $demarcheNumber) {
    number
    title
    groupeInstructeurs { number label }
  }
}
"""

# Champs PJ, y compris ceux situés dans des blocs répétables
_DOSSIER_FIELDS = """
fragment PJ on PieceJustificativeChamp {
  label
  files { filename url contentType }
}
fragment DossierPJ on Dossier {
  number
  champs {
    __typename
    ...PJ
    ... on RepetitionChamp { label rows { champs { __typename ...PJ } } }
  }
  annotations {
    __typename
    ...PJ
    ... on RepetitionChamp { label rows { champs { __typename ...PJ } } }
  }
}
"""

def _dossiers_query(root: str, with_order: bool = False, light: bool = False) -> str:
    """
    Query de liste de dossiers, via la démarche ou un groupe instructeur.
    `light` : numéros seulement (comptage rapide, sans les champs).
    """
    if root == "demarche":
        var, field = "$demarcheNumber: Int!", "demarche(number: $demarcheNumber)"
    else:
        var, field = "$groupeNumber: Int!", "groupeInstructeur(number: $groupeNumber)"
    order_var = ", $order: Order" if with_order else ""
    order_arg = ", order: $order" if with_order else ""
    return f"""
query Q({var}, $state: DossierState, $after: String{order_var}) {{
  {field} {{
    dossiers(first: 100, after: $after, state: $state{order_arg}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ {"number" if light else "...DossierPJ"} }}
    }}
  }}
}}
""" + ("" if light else _DOSSIER_FIELDS)


QUERY_DEMARCHE = _dossiers_query("demarche")
QUERY_GROUPE = _dossiers_query("groupe")

# Schéma de la démarche : tous les champs PJ, même absents des premiers dossiers
QUERY_SCHEMA = """
query GetSchema($demarcheNumber: Int!) {
  demarche(number: $demarcheNumber) {
    activeRevision {
      champDescriptors { ...Desc }
      annotationDescriptors { ...Desc }
    }
  }
}
fragment Desc on ChampDescriptor {
  __typename
  label
  ... on RepetitionChampDescriptor { champDescriptors { __typename label } }
}
"""


def _gql(token: str, query: str, variables: dict) -> dict:
    """
    Exécute une requête GraphQL. Les erreurs partielles (un dossier ou un fichier illisible,
    par ex. un fichier en cours d'analyse antivirus) n'empêchent pas d'exploiter le reste.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(DS_API_URL, json={"query": query, "variables": variables},
                         headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        if not data.get("data"):
            raise ValueError(f"Erreur GraphQL : {data['errors']}")
        print(f"[DS] {len(data['errors'])} erreur(s) partielle(s) : {data['errors'][0].get('message')}")
    return data["data"]


def file_kind(f: dict) -> str | None:
    """Type de tableur d'un fichier DS (xlsx, xls, ods, csv), ou None."""
    name = (f.get("filename") or "").strip().lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in SPREADSHEET_EXT:
        return SPREADSHEET_EXT[ext]
    return SPREADSHEET_MIME.get((f.get("contentType") or "").split(";")[0].strip().lower())


def file_ext(f: dict) -> str:
    """Extension affichable (pour le diagnostic)."""
    name = (f.get("filename") or "").strip().lower()
    return name.rsplit(".", 1)[-1] if "." in name else (f.get("contentType") or "?")


def _spreadsheets(champ: dict) -> list[dict]:
    out = []
    for f in champ.get("files") or []:
        if f and f.get("url"):
            kind = file_kind(f)
            if kind:
                out.append({**f, "kind": kind})
    return out


def _all_pj(dossier: dict) -> dict[str, list[dict]]:
    """{label: [tous les fichiers]} pour chaque champ PJ du dossier (y compris blocs répétables)."""
    result: dict[str, list[dict]] = {}
    for champ in (dossier.get("champs") or []) + (dossier.get("annotations") or []):
        if not champ:
            continue
        if champ.get("__typename") == "RepetitionChamp":
            parent = champ.get("label", "")
            for row in champ.get("rows") or []:
                for sub in (row or {}).get("champs") or []:
                    if sub and sub.get("__typename") == "PieceJustificativeChamp":
                        result.setdefault(f"{parent} - {sub.get('label', '')}", []).extend(
                            f for f in sub.get("files") or [] if f)
        elif champ.get("__typename") == "PieceJustificativeChamp":
            result.setdefault(champ.get("label", ""), []).extend(f for f in champ.get("files") or [] if f)
    return result


def _extract_tableur_fields(dossier: dict) -> dict[str, list[dict]]:
    """{label: [fichiers tableur]} — les champs de même label sont fusionnés, pas écrasés."""
    result = {}
    for label, files in _all_pj(dossier).items():
        sheets = _spreadsheets({"files": files})
        if sheets:
            result[label] = sheets
    return result


def get_demarche_meta(demarche_number: int, token: str) -> dict:
    data = _gql(token, QUERY_META, {"demarcheNumber": demarche_number})
    demarche = data.get("demarche") or {}
    return {
        "title": demarche.get("title", ""),
        "groupes": [{"number": g["number"], "label": g["label"]}
                    for g in (demarche.get("groupeInstructeurs") or [])],
    }


def _iter_nodes(token: str, query: str, variables: dict, root_key: str):
    after = None
    while True:
        data = _gql(token, query, {**variables, "after": after})
        dossiers = (data.get(root_key) or {}).get("dossiers") or {}
        yield from dossiers.get("nodes") or []
        page_info = dossiers.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info["endCursor"]


def get_schema_pj_labels(demarche_number: int, token: str) -> list[str]:
    """Labels des champs PJ déclarés dans la révision active (vide si le schéma est inaccessible)."""
    try:
        data = _gql(token, QUERY_SCHEMA, {"demarcheNumber": demarche_number})
    except (requests.RequestException, ValueError):
        return []
    rev = ((data.get("demarche") or {}).get("activeRevision")) or {}
    labels = []
    for d in (rev.get("champDescriptors") or []) + (rev.get("annotationDescriptors") or []):
        if d.get("__typename") == "PieceJustificativeChampDescriptor":
            labels.append(d.get("label", ""))
        elif d.get("__typename") == "RepetitionChampDescriptor":
            for sub in d.get("champDescriptors") or []:
                if sub.get("__typename") == "PieceJustificativeChampDescriptor":
                    labels.append(f"{d.get('label', '')} - {sub.get('label', '')}")
    return list(dict.fromkeys(labels))


def _iter_all(demarche_number, token, states=None, groupe_numbers=None, newest_first=False, light=False):
    """
    Parcourt les dossiers filtrés. L'API DS n'accepte qu'un statut par requête :
    on boucle sur chaque statut (et chaque groupe), en dédoublonnant.
    """
    states = states or [None]
    extra = {"order": "DESC"} if newest_first else {}
    q_dem = _dossiers_query("demarche", newest_first, light)
    q_grp = _dossiers_query("groupe", newest_first, light)
    seen = set()
    sources = (
        [(q_grp, {"groupeNumber": g, "state": s, **extra}, "groupeInstructeur")
         for g in groupe_numbers for s in states]
        if groupe_numbers else
        [(q_dem, {"demarcheNumber": demarche_number, "state": s, **extra}, "demarche")
         for s in states]
    )
    for query, vars_, root in sources:
        for node in _iter_nodes(token, query, vars_, root):
            if not node or node["number"] in seen:
                continue
            seen.add(node["number"])
            yield node


def scan_fields(demarche_number, token, states=None, groupe_numbers=None,
                min_dossiers=100, max_dossiers=500):
    """
    Liste les champs PJ (schéma de la démarche + dossiers les plus récents), en signalant sa progression.
    Analyse au moins `min_dossiers` dossiers, et continue jusqu'à `max_dossiers` tant qu'un champ
    du schéma n'a pas d'exemple de tableur.
    yield ("progress", message)… puis ("result", (titre, {label: {...}}, nb analysés)).
    """
    yield ("progress", "Lecture de la démarche et de ses champs pièce justificative…")
    title = get_demarche_meta(demarche_number, token)["title"]
    schema_labels = get_schema_pj_labels(demarche_number, token)
    yield ("progress", f"{len(schema_labels)} champ(s) pièce justificative déclaré(s) dans la démarche")

    def new():
        return {"files": [], "seen": 0, "pj_seen": 0, "exts": {}}

    def found():
        return sum(1 for e in fields.values() if e["files"])

    fields = {label: new() for label in schema_labels}
    yield ("progress", "Recherche de tableurs dans les dossiers les plus récents…")

    scanned = 0
    for node in _iter_all(demarche_number, token, states, groupe_numbers, newest_first=True):
        scanned += 1
        for label, files in _all_pj(node).items():
            entry = fields.setdefault(label, new())
            if files:
                entry["pj_seen"] += 1
            for f in files:
                ext = file_ext(f)
                entry["exts"][ext] = entry["exts"].get(ext, 0) + 1
            sheets = _spreadsheets({"files": files})
            if sheets:
                entry["seen"] += 1
                if not entry["files"]:
                    entry["files"] = sheets
        if scanned % 100 == 0:
            yield ("progress", f"{scanned} dossiers analysés · tableurs trouvés pour {found()} champ(s)")
        if scanned >= max_dossiers:
            break
        if scanned >= min_dossiers and all(fields[lb]["files"] for lb in schema_labels):
            break
    yield ("progress", f"{scanned} dossiers analysés · tableurs trouvés pour {found()} champ(s)")
    yield ("result", (title, fields, scanned))


def count_dossiers(demarche_number, token, states=None, groupe_numbers=None) -> int:
    """Nombre de dossiers correspondant aux filtres (l'API ne fournit pas de total : requête légère)."""
    return sum(1 for _ in _iter_all(demarche_number, token, states, groupe_numbers, light=True))


def iter_dossiers(demarche_number, token, states=None, groupe_numbers=None):
    """yield ("meta", {...}), ("count", {...}), puis ("dossier", {...})… puis ("done", {...})."""
    yield ("meta", {"title": get_demarche_meta(demarche_number, token)["title"]})
    yield ("count", {"total": count_dossiers(demarche_number, token, states, groupe_numbers)})
    total = 0
    for node in _iter_all(demarche_number, token, states, groupe_numbers):
        total += 1
        yield ("dossier", {"number": node["number"], "fields": _extract_tableur_fields(node)})
    yield ("done", {"total": total})


def download_file(url: str, token: str) -> bytes:
    # Les URLs de fichiers DS sont pré-signées : ne pas y envoyer le token
    if "temp_url_sig" in url or "static.demarches" in url:
        response = requests.get(url, timeout=TIMEOUT)
    else:
        response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
    response.raise_for_status()
    return response.content
