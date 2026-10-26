const $ = (selector, root = document) => root.querySelector(selector);

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

async function api(path, options = {}) {
  const response = await fetch("/api" + path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const details = body.details ? body.details.map((d) => `${d.champ} : ${d.raison}`).join(" ; ") : "";
    throw new Error((body.erreur || response.statusText) + (details ? " — " + details : ""));
  }
  return body;
}

const fcfa = (n) => Number(n).toLocaleString("fr-FR") + " FCFA";
const when = (iso) => new Date(iso).toLocaleString("fr-FR");
const ms = (n) => (n === null || n === undefined ? "—" : n >= 1000 ? (n / 1000).toFixed(2) + " s" : Math.round(n) + " ms");

let toastTimer;
function toast(message, bad = false) {
  const box = $("#toast");
  box.textContent = message;
  box.className = bad ? "bad" : "";
  box.style.display = "block";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.style.display = "none"), 5000);
}

const guard = (fn) => async (...args) => {
  try {
    await fn(...args);
  } catch (error) {
    toast(error.message, true);
  }
};

let currentTab = "tx";
document.querySelectorAll("nav button").forEach((button) =>
  button.addEventListener("click", () => {
    currentTab = button.dataset.tab;
    document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b === button));
    document.querySelectorAll("main > section").forEach((s) => (s.hidden = s.id !== "tab-" + currentTab));
    refreshTab();
  })
);

function refreshTab() {
  if (currentTab === "tx") loadTx(true);
  if (currentTab === "imports") loadImports();
  if (currentTab === "balance") loadBalance();
  if (currentTab === "results") loadResults();
  loadTotal();
}

async function loadTotal() {
  try {
    const data = await api("/balance");
    $("#total").textContent = "Solde : " + fcfa(data.balance);
  } catch {
    $("#total").textContent = "";
  }
}

let txCursor = null;

function filterQuery() {
  const form = new FormData($("#filters"));
  const params = new URLSearchParams();
  for (const [key, value] of form.entries()) if (value !== "") params.set(key, value === "on" ? "true" : value);
  return params;
}

const loadTx = guard(async (reset) => {
  const params = filterQuery();
  if (!reset && txCursor) params.set("cursor", txCursor);
  const data = await api("/transactions?" + params.toString());
  const body = $("#tx-rows");
  if (reset) body.replaceChildren();
  const deletedView = params.get("deleted") === "true";
  for (const tx of data.items) body.append(txRow(tx, deletedView));
  if (reset && !data.items.length) body.append(el("tr", {}, el("td", { colspan: 6, class: "muted" }, "Aucune transaction ne correspond. Ajoutez-en une ci-dessus ou importez un fichier.")));
  txCursor = data.next_cursor;
  $("#tx-more").hidden = !txCursor;
});

function txRow(tx, deletedView) {
  const actions = deletedView
    ? [el("button", { class: "ghost", onclick: guard(async () => { await api(`/transactions/${tx.id}/restore`, { method: "POST" }); toast("Transaction restaurée"); refreshTab(); }) }, "Restaurer")]
    : [
        el("button", { class: "ghost", onclick: guard(() => editTx(tx)) }, "Modifier"),
        el("button", { class: "ghost", onclick: guard(() => showSource(tx)) }, "Source"),
        el("button", { class: "danger", onclick: guard(async () => { await api(`/transactions/${tx.id}`, { method: "DELETE" }); toast("Supprimée, restaurable pendant 24 heures"); refreshTab(); }) }, "Supprimer"),
      ];
  return el("tr", {},
    el("td", { class: tx.type }, tx.type),
    el("td", { class: "num" }, fcfa(tx.amount)),
    el("td", {}, tx.description, tx.manual_edit ? el("span", { class: "muted" }, " (modifiée à la main)") : null),
    el("td", {}, tx.agency || "—"),
    el("td", {}, when(tx.created_at)),
    el("td", {}, el("div", { class: "actions" }, actions))
  );
}

