"use strict";
/* Carvana ranker UI.
 *
 * Polls /api/state once a second and re-renders. A run's stages take minutes, so polling is
 * plenty and avoids a websocket dependency.
 */

const POLL_MS = 1000;

const el = (id) => document.getElementById(id);
const show = (node, visible) => { node.hidden = !visible; };
const text = (node, value) => { node.textContent = value; };

let taxonomy = null;
let serverConfig = null;
let pasteVin = null;
let lastIngestCount = 0;

/* ---------- formatting ---------- */

const money = (value) =>
  value === null || value === undefined ? "—" : "$" + Math.round(value).toLocaleString();
const count = (value) =>
  value === null || value === undefined ? "—" : Math.round(value).toLocaleString();

function accidentTag(history) {
  if (!history) return '<span class="tag tag-warn">?</span>';
  if (history.accident_reported === true) return '<span class="tag tag-bad">yes</span>';
  if (history.accident_reported === false) return '<span class="tag tag-good">no</span>';
  return '<span class="tag tag-warn">?</span>';
}

/* Coerced to numbers locally rather than trusting the parsers to only ever emit ints.
   These values land in innerHTML, so the guarantee belongs here, not three modules away. */
const num = (value) => (Number.isFinite(Number(value)) ? Number(value) : null);

function autocheckCell(history) {
  const score = num(history && history.autocheck_score);
  if (score === null) return "—";
  const low = num(history.autocheck_score_low);
  const high = num(history.autocheck_score_high);
  return low !== null && high !== null ? `${score} (${low}–${high})` : `${score}`;
}

/* Escaping every interpolated value: report prose and model output both reach the DOM. */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

/* ---------- setup ---------- */

async function loadConfig() {
  serverConfig = await (await fetch("/api/config")).json();

  const sort = el("sort");
  sort.innerHTML = serverConfig.sort_keys
    .map((key) => `<option value="${esc(key)}">${esc(key)}</option>`).join("");

  const models = el("review-model");
  models.innerHTML = serverConfig.models
    .map((name) => `<option value="${esc(name)}"${name === serverConfig.default_model
      ? " selected" : ""}>${esc(name)}</option>`).join("");

  // Prefilled from this machine's captured delivery location rather than a hardcoded zip, so a
  // second operator does not silently search against someone else's city. Blank means nothing has
  // been captured yet — run Chrome login.
  if (serverConfig.default_zip) el("zip_code").value = serverConfig.default_zip;
}

async function loadTaxonomy() {
  const response = await fetch("/api/taxonomy");
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    formError(`${body.error || "taxonomy unavailable"} ${body.hint || ""}`);
    return;
  }
  taxonomy = await response.json();

  const makeSelect = el("make");
  makeSelect.innerHTML = '<option value="">any make</option>' + taxonomy.makes
    .map((make) => `<option value="${esc(make.name)}">${esc(make.name)} (${count(make.count)})</option>`)
    .join("");

  const years = taxonomy.bounds.year || [2009, 2027];
  const options = ['<option value="">any</option>'];
  for (let year = years[1]; year >= years[0]; year -= 1) {
    options.push(`<option value="${year}">${year}</option>`);
  }
  el("year_min").innerHTML = options.join("");
  el("year_max").innerHTML = options.join("");

  const price = taxonomy.bounds.price;
  const mileage = taxonomy.bounds.mileage;
  if (price) {
    el("max_price").min = price[0];
    text(el("price-bounds"), `Current inventory runs ${money(price[0])}–${money(price[1])}.`);
  }
  if (mileage) {
    text(el("mileage-bounds"),
      `Current inventory runs ${count(mileage[0])}–${count(mileage[1])} miles.`);
  }
}

el("make").addEventListener("change", () => {
  const modelSelect = el("model");
  const make = taxonomy?.makes.find((entry) => entry.name === el("make").value);
  if (!make) {
    modelSelect.innerHTML = '<option value="">choose a make first</option>';
    modelSelect.disabled = true;
    return;
  }
  modelSelect.disabled = false;
  modelSelect.innerHTML = '<option value="">any model</option>' + make.models
    .map((model) => `<option value="${esc(model.name)}">${esc(model.name)} (${count(model.count)})</option>`)
    .join("");
});

