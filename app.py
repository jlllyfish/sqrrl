import json
import os
import secrets
import threading
import time
import traceback

import pandas as pd
import requests
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)

from demarche import (
    DOSSIER_STATE_LABELS,
    download_file,
    get_demarche_meta,
    iter_dossiers,
    scan_fields,
)
from grist import (
    add_rows,
    build_columns,
    check_existing_dossier,
    create_table,
    delete_rows,
    df_to_records,
    ensure_columns,
    find_table,
    get_doc,
    infer_grist_type,
    list_columns,
    list_tables,
    to_cell,
)
from tableur import ColumnRegistry, read_table
from utils import unique_table_names

app = Flask(__name__)

# Jobs d'import en attente : les tokens restent côté serveur, l'EventSource ne reçoit qu'un ID
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 300  # secondes
DUPLICATE_MODES = ("skip", "update", "append")


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, default=str)}\n\n"


def file_to_df(f: dict, ds_token: str, dossier_number: int,
               registry: ColumnRegistry, options: dict) -> pd.DataFrame:
    """Lit un tableur DS et retourne un DataFrame aux colonnes identifiées par le registre de la table."""
    filename = (f.get("filename") or "").strip()
    df = read_table(download_file(f["url"], ds_token), f["kind"],
                    known_headers=registry.known_headers(), **options)
    df = registry.apply(df)
    df.insert(0, registry.id_for("source_filename"), filename)
    df.insert(0, registry.id_for("dossier_number"), dossier_number)
    return df


def _read_options(body: dict) -> dict:
    return {
        "compact_text": bool(body.get("compact_text")),
        "require_first_col": body.get("require_first_col", True) is not False,
    }


def _parse_states(raw) -> list[str] | None:
    states = [s for s in (raw or []) if s in DOSSIER_STATE_LABELS]
    return states or None


def _parse_groupes(raw) -> list[int] | None:
    return [int(g) for g in (raw or [])] or None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/meta", methods=["POST"])
def get_meta():
    body = request.json or {}
    ds_token, demarche_number = body.get("ds_token"), body.get("demarche_number")
    if not ds_token or not demarche_number:
        return jsonify({"error": "Paramètres manquants"}), 400
    try:
        meta = get_demarche_meta(int(demarche_number), ds_token)
        meta["states"] = [{"value": k, "label": v} for k, v in DOSSIER_STATE_LABELS.items()]
        return jsonify(meta)
    except (requests.RequestException, ValueError) as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/grist-check", methods=["POST"])
def grist_check():
    """Vérifie le token et l'ID du document Grist, retourne le nom du document."""
    body = request.json or {}
    if not body.get("grist_token") or not body.get("doc_id"):
        return jsonify({"error": "Token et ID du document requis"}), 400
    try:
        return jsonify(get_doc(body["doc_id"].strip(), body["grist_token"].strip()))
    except (PermissionError, LookupError) as e:
        return jsonify({"error": str(e)}), 400
    except requests.RequestException as e:
        return jsonify({"error": f"Grist injoignable : {e}"}), 502


def ndjson(data: dict) -> str:
    return json.dumps(data, default=str) + "\n"


