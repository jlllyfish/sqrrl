const $ = (id) => document.getElementById(id);
function show(id) {
  $(id).classList.remove("fr-hidden");
}
function hide(id) {
  $(id).classList.add("fr-hidden");
}

// Échappe tout contenu dynamique avant insertion en HTML (labels, en-têtes et cellules
// proviennent de fichiers déposés par les usagers)
function esc(v) {
  return String(v ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function toast(msg, type = "info") {
  const t = $("toast");
  t.className = `fr-alert fr-alert--${type} sqrrl-toast`;
  $("toast-msg").textContent = msg;
  show("toast");
  setTimeout(() => hide("toast"), 5000);
}

function setLoading(btn, loading, label) {
  btn.disabled = loading;
  btn.textContent = loading ? "Chargement…" : label;
}

const TYPE_ICONS = {
  Int: "123",
  Numeric: "1.5",
  Text: "Aa",
  Date: "📅",
  Bool: "☑",
};
const typeIcon = (t) => (t?.startsWith("DateTime") ? "🕒" : TYPE_ICONS[t] || "Aa");

let state = {
  fields: [],
  selected: new Set(),
  selectedStates: null,
  selectedGroupes: [],
};

// ── Étape 1 : vérification des connexions ────────────────────────────────────
function setStatus(id, cls, text) {
  const el = $(id);
  el.className = `fr-text--sm fr-mb-0 ${cls}`;
  el.textContent = text;
}

function debounce(fn, ms = 700) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

let dsCheckId = 0;
let dsMeta = null; // métadonnées de la dernière démarche vérifiée

async function checkDs({ showFilters = false } = {}) {
  const ds_token = $("ds_token").value.trim();
  const demarche_number = $("demarche_number").value.trim();
  if (!ds_token || !demarche_number) {
    setStatus("ds-status", "", "");
    dsMeta = null;
    return false;
  }
  const id = ++dsCheckId;
  setStatus("ds-status", "sqrrl-wait", "Vérification de la démarche…");
  try {
    const res = await fetch("/api/meta", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ds_token, demarche_number: parseInt(demarche_number) }),
    });
    const data = await res.json();
    if (id !== dsCheckId) return false; // une saisie plus récente a pris le relais
    if (data.error) throw new Error(data.error);

    const key = `${demarche_number}|${ds_token}`;
    const changed = dsMeta?.key !== key;
    dsMeta = { ...data, key };
    const n = data.groupes.length;
    setStatus(
      "ds-status",
      "sqrrl-ok",
      `✓ Démarche n° ${demarche_number} : ${data.title || "(sans titre)"} · ${n} groupe${n > 1 ? "s" : ""} instructeur${n > 1 ? "s" : ""}`,
    );
    if (changed || showFilters) {
      $("filters-title").textContent = data.title || `Démarche #${demarche_number}`;
      renderStateOptions(data.states);
      renderGroupesOptions(data.groupes);
      show("step-filters");
      hide("step-2");
      hide("step-progress");
    }
    return true;
  } catch (e) {
    if (id !== dsCheckId) return false;
    dsMeta = null;
    hide("step-filters");
    hide("step-2");
    setStatus("ds-status", "sqrrl-ko", `✗ ${dsErrorMessage(e.message)}`);
    return false;
  }
}

function dsErrorMessage(msg) {
  if (/401|unauthorized|token/i.test(msg)) return "Token DS refusé";
  if (/not found|introuvable|null|permission|autoris/i.test(msg))
    return "Démarche introuvable ou non accessible avec ce token";
  return msg;
}

let gristCheckId = 0;
let gristOk = false;

async function checkGrist() {
  const grist_token = $("grist_token").value.trim();
  const doc_id = $("doc_id").value.trim();
  gristOk = false;
  if (!grist_token || !doc_id) {
    setStatus("grist-status", "", "");
    return false;
  }
  const id = ++gristCheckId;
  setStatus("grist-status", "sqrrl-wait", "Vérification du document Grist…");
  try {
    const res = await fetch("/api/grist-check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ grist_token, doc_id }),
    });
    const data = await res.json();
    if (id !== gristCheckId) return false;
    if (data.error) throw new Error(data.error);
    gristOk = true;
    const n = data.tables.length;
    setStatus(
      "grist-status",
      "sqrrl-ok",
      `✓ Document Grist : ${data.name || doc_id} · ${n} table${n > 1 ? "s" : ""}`,
    );
    return true;
  } catch (e) {
    if (id !== gristCheckId) return false;
    setStatus("grist-status", "sqrrl-ko", `✗ ${e.message}`);
    return false;
  }
}