/* Invariant 6 is only guaranteed when both anchors are set, so say so before the run. */
function refreshAnchorWarning() {
  const both = el("max_price").value.trim() && el("max_miles").value.trim();
  show(el("anchor-warning"), !both);
}
el("max_price").addEventListener("input", refreshAnchorWarning);
el("max_miles").addEventListener("input", refreshAnchorWarning);

/* ---------- actions ---------- */

function formError(message) {
  const node = el("form-error");
  text(node, message || "");
  show(node, Boolean(message));
}

function formPayload() {
  const form = el("run-form");
  const payload = {};
  for (const element of form.elements) {
    if (!element.name) continue;
    payload[element.name] = element.type === "checkbox" ? element.checked : element.value;
  }
  return payload;
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const payload = await response.json().catch(() => ({}));
  return { ok: response.ok, payload };
}

el("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  formError("");
  const { ok, payload } = await post("/api/run", formPayload());
  if (!ok) formError(`${payload.error || "could not start"} ${payload.hint || ""}`);
});

el("btn-cancel").addEventListener("click", async () => {
  const { payload } = await post("/api/cancel");
  if (payload.note) formError(payload.note);
});

el("btn-login").addEventListener("click", async () => {
  formError("");
  const { ok, payload } = await post("/api/login");
  if (!ok) formError(payload.error || "could not start login");
});

el("btn-login-done").addEventListener("click", () => post("/api/login/done"));

el("btn-taxonomy").addEventListener("click", async () => {
  formError("");
  const { ok, payload } = await post("/api/taxonomy/refresh");
  if (!ok) formError(payload.error || "could not refresh");
});

el("btn-review").addEventListener("click", async () => {
  const { ok, payload } = await post("/api/review", {
    backend: "claude",
    model: el("review-model").value,
    count: serverConfig?.default_review_count,
  });
  if (!ok) formError(payload.error || "could not start review");
});

/* ---------- paste dialog ---------- */

function openPaste(vin, label) {
  pasteVin = vin;
  text(el("paste-title"), `Paste report — ${label}`);
  text(el("paste-hint"), `VIN ${vin}. Minimum ${count(serverConfig?.min_report_chars || 1500)} characters.`);
  el("paste-text").value = "";
  show(el("paste-error"), false);
  el("paste-dialog").showModal();
}

el("btn-paste-cancel").addEventListener("click", () => el("paste-dialog").close());

el("btn-paste-submit").addEventListener("click", async () => {
  const errorNode = el("paste-error");
  const button = el("btn-paste-submit");
  button.disabled = true;
  const { ok, payload } = await post("/api/ingest", {
    vin: pasteVin,
    text: el("paste-text").value,
  });
  button.disabled = false;

  if (!ok) {
    text(errorNode, `${payload.error || "could not add"} ${payload.hint || ""}`);
    show(errorNode, true);
    return;
  }
  el("paste-dialog").close();
  refresh();
});

/* ---------- rendering ---------- */

function renderStatus(state) {
  const pill = el("status-pill");
  pill.className = `pill pill-${state.status}`;
  text(pill, state.aborted && state.status === "done" ? "cancelled" : state.status);

  const running = state.status === "running";
  el("btn-run").disabled = running || state.status === "login";
  el("btn-cancel").disabled = !running;
  el("btn-login").disabled = running || state.status === "login";
  el("btn-taxonomy").disabled = running || state.status === "login";

  show(el("login-panel"), state.status === "login");
  if (state.status === "login") {
    const steps = state.events.filter((event) => event.kind === "login");
    el("login-steps").innerHTML = steps.map((step) => `<li>${esc(step.text)}</li>`).join("");
  }
}