@app.route("/api/fields", methods=["POST"])
def get_fields():
    """
    Analyse les dossiers et retourne les champs PJ tableur trouvés, avec aperçu.
    Réponse en flux NDJSON : {"type": "progress", "msg"}… puis {"type": "result", …} ou {"type": "error"}.
    """
    body = request.json or {}
    ds_token, demarche_number = body.get("ds_token"), body.get("demarche_number")
    if not ds_token or not demarche_number:
        return jsonify({"error": "ds_token et demarche_number requis"}), 400
    options = _read_options(body)

    def generate():
        try:
            result = None
            for kind, payload in scan_fields(
                int(demarche_number), ds_token,
                states=_parse_states(body.get("states")),
                groupe_numbers=_parse_groupes(body.get("groupe_numbers")),
            ):
                if kind == "progress":
                    yield ndjson({"type": "progress", "msg": payload})
                else:
                    result = payload
            title, fields, scanned = result
            table_names = unique_table_names(fields.keys())

            fields_info = []
            with_files = [lb for lb, info in fields.items() if info["files"]]
            for label, info in fields.items():
                schema, headers, rows, total_rows = [], [], [], None
                if info["files"]:
                    f = info["files"][0]
                    pos = with_files.index(label) + 1
                    yield ndjson({"type": "progress",
                                  "msg": f"Aperçu {pos}/{len(with_files)} « {label} » : lecture de "
                                         f"{(f.get('filename') or '').strip()}…"})
                    try:
                        reg = ColumnRegistry()
                        df = file_to_df(f, ds_token, 0, reg, options)
                        for col in df.columns:
                            non_null = df[col].dropna()
                            sample = to_cell(non_null.iloc[0]) if not non_null.empty else None
                            schema.append({"id": col, "original": reg.labels.get(col, col),
                                           "type": infer_grist_type(df[col]),
                                           "sample": "" if sample is None else str(sample)})
                        headers = [reg.labels.get(c, c) for c in df.columns]
                        rows = [[to_cell(v) for v in row]
                                for row in df.head(5).itertuples(index=False, name=None)]
                        total_rows = len(df)
                    except Exception:  # noqa: BLE001 — fichier usager illisible : l'aperçu est juste omis
                        traceback.print_exc()

                fields_info.append({
                    "label": label,
                    "table_name": table_names[label],
                    "seen": info["seen"],
                    "found": bool(info["files"]),
                    "pj_seen": info["pj_seen"],
                    "exts": info["exts"],
                    "sample_rows": total_rows,
                    "schema": schema,
                    "headers": headers,
                    "rows": rows,
                })

            # Champs avec tableur trouvé d'abord, puis les plus fréquents
            fields_info.sort(key=lambda f: (not f["found"], -f["seen"]))
            yield ndjson({"type": "result", "title": title, "scanned": scanned, "fields": fields_info})
        except (requests.RequestException, ValueError) as e:
            traceback.print_exc()
            yield ndjson({"type": "error", "error": str(e)})

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/import", methods=["POST"])
def start_import():
    """Enregistre un job d'import et retourne son ID (les tokens ne transitent pas par l'URL)."""
    body = request.json or {}
    required = ["ds_token", "grist_token", "doc_id", "demarche_number", "selected_labels"]
    if not all(body.get(k) for k in required):
        return jsonify({"error": "Paramètres manquants"}), 400

    job = {
        "ds_token": body["ds_token"],
        "grist_token": body["grist_token"],
        "doc_id": body["doc_id"],
        "demarche_number": int(body["demarche_number"]),
        "on_duplicate": body.get("on_duplicate") if body.get("on_duplicate") in DUPLICATE_MODES else "skip",
        "selected_labels": list(body["selected_labels"]),
        "states": _parse_states(body.get("states")),
        "groupe_numbers": _parse_groupes(body.get("groupe_numbers")),
        "options": _read_options(body),
        # En-têtes du fichier d'aperçu, par champ : référence pour les fichiers sans en-tête
        "templates": {k: [str(h) for h in v][:200] for k, v in (body.get("templates") or {}).items()
                      if isinstance(v, list)},
        "created": time.time(),
    }
    job_id = secrets.token_urlsafe(16)
    with JOBS_LOCK:
        now = time.time()
        for k in [k for k, j in JOBS.items() if now - j["created"] > JOB_TTL]:
            del JOBS[k]
        JOBS[job_id] = job
    return jsonify({"job_id": job_id})