const checkDsLater = debounce(() => checkDs());
const checkGristLater = debounce(() => checkGrist());
["ds_token", "demarche_number"].forEach((id) => {
  $(id).addEventListener("input", checkDsLater);
  $(id).addEventListener("change", () => checkDs());
});
["grist_token", "doc_id"].forEach((id) => {
  $(id).addEventListener("input", checkGristLater);
  $(id).addEventListener("change", () => checkGrist());
});

$("btn-analyze").addEventListener("click", async () => {
  const btn = $("btn-analyze");
  const label = btn.textContent;
  if (!$("ds_token").value.trim() || !$("demarche_number").value.trim()) {
    toast("Token DS et numéro de démarche requis", "error");
    return;
  }
  setLoading(btn, true, label);
  try {
    await Promise.all([checkDs({ showFilters: true }), checkGrist()]);
  } finally {
    setLoading(btn, false, label);
  }
});

function makeMultiSelect({ inputId, tagsId, dropdownId, items, labelKey, valueKey }) {
  // Remplace l'input par un clone pour ne pas cumuler les écouteurs à chaque analyse
  const oldInput = $(inputId);
  const input = oldInput.cloneNode(true);
  input.value = "";
  oldInput.replaceWith(input);

  const tagsEl = $(tagsId);
  const dropdown = $(dropdownId);
  const selected = new Map(); // value → label
  tagsEl.innerHTML = "";
  dropdown.classList.add("fr-hidden");

  function renderTags() {
    tagsEl.innerHTML = [...selected.entries()]
      .map(
        ([v, l]) => `
      <span class="sqrrl-ms-tag">
        ${esc(l)}
        <button type="button" data-val="${esc(v)}" aria-label="Retirer ${esc(l)}">×</button>
      </span>`,
      )
      .join("");
    tagsEl.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", () => {
        selected.delete(btn.dataset.val);
        renderTags();
        renderDropdown(input.value);
      });
    });
  }

  function renderDropdown(filter = "") {
    const f = filter.toLowerCase();
    const filtered = items.filter((it) => it[labelKey].toLowerCase().includes(f));
    dropdown.innerHTML =
      filtered
        .map(
          (it) => `
      <li data-val="${esc(it[valueKey])}" class="${selected.has(String(it[valueKey])) ? "selected" : ""}">
        ${esc(it[labelKey])}
      </li>`,
        )
        .join("") ||
      '<li style="color:var(--text-mention-grey);cursor:default">Aucun résultat</li>';
    dropdown.querySelectorAll("li[data-val]").forEach((li) => {
      li.addEventListener("click", () => {
        const v = li.dataset.val;
        const l = items.find((it) => String(it[valueKey]) === v)?.[labelKey] || v;
        if (selected.has(v)) selected.delete(v);
        else selected.set(v, l);
        renderTags();
        renderDropdown(input.value);
        input.value = "";
      });
    });
  }

  input.addEventListener("click", () => {
    dropdown.classList.toggle("fr-hidden");
    if (!dropdown.classList.contains("fr-hidden")) renderDropdown("");
  });

  input.addEventListener("input", () => {
    dropdown.classList.remove("fr-hidden");
    renderDropdown(input.value);
  });

  if (dropdown._outsideHandler) document.removeEventListener("click", dropdown._outsideHandler);
  dropdown._outsideHandler = (e) => {
    if (!dropdown.parentElement.contains(e.target)) dropdown.classList.add("fr-hidden");
  };
  document.addEventListener("click", dropdown._outsideHandler);

  return {
    getSelected: () => (selected.size > 0 ? [...selected.keys()] : null),
    getSelectedLabels: () => [...selected.values()],
  };
}

let msState = null;
let msGroupes = null;

function renderStateOptions(states) {
  msState = makeMultiSelect({
    inputId: "ms-state-input",
    tagsId: "ms-state-tags",
    dropdownId: "ms-state-dropdown",
    items: states,
    labelKey: "label",
    valueKey: "value",
  });
}

