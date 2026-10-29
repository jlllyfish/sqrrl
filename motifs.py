"""
Extraction par motifs : les colonnes utiles sont reconnues à leur contenu, pas à leur en-tête.

Pensé pour les PJ remplies sans respecter le modèle (en-têtes absents ou renommés, lignes de titre,
blocs recopiés côte à côte, colonnes d'aide ajoutées…). Chaque colonne cible a un type ; on cherche
dans le fichier la colonne dont les valeurs correspondent le mieux à ce type.

La 1re colonne cible est la clé : une ligne n'est gardée que si sa valeur clé est reconnue
(les en-têtes, titres, totaux et lignes vides disparaissent d'eux-mêmes).
"""
import datetime as dt
import re

import pandas as pd

MIN_RATIO = 0.5  # part minimale de valeurs reconnues pour retenir une colonne

_BOVIN_RE = re.compile(r"^([A-Z]{2})?(\d{10,12})$")
_EDE_RE = re.compile(r"^(?:EDE)?(\d{7,8})$")
_DATE_RE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$")


def _compact(v) -> str | None:
    """Valeur en texte sans espaces ni points, en majuscules ; None si vide."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = re.sub(r"[\s. ]+", "", str(v)).upper()
    return s or None


def _bovin(v):
    s = _compact(v)
    m = _BOVIN_RE.match(s) if s else None
    if not m:
        return None
    return (m.group(1) or "FR") + m.group(2)


def _ede(v):
    s = _compact(v)
    m = _EDE_RE.match(s) if s else None
    return m.group(1).zfill(8) if m else None


def _date(v):
    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)) and not pd.isna(v):
        return pd.Timestamp(v).normalize()
    if isinstance(v, str):
        m = _DATE_RE.match(v.strip())
        if m:
            d, mo, y = (int(x) for x in m.groups())
            y = y + 2000 if y < 100 else y
            try:
                return pd.Timestamp(year=y, month=mo, day=d)
            except ValueError:
                return None
    return None


# type → (libellé, fonction de normalisation : valeur → valeur propre ou None si non reconnue)
TYPES = {
    "bovin": ("N° de bovin (FR + 10 chiffres)", _bovin),
    "ede": ("N° EDE (8 chiffres, préfixe « EDE » toléré)", _ede),
    "date": ("Date", _date),
}

# Modèles proposés dans l'interface : liste de colonnes cibles, la 1re est la clé
PRESETS = {
    "bovins": {
        "label": "Liste de bovins (n° de bovin + EDE de provenance)",
        "targets": [
            {"name": "Numéro de bovin", "type": "bovin"},
            {"name": "EDE de provenance", "type": "ede"},
        ],
    },
}


def _is_label(v) -> bool:
    """Texte sans chiffre (« Numéro des bovins », « TOTAL », « LISTE DES ANIMAUX ») : en-tête ou titre."""
    return isinstance(v, str) and not re.search(r"\d", v)


def _ratio(series: pd.Series, fn) -> float:
    """Part des valeurs reconnues, en ignorant cellules vides et libellés (en-têtes, titres, totaux)."""
    values = [v for v in series
              if not (v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "" or _is_label(v))]
    if not values:
        return 0.0
    return sum(fn(v) is not None for v in values) / len(values)


def extract(raw: pd.DataFrame, targets: list[dict]) -> tuple[pd.DataFrame, dict]:
    """
    Extrait les colonnes cibles d'une feuille lue sans en-tête.
    Retourne (DataFrame aux colonnes cibles, diagnostic {cible: [colonnes sources]}).
    """
    names = [t["name"] for t in targets]
    raw = raw.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if raw.empty or not targets:
        return pd.DataFrame(columns=names), {}
    cols = list(raw.columns)  # positions d'origine dans la feuille (pour le diagnostic A, B, C…)
    fns = [TYPES[t["type"]][1] for t in targets]
    ratios = {(i, p): _ratio(raw[cols[p]], fn) for i, fn in enumerate(fns) for p in range(len(cols))}

    # Colonnes clés : une par bloc (blocs recopiés côte à côte → plusieurs colonnes clés)
    keys = [p for p in range(len(cols)) if ratios[(0, p)] >= MIN_RATIO]
    if not keys:
        return pd.DataFrame(columns=names), {n: [] for n in names}

    diag = {n: [] for n in names}
    parts = []
    for b, key in enumerate(keys):
        end = keys[b + 1] if b + 1 < len(keys) else len(cols)
        # Autres cibles cherchées dans le bloc (à droite de la clé), et à gauche s'il n'y a qu'un bloc
        zone = list(range(key + 1, end)) + (list(range(0, key)) if len(keys) == 1 else [])
        chosen = {0: key}
        for i in range(1, len(targets)):
            free = [p for p in zone if p not in chosen.values()]
            best = max(free, key=lambda p, i=i: (ratios[(i, p)], -p), default=None)
            if best is not None and ratios[(i, best)] >= MIN_RATIO:
                chosen[i] = best

        rows = []
        for r in raw.itertuples(index=False, name=None):
            k = fns[0](r[key])
            if k is None:
                continue  # en-tête, titre, total, ligne vide…
            rows.append([k] + [fns[i](r[chosen[i]]) if i in chosen else None for i in range(1, len(targets))])
        parts.append(pd.DataFrame(rows, columns=names))
        for i, p in chosen.items():
            diag[names[i]].append(_col_letter(cols[p]))

    df = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
    for t in targets:
        if t["type"] == "date":
            df[t["name"]] = pd.to_datetime(df[t["name"]])
    return df, diag


def _col_letter(i: int) -> str:
    """0 → A, 25 → Z, 26 → AA (repère de colonne relatif aux colonnes non vides)."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s
