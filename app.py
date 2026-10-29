import io
import json
import os
import secrets
import threading
import time
import traceback

import pandas as pd
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from demarche import DOSSIER_STATE_LABELS, collect_fields, download_file, get_demarche_meta, iter_dossiers
from grist import (
    add_rows, build_columns, check_existing_dossier, create_table, delete_rows,
    df_to_records, ensure_columns, find_table, infer_grist_type, list_tables, to_cell,
)
from utils import unique_col_ids, unique_table_names

app = Flask(__name__)

# Jobs d'import en attente : les tokens restent côté serveur, l'EventSource ne reçoit qu'un ID
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 300  # secondes


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, default=str)}\n\n"


def file_to_df(url: str, ds_token: str, dossier_number: int, filename: str):
    """Lit un tableur. Retourne (df aux colonnes nettoyées et uniques, {id: libellé d'origine})."""
    content = download_file(url, ds_token)
    ext = filename.rsplit(".", 1)[-1].lower()
    df = pd.read_excel(io.BytesIO(content), engine="odf" if ext == "ods" else None)
    df.insert(0, "source_filename", filename)
    df.insert(0, "dossier_number", dossier_number)
    originals = [str(c) for c in df.columns]
    ids = unique_col_ids(originals)
    df.columns = ids
    return df, dict(zip(ids, originals))


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
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fields", methods=["POST"])
def get_fields():
    """Analyse les premiers dossiers et retourne les champs PJ tableur trouvés, avec aperçu."""
    body = request.json or {}
    ds_token, demarche_number = body.get("ds_token"), body.get("demarche_number")
    if not ds_token or not demarche_number:
        return jsonify({"error": "ds_token et demarche_number requis"}), 400

    try:
        title, fields, scanned = collect_fields(
            int(demarche_number), ds_token,
            states=_parse_states(body.get("states")),
            groupe_numbers=_parse_groupes(body.get("groupe_numbers")),
        )
        table_names = unique_table_names(fields.keys())

        fields_info = []
        for label, info in fields.items():
            schema, headers, rows = [], [], []
            try:
                f = info["files"][0]
                df, labels = file_to_df(f["url"], ds_token, 0, f["filename"])
                for col in df.columns:
                    non_null = df[col].dropna()
                    sample = to_cell(non_null.iloc[0]) if not non_null.empty else None
                    schema.append({
                        "id": col, "original": labels[col],
                        "type": infer_grist_type(df[col]),
                        "sample": "" if sample is None else str(sample),
                    })
                headers = [labels[c] for c in df.columns]
                rows = [[to_cell(v) for v in row] for row in df.head(5).itertuples(index=False, name=None)]
            except Exception:
                traceback.print_exc()

            fields_info.append({
                "label": label,
                "table_name": table_names[label],
                "seen": info["seen"],
                "schema": schema,
                "headers": headers,
                "rows": rows,
            })

        return Response(json.dumps({"title": title, "scanned": scanned, "fields": fields_info}, default=str),
                        mimetype="application/json")
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


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
        "on_duplicate": body.get("on_duplicate") if body.get("on_duplicate") in ("skip", "update", "append") else "skip",
        "selected_labels": list(body["selected_labels"]),
        "states": _parse_states(body.get("states")),
        "groupe_numbers": _parse_groupes(body.get("groupe_numbers")),
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
        stats = {lab: {"imported": 0, "skipped": 0, "errors": 0} for lab in labels}
        step = 0

        try:
            existing_tables = list_tables(doc_id, grist_token)
            for lab in labels:
                real = find_table(tables[lab], existing_tables)
                if real:
                    tables[lab], exists[lab] = real, True

            for kind, payload in iter_dossiers(job["demarche_number"], ds_token,
                                               states=job["states"], groupe_numbers=job["groupe_numbers"]):
                if kind == "meta":
                    yield sse({"type": "meta", "title": payload["title"],
                               "tables": [{"label": l, "table": tables[l]} for l in labels]})
                    continue
                if kind == "done":
                    yield sse({"type": "done", "total": payload["total"], "stats": stats})
                    return

                step += 1
                num, fields = payload["number"], payload["fields"]
                yield sse({"type": "progress", "step": step, "dossier": num})

                for lab in labels:
                    def ev(type_, **kw):
                        return sse({"type": type_, "step": step, "dossier": num,
                                    "label": lab, "table": tables[lab], **kw})

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
                        except Exception as e:
                            stats[lab]["errors"] += 1
                            yield ev("error", msg=f"Vérification doublon : {e}")
                            continue
                    if existing_ids and on_duplicate == "skip":
                        stats[lab]["skipped"] += 1
                        yield ev("skip", reason="duplicate")
                        continue

                    # Téléchargement + concaténation
                    try:
                        dfs, col_labels = [], {}
                        for f in files:
                            df, lbls = file_to_df(f["url"], ds_token, num, f["filename"])
                            dfs.append(df)
                            col_labels.update(lbls)
                        merged = pd.concat(dfs, ignore_index=True) if len(dfs) > 1 else dfs[0]
                    except Exception as e:
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
                    except Exception as e:
                        traceback.print_exc()
                        stats[lab]["errors"] += 1
                        yield ev("error", msg=f"Table / colonnes : {e}")
                        continue

                    # Insertion, puis suppression des anciennes lignes en mode « Remplacer »
                    try:
                        records = df_to_records(merged, colmap[lab])
                        add_rows(doc_id, tables[lab], records, grist_token)
                        if existing_ids and on_duplicate == "update":
                            delete_rows(doc_id, tables[lab], existing_ids, grist_token)
                        stats[lab]["imported"] += 1
                        yield ev("imported", rows=len(records), replaced=bool(existing_ids))
                    except Exception as e:
                        stats[lab]["errors"] += 1
                        yield ev("error", msg=str(e))

        except Exception as e:
            traceback.print_exc()
            yield sse({"type": "fatal", "msg": str(e)})

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=int(os.getenv("PORT", 5000)), threaded=True)