function renderGroupesOptions(groupes) {
  const input = $("ms-groupes-input");
  if (!groupes.length) {
    $("ms-groupes-tags").innerHTML = "";
    input.disabled = true;
    input.placeholder = "Aucun groupe défini";
    msGroupes = { getSelected: () => null };
    return;
  }
  input.disabled = false;
  input.placeholder = "Sélectionnez ou commencez à saisir";
  msGroupes = makeMultiSelect({
    inputId: "ms-groupes-input",
    tagsId: "ms-groupes-tags",
    dropdownId: "ms-groupes-dropdown",
    items: groupes,
    labelKey: "label",
    valueKey: "number",
  });
}

// ── Étape 1b ──────────────────────────────────────────────────────────────────
$("btn-confirm-filters").addEventListener("click", async () => {
  const ds_token = $("ds_token").value.trim();
  const demarche_number = $("demarche_number").value.trim();

  state.selectedStates = msState?.getSelected() || null;
  const selectedGroupes = msGroupes?.getSelected() || null;
  state.selectedGroupes = selectedGroupes ? selectedGroupes.map((v) => parseInt(v)) : [];

  const btn = $("btn-confirm-filters");
  setLoading(btn, true, "Confirmer les filtres");

  try {
    const res = await fetch("/api/fields", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ds_token,
        demarche_number: parseInt(demarche_number),
        states: state.selectedStates,
        groupe_numbers: state.selectedGroupes.length ? state.selectedGroupes : null,
        ...readOptions(),
      }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    if (!data.fields.length) {
      toast("Aucun champ PJ tableur trouvé avec ces filtres", "warning");
      return;
    }

    state.fields = data.fields;
    state.scanned = data.scanned;
    state.selected = new Set(data.fields.filter((f) => f.found).map((f) => f.label));

    let subtitle = "";
    const stateLabels = msState?.getSelectedLabels?.() || [];
    if (stateLabels.length) subtitle += ` · ${stateLabels.join(", ")}`;
    if (state.selectedGroupes.length) subtitle += ` · ${state.selectedGroupes.length} groupe(s)`;
    $("demarche-title").textContent = (data.title || `Démarche #${demarche_number}`) + subtitle;

    renderFields(data.fields);
    show("step-2");
  } catch (e) {
    toast("Erreur : " + e.message, "error");
  } finally {
    setLoading(btn, false, "Confirmer les filtres");
  }
});

function readOptions() {
  return {
    require_first_col: $("opt-first-col").checked,
    compact_text: $("opt-compact").checked,
  };
}

// ── Étape 2 ───────────────────────────────────────────────────────────────────
function renderFields(fields) {
  const list = $("fields-list");
  list.innerHTML = "";
  const found = fields.filter((f) => f.found);
  const others = fields.filter((f) => !f.found);

  found.forEach((f) => list.appendChild(fieldCard(f, true)));

  if (others.length) {
    const h = document.createElement("p");
    h.className = "fr-text--sm sqrrl-subtitle";
    h.textContent = `Autres champs pièce justificative (${others.length})`;
    const hint = document.createElement("p");
    hint.className = "fr-text--sm fr-text-mention--grey fr-mb-1w";
    hint.textContent = `Aucun tableur trouvé dans les ${state.scanned} dossiers les plus récents. Vous pouvez les cocher : les dossiers qui en contiennent seront importés.`;
    list.append(h, hint);
    others.forEach((f) => list.appendChild(fieldCard(f, false)));
  }
  updateImportBtn();
}