@app.route("/api/import-stream")
def import_stream():
    """SSE : importe les champs PJ sélectionnés pour tous les dossiers."""
    with JOBS_LOCK:
        job = JOBS.pop(request.args.get("job", ""), None)
    if not job:
        return jsonify({"error": "Import inconnu ou expiré"}), 404

    ds_token, grist_token, doc_id = job["ds_token"], job["grist_token"], job["doc_id"]
    on_duplicate, labels = job["on_duplicate"], job["selected_labels"]

    def generate():
        tables = unique_table_names(labels)          # label → ID de table (réel une fois connu)
        exists = {lab: False for lab in labels}
        colmap = {lab: {} for lab in labels}         # label → {id voulu: id réel}
        registries = {lab: ColumnRegistry() for lab in labels}
        known_cols = {lab: set() for lab in labels}     # colonnes réelles de la table Grist
        stats = {lab: {"imported": 0, "skipped": 0, "errors": 0} for lab in labels}
        step = 0

        try:
            existing_tables = list_tables(doc_id, grist_token)
            for lab in labels:
                real = find_table(tables[lab], existing_tables)
                if real:
                    tables[lab], exists[lab] = real, True
                    # Les en-têtes déjà présents dans Grist gardent leur colonne
                    grist_cols = list_columns(doc_id, real, grist_token)
                    registries[lab].seed(grist_cols)
                    known_cols[lab] = {c["id"] for c in grist_cols}
            for lab in labels:
                registries[lab].seed_headers(job["templates"].get(lab, []))

            for kind, payload in iter_dossiers(job["demarche_number"], ds_token,
                                               states=job["states"], groupe_numbers=job["groupe_numbers"]):
                if kind == "meta":
                    yield sse({"type": "meta", "title": payload["title"],
                               "tables": [{"label": lb, "table": tables[lb]} for lb in labels]})
                    continue
                if kind == "count":
                    yield sse({"type": "count", "total": payload["total"]})
                    continue
                if kind == "done":
                    yield sse({"type": "done", "total": payload["total"], "stats": stats})
                    return

                step += 1
                num, fields = payload["number"], payload["fields"]
                yield sse({"type": "progress", "step": step, "dossier": num})

                for lab in labels:
                    def ev(type_, *, _step=step, _num=num, _lab=lab, **kw):
                        return sse({"type": type_, "step": _step, "dossier": _num,
                                    "label": _lab, "table": tables[_lab], **kw})

                    files = fields.get(lab, [])
                    if not files:
                        stats[lab]["skipped"] += 1
                        yield ev("skip", reason="no_files")
                        continue

                    # Doublons
                    existing_ids = []
                    if exists[lab] and on_duplicate != "append":
                        try:
                            existing_ids = check_existing_dossier(doc_id, tables[lab], num, grist_token)
                        except requests.RequestException as e:
                            stats[lab]["errors"] += 1
                            yield ev("error", msg=f"Vérification doublon : {e}")
                            continue
                    if existing_ids and on_duplicate == "skip":
                        stats[lab]["skipped"] += 1
                        yield ev("skip", reason="duplicate")
                        continue

                    # Téléchargement + concaténation
                    try:
                        reg = registries[lab]
                        dfs = [file_to_df(f, ds_token, num, reg, job["options"])
                               for f in files]
                        merged = pd.concat(dfs, ignore_index=True) if len(dfs) > 1 else dfs[0]
                        col_labels = reg.labels
                    except Exception as e:  # noqa: BLE001 — fichier usager : toute erreur de lecture est isolée au dossier
                        stats[lab]["errors"] += 1
                        yield ev("error", msg=f"Lecture fichier : {e}")
                        continue

                    # Table et colonnes (création ou ajout des colonnes manquantes)
                    try:
                        columns = build_columns(merged, col_labels)
                        if not exists[lab]:
                            tables[lab] = create_table(doc_id, tables[lab], columns, grist_token)
                            exists[lab] = True
                        if set(merged.columns) - colmap[lab].keys():
                            colmap[lab].update(ensure_columns(doc_id, tables[lab], columns, grist_token))
                            known_cols[lab].update(colmap[lab].values())
                    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError) as e:
                        traceback.print_exc()
                        stats[lab]["errors"] += 1
                        yield ev("error", msg=f"Table / colonnes : {e}")
                        continue

                    # Insertion, puis suppression des anciennes lignes en mode « Remplacer »
                    try:
                        records = df_to_records(merged, colmap[lab], all_cols=known_cols[lab])
                        add_rows(doc_id, tables[lab], records, grist_token)
                        if existing_ids and on_duplicate == "update":
                            delete_rows(doc_id, tables[lab], existing_ids, grist_token)
                        stats[lab]["imported"] += 1
                        yield ev("imported", rows=len(records), replaced=bool(existing_ids))
                    except (requests.RequestException, RuntimeError, ValueError, TypeError) as e:
                        stats[lab]["errors"] += 1
                        yield ev("error", msg=str(e))

        except Exception as e:  # noqa: BLE001 — dernier filet : le flux SSE doit signaler l'échec au navigateur
            traceback.print_exc()
            yield sse({"type": "fatal", "msg": str(e)})

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=int(os.getenv("PORT", "5000")), threaded=True)
