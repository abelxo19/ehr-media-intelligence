/* EHR Media Intelligence - vanilla JS front end (Fetch API, no framework). */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const els = {
    form: $("#search-form"),
    input: $("#search-input"),
    type: $("#filter-type"),
    from: $("#filter-from"),
    to: $("#filter-to"),
    clear: $("#clear-filters"),
    filterError: $("#filter-error"),
    results: $("#results"),
    status: $("#results-status"),
    heading: $("#results-heading"),
    pill: $("#status-pill"),
    drawerRoot: $("#drawer-root"),
    drawer: $("#drawer"),
    drawerTitle: $("#drawer-title"),
    drawerSubtitle: $("#drawer-subtitle"),
    drawerBody: $("#drawer-body"),
    drawerClose: $("#drawer-close"),
    backdrop: $("#drawer-backdrop"),
  };

  const DEBOUNCE_MS = 300;
  const MIN_CHARS = 2;
  let debounceTimer = null;
  let inflight = null;
  let lastFocused = null;
  let patientsCache = [];

  // ------------------------------------------------------------------ helpers
  const escapeHtml = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  const fmtDate = (iso) => {
    if (!iso) return "Undated";
    const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
    return isNaN(d) ? iso : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  };

  const ageFrom = (birth) => {
    if (!birth) return null;
    const b = new Date(birth + "T00:00:00");
    const now = new Date();
    let age = now.getFullYear() - b.getFullYear();
    if (now.getMonth() < b.getMonth() || (now.getMonth() === b.getMonth() && now.getDate() < b.getDate())) age--;
    return age;
  };

  /** Highlight query words inside already-escaped text. */
  const highlight = (text, query) => {
    let html = escapeHtml(text);
    const words = (query || "").toLowerCase().split(/\W+/).filter((w) => w.length > 2);
    if (!words.length) return html;
    const re = new RegExp(`\\b(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
    return html.replace(re, "<mark>$1</mark>");
  };

  const TYPE_BADGE = {
    DiagnosticReport: { label: "DiagnosticReport", cls: "bg-sky-50 text-sky-700 ring-sky-200" },
    DocumentReference: { label: "DocumentReference", cls: "bg-violet-50 text-violet-700 ring-violet-200" },
    Patient: { label: "AI summary", cls: "bg-brand-50 text-brand-700 ring-brand-100" },
    Encounter: { label: "Encounter", cls: "bg-slate-50 text-slate-600 ring-slate-200" },
    Observation: { label: "Observation", cls: "bg-amber-50 text-amber-700 ring-amber-200" },
  };
  const badge = (type) => {
    const b = TYPE_BADGE[type] || { label: type, cls: "bg-slate-50 text-slate-600 ring-slate-200" };
    return `<span class="inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${b.cls}">${escapeHtml(b.label)}</span>`;
  };

  const scoreBar = (score) => {
    const pct = Math.max(0, Math.min(100, Math.round(score * 100)));
    return `<div class="flex items-center gap-2" title="Cosine similarity ${score.toFixed(3)}">
      <div class="h-1.5 w-16 overflow-hidden rounded-full bg-slate-100" aria-hidden="true"><div class="h-full rounded-full bg-brand-500" style="width:${pct}%"></div></div>
      <span class="text-xs tabular-nums text-slate-600"><span class="sr-only">Relevance </span>${pct}%</span></div>`;
  };

  const setBusy = (busy) => els.results.setAttribute("aria-busy", busy ? "true" : "false");

  async function api(path, options = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch (_) { /* ignore */ }
      const err = new Error(detail || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  // ------------------------------------------------------------------ rendering
  function renderSkeleton(n = 3) {
    els.results.innerHTML = `<ul class="space-y-3" aria-hidden="true">${Array.from({ length: n }, () => `
      <li class="rounded-xl border border-slate-200 bg-white p-4">
        <div class="flex justify-between"><div class="skeleton h-4 w-48"></div><div class="skeleton h-4 w-20"></div></div>
        <div class="skeleton mt-3 h-3 w-full"></div><div class="skeleton mt-2 h-3 w-5/6"></div>
        <div class="skeleton mt-4 h-3 w-2/3"></div>
      </li>`).join("")}</ul>`;
  }

  function renderMessage(title, body, tone = "neutral") {
    const toneCls = tone === "error" ? "border-red-200 bg-red-50 text-red-800" : "border-dashed border-slate-300 bg-white text-slate-600";
    els.results.innerHTML = `<div class="rounded-xl border ${toneCls} px-6 py-10 text-center">
      <p class="font-medium">${escapeHtml(title)}</p><p class="mt-1 text-sm opacity-80">${body}</p></div>`;
  }

  function renderResults(data) {
    els.heading.textContent = "Results";
    if (!data.results.length) {
      const hasFilters = els.type.value || els.from.value || els.to.value;
      renderMessage("No matching records",
        hasFilters ? `Nothing matched "${escapeHtml(data.query)}" with the current filters. <button type="button" class="font-medium text-brand-700 underline" data-action="clear-filters">Clear filters</button> and try again.`
                   : `Nothing matched "${escapeHtml(data.query)}". Try a broader clinical term.`);
      els.status.textContent = `No results (${data.took_ms} ms)`;
      return;
    }
    els.status.textContent = `Top ${data.results.length} of ${data.candidates} matching records · ${data.took_ms} ms`;
    els.results.innerHTML = `<ol class="space-y-3" aria-label="Search results">${data.results.map((r) => `
      <li>
        <article class="result-card group cursor-pointer rounded-xl border border-slate-200 bg-white p-4 shadow-sm transition hover:border-brand-500 hover:shadow focus:border-brand-600"
                 tabindex="0" role="button" data-patient="${escapeHtml(r.patient_id)}" data-record="${escapeHtml(r.fhir_id)}"
                 aria-label="Result ${r.rank}: ${escapeHtml(r.title)} for ${escapeHtml(r.patient_name)}, MRN ${escapeHtml(r.mrn)}, ${escapeHtml(fmtDate(r.record_date))}, relevance ${Math.round(r.score * 100)} percent. Press Enter for patient details.">
          <div class="flex flex-wrap items-start justify-between gap-2">
            <div class="min-w-0">
              <p class="font-semibold text-slate-900">${escapeHtml(r.patient_name)}
                <span class="ml-1 font-mono text-xs font-normal text-slate-500">MRN ${escapeHtml(r.mrn)}</span></p>
              <p class="mt-0.5 text-sm text-slate-600"><span class="font-medium text-slate-800">${escapeHtml(r.title)}</span> · <time datetime="${escapeHtml(r.record_date || "")}">${escapeHtml(fmtDate(r.record_date))}</time></p>
            </div>
            <div class="flex items-center gap-3">${badge(r.resource_type)}${scoreBar(r.score)}</div>
          </div>
          <p class="mt-3 text-sm leading-relaxed text-slate-700">${highlight(r.snippet, data.query)}</p>
          ${r.summary_snippet ? `<div class="mt-3 rounded-lg bg-slate-50 px-3 py-2 text-xs leading-relaxed text-slate-600">
            <span class="font-semibold text-brand-700">AI summary</span> · ${escapeHtml(r.summary_snippet)}</div>` : ""}
        </article>
      </li>`).join("")}</ol>`;
  }

  function renderPatientGrid(patients) {
    els.heading.textContent = "Patients";
    els.status.textContent = `${patients.length} patients · search or pick one to open the summary`;
    if (!patients.length) {
      renderMessage("No patients loaded", "Run <code>python -m app.pipeline</code> to ingest the sample data.");
      return;
    }
    els.results.innerHTML = `<ul class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3" aria-label="All patients">${patients.map((p) => `
      <li><article class="result-card h-full cursor-pointer rounded-xl border border-slate-200 bg-white p-4 shadow-sm transition hover:border-brand-500 hover:shadow"
            tabindex="0" role="button" data-patient="${escapeHtml(p.patient_id)}"
            aria-label="${escapeHtml(p.display_name)}, MRN ${escapeHtml(p.mrn)}, ${p.record_count} records. Press Enter for details.">
        <div class="flex items-start justify-between gap-2">
          <p class="font-semibold text-slate-900">${escapeHtml(p.display_name)}</p>
          ${p.flags.length ? `<span class="rounded-md bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-800 ring-1 ring-inset ring-amber-200" title="${escapeHtml(p.flags.join(", "))}">Review ID</span>` : ""}
        </div>
        <p class="mt-1 font-mono text-xs text-slate-500">MRN ${escapeHtml(p.mrn)}</p>
        <p class="mt-2 text-sm text-slate-600">${p.birth_date ? `${ageFrom(p.birth_date)} y` : "Age unknown"} · ${escapeHtml(p.gender)} · ${p.record_count} records</p>
        <p class="mt-1 text-xs text-slate-500">Last record ${escapeHtml(fmtDate(p.last_record_date))}</p>
      </article></li>`).join("")}</ul>`;
  }

  // ------------------------------------------------------------------ search
  function validateFilters() {
    const bad = els.from.value && els.to.value && els.from.value > els.to.value;
    els.filterError.textContent = bad ? "“From” date must be on or before “To” date." : "";
    els.filterError.classList.toggle("hidden", !bad);
    els.from.setAttribute("aria-invalid", bad ? "true" : "false");
    els.to.setAttribute("aria-invalid", bad ? "true" : "false");
    return !bad;
  }

  async function runSearch() {
    const query = els.input.value.trim();
    if (!validateFilters()) return;
    if (query.length < MIN_CHARS) {
      if (inflight) inflight.abort();
      renderPatientGrid(patientsCache);
      return;
    }
    const params = new URLSearchParams();
    if (els.type.value) params.append("resource_type", els.type.value);
    if (els.from.value) params.set("date_from", els.from.value);
    if (els.to.value) params.set("date_to", els.to.value);

    if (inflight) inflight.abort();
    inflight = new AbortController();
    setBusy(true);
    els.status.textContent = "Searching…";
    renderSkeleton();
    try {
      const data = await api(`/search?${params}`, { method: "POST", body: JSON.stringify({ query }), signal: inflight.signal });
      renderResults(data);
    } catch (err) {
      if (err.name === "AbortError") return;
      els.status.textContent = "Search failed";
      renderMessage("Search failed", escapeHtml(err.message), "error");
    } finally {
      setBusy(false);
    }
  }

  const scheduleSearch = () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(runSearch, DEBOUNCE_MS);
  };

  // ------------------------------------------------------------------ drawer
  function openDrawer(patientId, recordId) {
    lastFocused = document.activeElement;
    els.drawerRoot.classList.remove("hidden");
    els.drawerRoot.setAttribute("aria-hidden", "false");
    document.body.classList.add("overflow-hidden");
    els.drawerTitle.textContent = "Loading…";
    els.drawerSubtitle.textContent = "";
    els.drawerBody.innerHTML = `<div class="space-y-3" aria-hidden="true"><div class="skeleton h-4 w-1/2"></div><div class="skeleton h-24 w-full"></div><div class="skeleton h-4 w-2/3"></div><div class="skeleton h-40 w-full"></div></div>`;
    els.drawer.focus();
    loadPatient(patientId, recordId);
  }

  function closeDrawer() {
    els.drawerRoot.classList.add("hidden");
    els.drawerRoot.setAttribute("aria-hidden", "true");
    document.body.classList.remove("overflow-hidden");
    if (lastFocused && document.contains(lastFocused)) lastFocused.focus();
  }

  const section = (title, inner) => `<section class="mt-5 first:mt-0"><h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">${title}</h3>${inner}</section>`;

  function renderSummary(s) {
    if (!s) return section("AI clinical summary", `<p class="text-sm text-slate-500">No summary generated yet.</p>`);
    const conf = { high: "bg-emerald-50 text-emerald-700 ring-emerald-200", medium: "bg-amber-50 text-amber-800 ring-amber-200", low: "bg-red-50 text-red-700 ring-red-200" }[s.confidence];
    return section("AI clinical summary", `
      <div class="rounded-xl border border-brand-100 bg-brand-50/40 p-4">
        <div class="mb-3 flex flex-wrap items-center gap-2 text-xs">
          <span class="rounded-md px-2 py-0.5 font-medium ring-1 ring-inset ${conf}">Confidence: ${escapeHtml(s.confidence)}</span>
          <span class="rounded-md bg-white px-2 py-0.5 text-slate-600 ring-1 ring-inset ring-slate-200">Grounding ${Math.round(s.grounding.score * 100)}% of ${s.grounding.checked} checks</span>
          <span class="rounded-md bg-white px-2 py-0.5 text-slate-600 ring-1 ring-inset ring-slate-200">${s.word_count} words</span>
          <span class="rounded-md bg-white px-2 py-0.5 font-mono text-slate-500 ring-1 ring-inset ring-slate-200">${escapeHtml(s.generator)}</span>
        </div>
        <dl class="space-y-3 text-sm">
          <div><dt class="font-semibold text-slate-900">Chief concern</dt><dd class="mt-0.5 text-slate-700">${escapeHtml(s.chief_concern)}</dd></div>
          <div><dt class="font-semibold text-slate-900">Key diagnoses</dt><dd class="mt-1 flex flex-wrap gap-1.5">${s.key_diagnoses.map((d) => `<span class="rounded-full bg-white px-2.5 py-0.5 text-xs text-slate-700 ring-1 ring-inset ring-slate-200">${escapeHtml(d)}</span>`).join("") || "<span class='text-slate-500'>None documented</span>"}</dd></div>
          <div><dt class="font-semibold text-slate-900">Recent media records</dt><dd class="mt-1"><ul class="space-y-1.5">${s.recent_media.map((m) => `
            <li class="text-slate-700"><span class="font-medium">${escapeHtml(m.type)}</span> <span class="text-xs text-slate-500">${escapeHtml(fmtDate(m.date))} · ${escapeHtml(m.record_id)}</span><br/><span class="text-sm">${escapeHtml(m.finding)}</span></li>`).join("") || "<li class='text-slate-500'>None</li>"}</ul></dd></div>
          <div><dt class="font-semibold text-red-800">Flagged anomalies</dt><dd class="mt-1"><ul class="space-y-1">${s.flagged_anomalies.map((a) => `
            <li class="flex gap-2 text-red-800"><span aria-hidden="true">&#9888;</span><span>${escapeHtml(a.finding)}${a.record_id ? ` <span class="text-xs text-red-600/80">(${escapeHtml(a.record_id)})</span>` : ""}</span></li>`).join("") || "<li class='text-slate-500'>None</li>"}</ul></dd></div>
          <div><dt class="font-semibold text-slate-900">Narrative</dt><dd class="mt-0.5 text-slate-700">${escapeHtml(s.narrative)}</dd></div>
        </dl>
        ${s.grounding.ungrounded.length ? `<p class="mt-3 text-xs text-amber-800">Not verified against source: ${s.grounding.ungrounded.map(escapeHtml).join("; ")}</p>` : ""}
        <p class="mt-4 rounded-lg bg-white px-3 py-2 text-xs text-slate-600 ring-1 ring-inset ring-slate-200" role="note"><strong>Disclaimer:</strong> ${escapeHtml(s.disclaimer)}</p>
      </div>`);
  }

  function renderResources(resources, patientId, highlightId) {
    const order = ["DocumentReference", "DiagnosticReport", "Observation", "Encounter", "Patient"];
    const groups = order.map((t) => [t, resources.filter((r) => r.resource_type === t)]).filter(([, l]) => l.length);
    return section(`Linked FHIR resources (${resources.length})`, groups.map(([type, list]) => `
      <details class="mb-2 rounded-lg border border-slate-200" ${type === "DocumentReference" || type === "DiagnosticReport" ? "open" : ""}>
        <summary class="cursor-pointer select-none px-3 py-2 text-sm font-medium text-slate-800">${badge(type)} <span class="ml-1 text-slate-500">${list.length}</span></summary>
        <ul class="divide-y divide-slate-100 border-t border-slate-100">${list.map((r) => `
          <li class="px-3 py-2 ${r.id === highlightId ? "bg-amber-50" : ""}">
            <div class="flex items-start justify-between gap-2">
              <div class="min-w-0 text-sm"><p class="text-slate-800">${escapeHtml(r.title || r.resource_type)}</p>
                <p class="text-xs text-slate-500">${escapeHtml(fmtDate(r.date))}${r.status ? ` · ${escapeHtml(r.status)}` : ""} · <span class="font-mono">${escapeHtml(r.id.slice(0, 8))}</span></p></div>
              <button type="button" class="view-json shrink-0 rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                      data-patient="${escapeHtml(patientId)}" data-resource="${escapeHtml(r.id)}" aria-expanded="false">View JSON</button>
            </div>
            <pre class="json-view mt-2 hidden max-h-72 overflow-auto rounded-md bg-slate-900 p-3 text-xs text-slate-100"></pre>
          </li>`).join("")}</ul>
      </details>`).join(""));
  }

  async function loadPatient(patientId, recordId) {
    try {
      const data = await api(`/patients/${encodeURIComponent(patientId)}`);
      const p = data.patient;
      const name = [p.given_name, p.family_name].filter(Boolean).join(" ");
      els.drawerTitle.textContent = name || "Unknown patient";
      const age = ageFrom(p.birth_date);
      els.drawerSubtitle.textContent = `MRN ${p.mrn} · ${p.birth_date ? `DOB ${p.birth_date} (${age} y)` : "DOB unknown"} · ${p.gender}`;

      // highlight the resource the user clicked on
      const highlightRes = recordId ? data.resources.find((r) => r.id === recordId) : null;
      const flags = p.flags.length ? `<div class="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900" role="note">
          <strong>Identity review needed:</strong> ${p.flags.map((f) => escapeHtml(f.replace(/_/g, " "))).join(", ")}.
          ${p.alternate_mrns.length ? `Alternate MRN(s): <span class="font-mono">${p.alternate_mrns.map(escapeHtml).join(", ")}</span>.` : ""}</div>` : "";
      const v = data.validation;
      const validation = `<p class="text-sm ${v.valid ? "text-emerald-700" : "text-red-700"}">${v.valid ? "&#10003; Bundle valid" : "&#10007; Bundle has errors"} against FHIR R4 schema and reference checks
        · ${v.issues.filter((i) => i.severity === "error").length} errors, ${v.issues.filter((i) => i.severity === "warning").length} warnings</p>
        <a class="mt-1 inline-block text-sm font-medium text-brand-700 underline" href="/patients/${encodeURIComponent(patientId)}/bundle" target="_blank" rel="noopener">Open FHIR Bundle JSON</a>`;
      els.drawerBody.innerHTML = flags + renderSummary(data.summary) + renderResources(data.resources, patientId, highlightRes && highlightRes.id) + section("Validation", validation);
    } catch (err) {
      els.drawerTitle.textContent = "Could not load patient";
      els.drawerBody.innerHTML = `<p class="text-sm text-red-700">${escapeHtml(err.message)}</p>`;
    }
  }

  async function toggleJson(btn) {
    const pre = btn.closest("li").querySelector(".json-view");
    const open = btn.getAttribute("aria-expanded") === "true";
    if (open) {
      pre.classList.add("hidden");
      btn.setAttribute("aria-expanded", "false");
      btn.textContent = "View JSON";
      return;
    }
    if (!pre.textContent) {
      pre.textContent = "Loading…";
      try {
        const res = await api(`/patients/${encodeURIComponent(btn.dataset.patient)}/resources/${encodeURIComponent(btn.dataset.resource)}`);
        pre.textContent = JSON.stringify(res, null, 2);
      } catch (err) {
        pre.textContent = `Error: ${err.message}`;
      }
    }
    pre.classList.remove("hidden");
    btn.setAttribute("aria-expanded", "true");
    btn.textContent = "Hide JSON";
  }

  // ------------------------------------------------------------------ events
  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    clearTimeout(debounceTimer);
    runSearch();
  });
  els.input.addEventListener("input", scheduleSearch);
  [els.type, els.from, els.to].forEach((el) => el.addEventListener("change", () => { clearTimeout(debounceTimer); runSearch(); }));
  const clearFilters = () => {
    els.type.value = ""; els.from.value = ""; els.to.value = "";
    runSearch();
  };
  els.clear.addEventListener("click", clearFilters);
  $$(".example").forEach((b) => b.addEventListener("click", () => { els.input.value = b.textContent; runSearch(); els.input.focus(); }));

  els.results.addEventListener("click", (e) => {
    if (e.target.closest("[data-action='clear-filters']")) return clearFilters();
    const card = e.target.closest(".result-card");
    if (card) openDrawer(card.dataset.patient, card.dataset.record);
  });
  els.results.addEventListener("keydown", (e) => {
    const card = e.target.closest(".result-card");
    if (!card) return;
    const cards = $$(".result-card", els.results);
    const i = cards.indexOf(card);
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDrawer(card.dataset.patient, card.dataset.record); }
    else if (e.key === "ArrowDown" || e.key === "ArrowRight") { e.preventDefault(); cards[Math.min(i + 1, cards.length - 1)].focus(); }
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") { e.preventDefault(); i === 0 ? els.input.focus() : cards[i - 1].focus(); }
    else if (e.key === "Home") { e.preventDefault(); cards[0].focus(); }
    else if (e.key === "End") { e.preventDefault(); cards[cards.length - 1].focus(); }
  });
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      const first = $(".result-card", els.results);
      if (first) { e.preventDefault(); first.focus(); }
    }
  });

  els.drawerBody.addEventListener("click", (e) => {
    const btn = e.target.closest(".view-json");
    if (btn) toggleJson(btn);
  });
  els.drawerClose.addEventListener("click", closeDrawer);
  els.backdrop.addEventListener("click", closeDrawer);

  document.addEventListener("keydown", (e) => {
    const drawerOpen = !els.drawerRoot.classList.contains("hidden");
    if (drawerOpen && e.key === "Escape") { e.preventDefault(); closeDrawer(); return; }
    if (drawerOpen && e.key === "Tab") {
      // focus trap inside the dialog
      const focusable = $$("button, [href], input, select, summary, [tabindex]:not([tabindex='-1'])", els.drawer).filter((el) => !el.closest(".hidden") && el.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === els.drawer)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      return;
    }
    if (!drawerOpen && e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      e.preventDefault();
      els.input.focus();
    }
  });

  // ------------------------------------------------------------------ init
  async function init() {
    renderSkeleton(6);
    try {
      const [health, patients] = await Promise.all([api("/health"), api("/patients")]);
      patientsCache = patients;
      const gen = health.last_run?.summaries?.generator || "";
      els.pill.textContent = `${health.patients} patients · ${health.indexed_documents} indexed · ${gen.startsWith("claude") ? "Claude summaries" : "offline summaries"}`;
      els.pill.className = "rounded-full bg-emerald-50 px-3 py-1 text-emerald-700";
      renderPatientGrid(patients);
    } catch (err) {
      els.pill.textContent = "API unavailable";
      els.pill.className = "rounded-full bg-red-50 px-3 py-1 text-red-700";
      renderMessage("Cannot reach the API", escapeHtml(err.message), "error");
    }
  }
  init();
})();