function fieldCard(f, checked) {
  const div = document.createElement("div");
  div.className = `sqrrl-field-card${f.found ? "" : " sqrrl-field-card--muted"}`;
  const badge = f.found
    ? `<span class="fr-badge fr-badge--sm" title="Dossiers contenant un tableur pour ce champ, sur les ${state.scanned} plus récents analysés">
         ${f.seen} / ${state.scanned} dossier${state.scanned > 1 ? "s" : ""}
       </span>`
    : `<span class="fr-badge fr-badge--sm fr-badge--warning">aucun tableur trouvé</span>`;
  const rowsInfo =
    f.sample_rows != null
      ? `<p class="fr-text--xs fr-text-mention--grey fr-mt-1w fr-mb-0">Exemple : ${f.sample_rows} ligne${f.sample_rows > 1 ? "s" : ""} après nettoyage</p>`
      : "";
  div.innerHTML = `
    <div class="sqrrl-field-card-header">
      <label class="sqrrl-field-label">
        <input type="checkbox" class="fr-checkbox field-check" ${checked ? "checked" : ""} />
        <span>${esc(f.label)}</span>
      </label>
      <div class="sqrrl-field-meta">
        ${badge}
        <span class="fr-text--sm fr-text-mention--grey">→ ${esc(f.table_name)}</span>
      </div>
    </div>
    ${f.headers.length ? renderMiniGrid(f) + rowsInfo : f.found ? '<p class="fr-text--sm fr-text-mention--grey fr-mt-1w">Aperçu indisponible</p>' : ""}
  `;
  const cb = div.querySelector(".field-check");
  cb.dataset.label = f.label; // via dataset : pas d'échappement nécessaire
  cb.addEventListener("change", onFieldCheck);
  return div;
}

function renderMiniGrid(f) {
  let html =
    '<div class="sqrrl-mini-grid-wrap"><table class="sqrrl-mini-grid"><thead><tr><th class="row-num"></th>';
  f.headers.forEach((h, i) => {
    html += `<th title="${esc(h)}">${esc(h)} <small>${typeIcon(f.schema[i]?.type)}</small></th>`;
  });
  html += "</tr></thead><tbody>";
  f.rows.forEach((row, ri) => {
    html += `<tr><td class="row-num">${ri + 1}</td>`;
    row.forEach((val) => {
      const empty = val === null || val === undefined || val === "";
      html += empty ? `<td class="null-cell">—</td>` : `<td title="${esc(val)}">${esc(val)}</td>`;
    });
    html += "</tr>";
  });
  html += "</tbody></table></div>";
  return html;
}

function onFieldCheck(e) {
  const label = e.target.dataset.label;
  if (e.target.checked) state.selected.add(label);
  else state.selected.delete(label);
  updateImportBtn();
}

function updateImportBtn() {
  const btn = $("btn-import");
  const n = state.selected.size;
  btn.disabled = n === 0;
  btn.textContent = n > 1 ? `Importer ${n} tables` : n === 1 ? "Importer 1 table" : "Sélectionnez un champ";
}

$("btn-select-all-fields").addEventListener("click", () => {
  const checkboxes = document.querySelectorAll(".field-check");
  const allChecked = [...checkboxes].every((c) => c.checked);
  checkboxes.forEach((c) => {
    c.checked = !allChecked;
    if (!allChecked) state.selected.add(c.dataset.label);
    else state.selected.delete(c.dataset.label);
  });
  $("btn-select-all-fields").textContent = allChecked ? "Tout sélectionner" : "Tout désélectionner";
  updateImportBtn();
});