function renderProgress(state) {
  const active = state.status !== "idle" && state.stage && state.stage.n;
  show(el("progress-panel"), Boolean(active) || state.events.length > 0);
  if (!active) return;

  const names = { search: "Searching inventory", autocheck: "AutoCheck reports",
                  carfax: "Carfax reports", scoring: "Scoring" };
  text(el("stage-label"),
    `Stage ${state.stage.n}/${state.stage.of} — ${names[state.stage.name] || state.stage.name}` +
    (state.stage.skipped ? " (skipped)" : ""));

  const { i, of } = state.progress || {};
  const total = of || state.stage.total;
  text(el("progress-counts"), i && total ? `${i} of ${total}` : "");
  const fraction = i && total
    ? i / total
    : (state.stage.n - (state.status === "done" ? 0 : 1)) / state.stage.of;
  el("bar-fill").style.width = `${Math.max(0, Math.min(1, fraction)) * 100}%`;

  const log = el("log");
  log.textContent = state.events
    .map((event) => event.text ?? event.message ?? `[${event.kind}]`)
    .join("\n");
  if (el("log-details").open) log.scrollTop = log.scrollHeight;
}

function renderError(state) {
  show(el("error-panel"), Boolean(state.error));
  if (!state.error) return;
  text(el("error-title"), state.error.kind.replace(/_/g, " "));
  text(el("error-message"), state.error.message);
  text(el("error-hint"), state.error.hint || "");
}

function renderChallenge(state) {
  show(el("challenge-panel"), Boolean(state.challenge));
  if (state.challenge) {
    text(el("challenge-label"),
      `${state.challenge.label}\nWaiting up to ${state.challenge.timeout_s}s.`);
  }
}

/* Renders one held-out car. `actionable` cars get the paste button prominently; the rest are a
   reference list, because their remedy is one setting, not a manual step per car. */
function heldOutCard(row, actionable) {
  const missing = row.missing_decision_fields.join(", ") || "none";
  const pasteButton = `<button type="button" class="${actionable ? "primary" : ""}"
      data-paste-vin="${esc(row.vin)}" data-paste-label="${esc(row.label)}">Paste report…</button>`;
  return `<div class="card">
    <div class="card-title">${esc(row.label)}</div>
    <div class="card-meta">
      VIN ${esc(row.vin)} · ${money(row.landed_price)} · ${count(row.mileage)} mi<br>
      Missing: <code>${esc(missing)}</code>
    </div>
    ${actionable ? `<div class="url">${esc(row.carfax_url)}</div>` : ""}
    <div class="actions">
      <a href="${esc(row.carfax_url)}" target="_blank" rel="noopener">
        <button type="button">Open Carfax</button></a>
      <a href="${esc(row.listing_url)}" target="_blank" rel="noopener">
        <button type="button">Listing</button></a>
      ${pasteButton}
    </div>
  </div>`;
}

function wirePasteButtons(container) {
  for (const button of container.querySelectorAll("[data-paste-vin]")) {
    button.addEventListener("click", () =>
      openPaste(button.dataset.pasteVin, button.dataset.pasteLabel));
  }
}

/* Two states were previously lumped under "Needs your help", which is how a run where nothing
   needed help announced that 27 cars did. They are genuinely different:
     - Carfax attempted and not obtained -> you can paste it. Actionable, per car.
     - never reached the Carfax shortlist -> raise one number and re-run. Not per car at all. */
function renderHelp(state) {
  const rows = state.needs_carfax || [];
  const actionable = rows.filter((row) => row.remedy !== "raise_top_n");
  const deferred = rows.filter((row) => row.remedy === "raise_top_n");

  show(el("help-panel"), actionable.length > 0);
  text(el("help-count"), actionable.length ? `— ${actionable.length}` : "");
  el("help-list").innerHTML = actionable.map((row) => heldOutCard(row, true)).join("");
  wirePasteButtons(el("help-list"));

  show(el("deferred-panel"), deferred.length > 0);
  text(el("deferred-count"), deferred.length ? `— ${deferred.length}` : "");
  if (deferred.length) {
    const topN = state.manifest ? state.manifest.counters.shortlisted : null;
    el("deferred-why").innerHTML =
      `Carfax ran for the top ${topN ?? "N"} by provisional score, so these ${deferred.length}
       have AutoCheck only and cannot be ranked — AutoCheck alone can never establish structural
       damage or airbag deployment. <strong>Nothing is required of you.</strong> To include them,
       raise <em>Carfax for top N</em> and re-run; Carfax allows roughly 6 per session before a
       puzzle, so expect to solve one. You can also paste any single report by hand.`;
    el("deferred-list").innerHTML = deferred.map((row) => heldOutCard(row, false)).join("");
    wirePasteButtons(el("deferred-list"));
  }
}

