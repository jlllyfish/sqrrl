import os

import requests
from dotenv import load_dotenv

load_dotenv()

DS_API_URL = os.getenv("DS_API_URL", "https://demarche.numerique.gouv.fr/api/v2/graphql")
TIMEOUT = 60
SPREADSHEET_EXT = (".xlsx", ".xls", ".ods")

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

QUERY_DEMARCHE = """
query GetDemarche($demarcheNumber: Int!, $state: DossierState, $after: String) {
  demarche(number: $demarcheNumber) {
    dossiers(first: 100, after: $after, state: $state) {
      pageInfo { hasNextPage endCursor }
      nodes { ...DossierPJ }
    }
  }
}
""" + _DOSSIER_FIELDS

QUERY_GROUPE = """
query GetGroupe($groupeNumber: Int!, $state: DossierState, $after: String) {
  groupeInstructeur(number: $groupeNumber) {
    dossiers(first: 100, after: $after, state: $state) {
      pageInfo { hasNextPage endCursor }
      nodes { ...DossierPJ }
    }
  }
}
""" + _DOSSIER_FIELDS


def _gql(token: str, query: str, variables: dict) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(DS_API_URL, json={"query": query, "variables": variables},
                         headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise ValueError(f"Erreur GraphQL : {data['errors']}")
    return data["data"]


def _spreadsheets(champ: dict) -> list[dict]:
    return [
        f for f in (champ.get("files") or [])
        if (f.get("filename") or "").lower().endswith(SPREADSHEET_EXT)
    ]


def _extract_tableur_fields(dossier: dict) -> dict[str, list[dict]]:
    """{label: [fichiers]} — les champs de même label sont fusionnés, pas écrasés."""
    result: dict[str, list[dict]] = {}

    def add(label, files):
        if files:
            result.setdefault(label, []).extend(files)

    for champ in (dossier.get("champs") or []) + (dossier.get("annotations") or []):
        if not champ:
            continue
        if champ.get("__typename") == "RepetitionChamp":
            parent = champ.get("label", "")
            for row in champ.get("rows") or []:
                for sub in row.get("champs") or []:
                    if sub and sub.get("__typename") == "PieceJustificativeChamp":
                        add(f"{parent} - {sub.get('label', '')}", _spreadsheets(sub))
        elif champ.get("__typename") == "PieceJustificativeChamp":
            add(champ.get("label", ""), _spreadsheets(champ))
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


def _iter_all(demarche_number, token, states=None, groupe_numbers=None):
    """
    Parcourt les dossiers filtrés. L'API DS n'accepte qu'un statut par requête :
    on boucle sur chaque statut (et chaque groupe), en dédoublonnant.
    """
    states = states or [None]
    seen = set()
    sources = (
        [(QUERY_GROUPE, {"groupeNumber": g, "state": s}, "groupeInstructeur")
         for g in groupe_numbers for s in states]
        if groupe_numbers else
        [(QUERY_DEMARCHE, {"demarcheNumber": demarche_number, "state": s}, "demarche")
         for s in states]
    )
    for query, vars_, root in sources:
        for node in _iter_nodes(token, query, vars_, root):
            if node["number"] in seen:
                continue
            seen.add(node["number"])
            yield node


def collect_fields(demarche_number, token, states=None, groupe_numbers=None, max_dossiers=50):
    """
    Analyse jusqu'à `max_dossiers` dossiers et retourne
    (titre, {label: {"files": fichiers d'un exemple, "seen": nb de dossiers}}, nb analysés).
    """
    title = get_demarche_meta(demarche_number, token)["title"]
    fields, scanned = {}, 0
    for node in _iter_all(demarche_number, token, states, groupe_numbers):
        scanned += 1
        for label, files in _extract_tableur_fields(node).items():
            entry = fields.setdefault(label, {"files": files, "seen": 0})
            entry["seen"] += 1
        if scanned >= max_dossiers:
            break
    return title, fields, scanned


def iter_dossiers(demarche_number, token, states=None, groupe_numbers=None):
    """yield ("meta", {...}) puis ("dossier", {...})… puis ("done", {...})."""
    yield ("meta", {"title": get_demarche_meta(demarche_number, token)["title"]})
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