async function editTx(tx) {
  const amount = prompt("Nouveau montant en FCFA", tx.amount);
  if (amount === null) return;
  const description = prompt("Nouvelle description", tx.description);
  if (description === null) return;
  await api(`/transactions/${tx.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ amount: Number(amount), description }),
  });
  toast("Transaction modifiée");
  refreshTab();
}

async function showSource(tx) {
  const data = await api(`/transactions/${tx.id}/source`);
  toast(data.import_id ? `Fichier ${data.filename}, ligne ${data.source_line}` : "Saisie manuelle");
}

$("#create").addEventListener("submit", guard(async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  await api("/transactions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      type: form.get("type"),
      amount: Number(form.get("amount")),
      description: form.get("description"),
      agency: form.get("agency"),
    }),
  });
  event.target.reset();
  toast("Transaction ajoutée");
  refreshTab();
}));
$("#filters").addEventListener("submit", (event) => { event.preventDefault(); loadTx(true); });
$("#tx-more").addEventListener("click", () => loadTx(false));

let selectedImport = null;
let errorCursor = null;
let detailId = null;

$("#send").addEventListener("click", guard(async () => {
  const files = [...$("#files").files];
  if (!files.length) throw new Error("Choisissez au moins un fichier .txt ou .json");
  const agency = $("#agency").value;
  await Promise.all(files.map(async (file) => {
    try {
      const data = await api(`/imports?filename=${encodeURIComponent(file.name)}&agency=${encodeURIComponent(agency)}`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
      });
      if (data.already_known) toast(`${file.name} : fichier déjà reçu, même rapport affiché`);
      selectedImport = selectedImport || data.id;
    } catch (error) {
      toast(`${file.name} : ${error.message}`, true);
    }
  }));
  $("#files").value = "";
  loadImports();
}));

const loadImports = guard(async () => {
  const data = await api("/imports");
  const list = $("#import-list");
  list.replaceChildren();
  if (!data.items.length) list.append(el("p", { class: "muted" }, "Aucun import. Envoyez un ou plusieurs fichiers."));
  for (const imp of data.items) {
    const pct = imp.size_bytes ? Math.min(100, Math.round((imp.bytes_offset / imp.size_bytes) * 100)) : 0;
    list.append(el("div", { class: "card" + (imp.id === selectedImport ? " sel" : ""), tabindex: 0, onclick: () => { selectedImport = imp.id; loadImports(); } },
      el("h3", {}, imp.filename),
      el("div", { class: "muted" }, `${imp.agency || "sans agence"} · ${imp.status} · ${ms(imp.duration_ms)}`),
      imp.status === "running" ? el("progress", { value: imp.bytes_offset, max: imp.size_bytes }) : null,
      el("div", {}, `${pct} % · ${imp.inserted} insérées · ${imp.duplicates} doublons · ${imp.errors} erreurs`)
    ));
  }
  if (selectedImport) await loadDetail(selectedImport, false);
});

async function loadDetail(id, resetErrors) {
  const imp = await api(`/imports/${id}`);
  const box = $("#import-detail");
  const keepErrors = !resetErrors && detailId === id && $("#error-rows") ? $("#error-rows") : null;
  detailId = id;
  const errorRows = keepErrors || el("tbody", { id: "error-rows" });
  const more = keepErrors ? $("#err-more") : el("button", { id: "err-more", class: "ghost", hidden: true, onclick: guard(() => loadErrors(id, false)) }, "Charger plus d'erreurs");
  const context = keepErrors ? $("#context") : el("div", { id: "context" });
  const report = keepErrors ? $("#report") : el("div", { id: "report" });
  box.replaceChildren(el("div", { class: "panel" },
    el("h2", {}, imp.filename),
    el("p", {}, `Statut ${imp.status} · ${imp.lines_read} lignes lues · ${imp.inserted} insérées · ${imp.duplicates} doublons ignorés · ${imp.errors} erreurs · durée ${ms(imp.duration_ms)}` + (imp.status === "cancelled" ? ` · ${imp.rolled_back} lignes retirées` : "")),
    imp.failure ? el("p", { class: "DEBIT" }, imp.failure) : null,
    el("div", { class: "actions" },
      imp.status !== "cancelled" ? el("button", { class: "danger", onclick: guard(async () => { if (!confirm("Annuler cet import ? Les modifications manuelles seront conservées.")) return; const r = await api(`/imports/${id}/cancel`, { method: "POST" }); toast(`${r.rolled_back} lignes retirées`); loadImports(); loadTotal(); }) }, "Annuler l'import") : null,
      imp.errors ? el("a", { class: "btn", href: `/api/imports/${id}/errors.csv`, download: true }, "Erreurs en CSV") : null,
      el("button", { class: "ghost", onclick: guard(() => showReport(id)) }, "Qui a envoyé quoi")
    ),
    el("h2", {}, "Lignes rejetées"),
    el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "Ligne"), el("th", {}, "Raison"), el("th", {}, "Contenu"))), errorRows),
    more,
    context,
    report
  ));
  if (!keepErrors || (imp.errors > 0 && !errorRows.children.length)) await loadErrors(id, true);
}

async function loadErrors(id, reset) {
  const params = new URLSearchParams();
  if (!reset && errorCursor) params.set("cursor", errorCursor);
  const data = await api(`/imports/${id}/errors?` + params.toString());
  const body = $("#error-rows");
  if (reset) body.replaceChildren();
  for (const err of data.items) {
    body.append(el("tr", { tabindex: 0, onclick: guard(() => showContext(id, err.id)), onkeydown: (e) => { if (e.key === "Enter") showContext(id, err.id).catch((x) => toast(x.message, true)); } },
      el("td", { class: "num" }, err.line_no), el("td", {}, err.reason), el("td", {}, err.content.slice(0, 80))));
  }
  errorCursor = data.next_cursor;
  $("#err-more").hidden = !errorCursor;
}

async function showContext(id, errorId) {
  const data = await api(`/imports/${id}/errors/${errorId}/context?around=3`);
  const pre = el("pre", { class: "ctx" });
  const row = (item) => [el("span", { class: "ln" }, item.n), item.text, "\n"];
  data.before.forEach((item) => pre.append(...row(item)));
  const text = data.line.text;
  const col = Math.min(data.col, text.length);
  pre.append(el("span", { class: "bad" },
    el("span", { class: "ln" }, data.line.n), text.slice(0, col), el("mark", {}, text.slice(col, col + 1) || "⏎"), text.slice(col + 1)));
  data.after.forEach((item) => pre.append(...row(item)));
  $("#context").replaceChildren(
    el("p", {}, `Ligne ${data.line_no}, octets ${data.byte_start} à ${data.byte_end} : ${data.reason}`),
    pre
  );
}

async function showReport(id) {
  const data = await api(`/imports/${id}/transactions?limit=50`);
  const body = el("tbody", {});
  const fill = (items, imp) => items.forEach((t) => body.append(el("tr", {}, el("td", {}, t.id), el("td", {}, t.type), el("td", { class: "num" }, fcfa(t.amount)), el("td", {}, `${t.filename}, ligne ${t.source_line}`))));
  fill(data.items);
  let cursor = data.next_cursor;
  const more = el("button", { class: "ghost", hidden: !cursor, onclick: guard(async () => { const next = await api(`/imports/${id}/transactions?limit=50&cursor=${cursor}`); fill(next.items); cursor = next.next_cursor; more.hidden = !cursor; }) }, "Charger la suite");
  $("#report").replaceChildren(
    el("h2", {}, "Transactions insérées et leur source"),
    el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "UUID"), el("th", {}, "Type"), el("th", { class: "num" }, "Montant"), el("th", {}, "Source"))), body),
    more
  );
}

const loadBalance = guard(async () => {
  const at = $("#at").value;
  const query = at ? `?at=${at}` : "";
  const total = await api("/balance" + query);
  const agencies = await api("/balance/agencies" + query);
  $("#balance-total").textContent = fcfa(total.balance);
  const body = $("#balance-rows");
  body.replaceChildren();
  for (const row of agencies.items) body.append(el("tr", {}, el("td", {}, row.agency || "Sans agence"), el("td", { class: "num" }, fcfa(row.balance))));
});
$("#show-balance").addEventListener("click", loadBalance);

const loadResults = guard(async () => {
  const data = await api("/bench/results");
  const body = $("#result-rows");
  body.replaceChildren();
  if (!data.items.length) body.append(el("tr", {}, el("td", { colspan: 6, class: "muted" }, "Aucune mesure. Lancez : python tools/bench.py all")));
  for (const r of data.items) {
    body.append(el("tr", {},
      el("td", {}, r.challenge > 100 ? "Bonus " + (r.challenge - 100) : r.challenge),
      el("td", {}, r.label), el("td", { class: "num" }, ms(r.duration_ms)),
      el("td", { class: "num" }, r.rows_ok ?? "—"), el("td", { class: "num" }, r.errors_reported ?? "—"),
      el("td", {}, r.detail || "")));
  }
});

setInterval(() => {
  if (currentTab === "imports") loadImports();
  loadTotal();
}, 1000);

refreshTab();