function renderRanked(state) {
  const rows = state.ranked || [];
  show(el("ranked-panel"), Boolean(state.manifest));
  text(el("ranked-count"), rows.length ? `— ${rows.length} with both reports` : "— none yet");
  el("btn-review").disabled = rows.length === 0 || state.review_running;
  text(el("btn-review"), state.review_running ? "Reviewing…" : "Review reports with Claude");

  const pick = state.review && !state.review.error ? state.review.pick_vin : null;
  el("ranked-table").querySelector("tbody").innerHTML = rows.map((vehicle, index) => {
    const listing = vehicle.listing;
    const history = vehicle.history || {};
    return `<tr class="${listing.vin === pick ? "is-pick" : ""}">
      <td class="num">${index + 1}</td>
      <td class="num">${num(vehicle.score) ?? "—"}</td>
      <td>${esc(listing.label)}<br><span class="muted" style="font-size:11px">${esc(listing.vin)}</span></td>
      <td class="num">${money(listing.landed_price)}</td>
      <td class="num">${count(listing.mileage)}</td>
      <td class="num">${vehicle.cost_per_remaining_mile === null ? "—"
        : "$" + vehicle.cost_per_remaining_mile.toFixed(3)}</td>
      <td class="num">${num(history.owner_count) ?? "?"}</td>
      <td>${accidentTag(history)}</td>
      <td class="num">${autocheckCell(history)}</td>
      <td><a href="${esc(listing.listing_url)}" target="_blank" rel="noopener">view</a></td>
    </tr>`;
  }).join("");
}

function renderReview(state) {
  const review = state.review;
  show(el("review-panel"), Boolean(review));
  if (!review) return;

  if (review.error) {
    el("review-body").innerHTML =
      `<div class="alert alert-warn">Review unavailable: ${esc(review.error)}</div>
       <p class="hint">The ranking above is unaffected.</p>`;
    return;
  }

  const byVin = {};
  for (const vehicle of state.ranked || []) byVin[vehicle.listing.vin] = vehicle.listing.label;
  const name = (vin) => byVin[vin] || vin;

  const parts = [];
  if (review.pick_vin) {
    parts.push(`<div class="pick"><strong>★ Pick: ${esc(name(review.pick_vin))}</strong>
      <div>${esc(review.pick_reason)}</div></div>`);
  }

  if (review.findings.length) {
    parts.push("<h3 style='font-size:13px;margin:0 0 8px'>Findings from the report text</h3>");
    parts.push(review.findings.map((finding) => {
      /* Whether the quote was located in the report is checked server-side. A claim whose quote
         cannot be found is still shown — it may well be true — but never as if the report said it. */
      let quote;
      if (!finding.evidence) {
        quote = '<p class="hint">No quote supplied — unverified.</p>';
      } else if (finding.evidence_supported === false) {
        quote = `<blockquote>${esc(finding.evidence)}</blockquote>
          <p class="hint" style="color:var(--warn)">⚠ This quote was not found in the report —
          paraphrased or stitched together. Check it against the report yourself before trusting
          the claim.</p>`;
      } else {
        quote = `<blockquote>${esc(finding.evidence)}</blockquote>`;
      }
      return `<div class="finding sev-${esc(finding.severity)}">
        <div class="claim"><strong>${esc(name(finding.vin))}</strong> — ${esc(finding.claim)}</div>
        ${quote}
      </div>`;
    }).join(""));
  }

  if (review.conflict_resolutions.length) {
    parts.push("<h3 style='font-size:13px;margin:14px 0 8px'>Vendor disagreements</h3>");
    parts.push(review.conflict_resolutions.map((entry) => `
      <div class="finding sev-warn">
        <div class="claim"><strong>${esc(name(entry.vin))}</strong> —
          <code>${esc(entry.field)}</code>: ${esc(entry.resolution)}</div>
        <div class="hint">${esc(entry.reasoning)}</div>
      </div>`).join(""));
  }

  /* Shown, not hidden: if the reviewer tried to talk about a car outside its input, that is worth
     seeing rather than quietly filtering. */
  if (review.dropped && review.dropped.length) {
    parts.push(`<div class="alert alert-warn">Discarded from the reply:
      <ul>${review.dropped.map((note) => `<li>${esc(note)}</li>`).join("")}</ul></div>`);
  }

  if (review.unsupported_findings) {
    parts.push(`<div class="alert alert-warn">${review.unsupported_findings} of
      ${review.findings.length} findings carry a quote that could not be located in the report.
      They are marked above.</div>`);
  }
  parts.push(`<p class="hint">Reviewed ${review.reviewed_vins.length} vehicle(s) via
    ${esc(review.backend)} (${esc(review.model)}). Scores and ordering above are untouched.</p>`);
  el("review-body").innerHTML = parts.join("");
}