// ── Étape 3 : Import SSE ──────────────────────────────────────────────────────
$("btn-import").addEventListener("click", async () => {
  const ds_token = $("ds_token").value.trim();
  const grist_token = $("grist_token").value.trim();
  const doc_id = $("doc_id").value.trim();
  const demarche_number = $("demarche_number").value.trim();
  const on_duplicate = $("on_duplicate").value;

  if (!ds_token || !grist_token || !doc_id || !demarche_number) {
    toast("Tous les champs sont requis", "error");
    return;
  }
  if (!state.selected.size) {
    toast("Sélectionnez au moins un champ", "warning");
    return;
  }
  if (!gristOk && !(await checkGrist())) {
    toast("Connexion Grist invalide : vérifiez le token et l'ID du document", "error");
    return;
  }

  // 1. Création du job côté serveur (les tokens partent en POST, pas dans l'URL)
  let job_id;
  try {
    const res = await fetch("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ds_token,
        grist_token,
        doc_id,
        demarche_number: parseInt(demarche_number),
        on_duplicate,
        selected_labels: [...state.selected],
        states: state.selectedStates,
        groupe_numbers: state.selectedGroupes,
        ...readOptions(),
      }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    job_id = data.job_id;
  } catch (e) {
    toast("Erreur : " + e.message, "error");
    return;
  }

  const bar = $("progress-bar");
  bar.style.width = "0%";
  bar.style.background = "";
  bar.classList.add("indeterminate");
  $("prog-step").textContent = "0";
  $("prog-total").textContent = "?";
  $("progress-log").innerHTML = "";
  $("table-stats").innerHTML = "";
  $("progress-title").textContent = "Import en cours…";
  show("step-progress");
  $("btn-import").disabled = true;

  // Stats indexées par label de champ (le nom de table réel peut être ajusté par Grist)
  const tableStats = {};
  state.selected.forEach((label) => {
    const tname = state.fields.find((x) => x.label === label)?.table_name || label;
    tableStats[label] = { table: tname, imported: 0, skipped: 0, errors: 0 };
  });
  renderTableStats(tableStats);

  const bump = (msg, key) => {
    const s = tableStats[msg.label];
    if (!s) return;
    s[key]++;
    if (msg.table) s.table = msg.table;
    renderTableStats(tableStats);
  };

  let finished = false;
  const finish = () => {
    finished = true;
    es.close();
    bar.classList.remove("indeterminate");
    updateImportBtn();
  };

  const es = new EventSource(`/api/import-stream?job=${encodeURIComponent(job_id)}`);

  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);

    if (msg.type === "meta") {
      $("progress-title").textContent = `Import : ${msg.title || "#" + demarche_number}`;
      (msg.tables || []).forEach((t) => tableStats[t.label] && (tableStats[t.label].table = t.table));
      renderTableStats(tableStats);
    }
    if (msg.type === "progress") {
      $("prog-step").textContent = msg.step;
      addLog(`Dossier #${msg.dossier}…`, "sqrrl-log-skip");
    }
    if (msg.type === "imported") {
      bump(msg, "imported");
      addLog(
        `✓ Dossier #${msg.dossier} → ${msg.table} (${msg.rows} lignes${msg.replaced ? ", remplacées" : ""})`,
        "sqrrl-log-ok",
      );
    }
    if (msg.type === "skip") {
      bump(msg, "skipped");
      const reason = msg.reason === "duplicate" ? "déjà importé" : "sans PJ";
      addLog(`– Dossier #${msg.dossier} · ${msg.table} ignoré (${reason})`, "sqrrl-log-skip");
    }
    if (msg.type === "error") {
      bump(msg, "errors");
      addLog(`✗ Dossier #${msg.dossier} · ${msg.table} : ${msg.msg}`, "sqrrl-log-err");
    }
    if (msg.type === "fatal") {
      finish();
      toast("Erreur fatale : " + msg.msg, "error");
      bar.style.background = "var(--background-flat-error)";
      $("progress-title").textContent = "✗ Import interrompu";
    }
    if (msg.type === "done") {
      finish();
      $("prog-total").textContent = msg.total;
      bar.style.width = "100%";
      const totalImported = Object.values(tableStats).reduce((s, t) => s + t.imported, 0);
      $("progress-title").textContent = `✓ Terminé — ${totalImported} import(s) pour ${msg.total} dossier(s)`;
      addLog("✓ Import terminé", "sqrrl-log-ok");
    }
  };

  es.onerror = () => {
    if (finished) return;
    finish();
    bar.style.background = "var(--background-flat-error)";
    $("progress-title").textContent = "✗ Connexion interrompue";
    toast("La connexion avec le serveur a été interrompue. Relancez l'import (les doublons seront gérés).", "error");
  };
});

function renderTableStats(tableStats) {
  $("table-stats").innerHTML = Object.values(tableStats)
    .map(
      (s) => `
    <div class="sqrrl-table-stat">
      <span class="sqrrl-table-stat-name">${esc(s.table)}</span>
      <span class="sqrrl-stat-ok">${s.imported} ✓</span>
      <span class="sqrrl-stat-muted">${s.skipped} –</span>
      <span class="sqrrl-stat-err">${s.errors} ✗</span>
    </div>`,
    )
    .join("");
}

function addLog(text, cls) {
  const log = $("progress-log");
  // Une ligne « Dossier #… » en attente est remplacée par son résultat
  const last = log.lastElementChild;
  const el = last && last.dataset.pending === "1" ? last : document.createElement("div");
  el.dataset.pending = text.endsWith("…") ? "1" : "0";
  el.className = `sqrrl-log-entry ${cls}`;
  el.textContent = text;
  if (el !== last) log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}