function renderDisqualified(state) {
  const rows = state.disqualified || [];
  show(el("dq-panel"), rows.length > 0);
  text(el("dq-count"), rows.length ? `— ${rows.length}` : "");
  el("dq-list").innerHTML = rows.map((vehicle) => `
    <div class="card">
      <div class="card-title">${esc(vehicle.listing.label)}
        <span class="tag tag-bad">disqualified</span></div>
      <div class="card-meta">VIN ${esc(vehicle.listing.vin)} ·
        ${money(vehicle.listing.landed_price)} · ${count(vehicle.listing.mileage)} mi</div>
      <div>${vehicle.disqualifiers.map((reason) =>
        `<span class="tag tag-bad">${esc(reason)}</span>`).join(" ")}</div>
    </div>`).join("");
}

function renderManifest(state) {
  show(el("manifest-panel"), Boolean(state.manifest));
  if (!state.manifest) return;

  if (!state.manifest.reconciled) {
    /* --search-only / --no-history skip the history stages by request, so the reconciliation
       checks do not apply and claiming a fault would be wrong. */
    el("reconciliation").innerHTML =
      '<div class="alert alert-warn">History stages were skipped by request, so the ' +
      "reconciliation checks do not apply to this run.</div>";
  } else if (state.manifest.reconciliation_problems.length) {
    el("reconciliation").innerHTML =
      `<div class="alert alert-bad"><strong>This run did not reconcile.</strong>
        <ul>${state.manifest.reconciliation_problems.map((problem) =>
          `<li>${esc(problem)}</li>`).join("")}</ul></div>`;
  } else {
    el("reconciliation").innerHTML =
      '<div class="alert alert-warn" style="background:var(--good-bg);color:var(--good)">' +
      "Counts reconcile.</div>";
  }

  el("warnings").innerHTML = (state.warnings || []).length
    ? `<div class="alert alert-warn"><ul>${state.warnings.map((warning) =>
        `<li>${esc(warning)}</li>`).join("")}</ul></div>`
    : "";

  text(el("manifest-lines"), state.manifest.lines.join("\n"));
  text(el("report-body"), state.report_body || "");
  text(el("report-path"), state.report_path ? `Report written to ${state.report_path}` : "");
}

/* ---------- poll ---------- */

async function refresh() {
  let state;
  try {
    state = await (await fetch("/api/state")).json();
  } catch {
    return; // server restarting or stopped; the next tick retries
  }

  renderStatus(state);
  renderProgress(state);
  renderError(state);
  renderChallenge(state);
  renderHelp(state);
  renderRanked(state);
  renderReview(state);
  renderDisqualified(state);
  renderManifest(state);

  // A finished paste is worth an explicit line, since the table change can be subtle.
  const ingested = state.ingested || [];
  if (ingested.length > lastIngestCount) {
    const latest = ingested[ingested.length - 1];
    formError(latest.now_ranked
      ? `${latest.label}: ${latest.vendor} added — now ranked, score ${latest.score}.`
      : `${latest.label}: ${latest.vendor} added — still held out (missing ${
          latest.missing_decision_fields.join(", ") || "none"}).`);
  }
  lastIngestCount = ingested.length;
}

(async function start() {
  await loadConfig();
  await loadTaxonomy();
  refreshAnchorWarning();
  await refresh();
  setInterval(refresh, POLL_MS);
})();
