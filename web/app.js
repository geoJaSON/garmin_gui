"use strict";

// --- tiny API helper -----------------------------------------------------
// cache:"no-store" so the browser never replays a stale API response
// (e.g. a pre-login /api/me) regardless of what's already in its cache.
const api = (path, opts) =>
  fetch(path, Object.assign({ credentials: "same-origin", cache: "no-store" }, opts));
const $ = (id) => document.getElementById(id);

// --- map -----------------------------------------------------------------
const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: "© OpenStreetMap contributors",
      },
    },
    layers: [{ id: "osm", type: "raster", source: "osm" }],
  },
  center: [0, 20],
  zoom: 2,
});
map.addControl(new maplibregl.NavigationControl(), "bottom-right");

// rsd_name -> {cog} for completed mosaics
let runsByRsd = {};

function bboxOf(geojson) {
  let minX = 180, minY = 90, maxX = -180, maxY = -90;
  const eat = (c) => { if (c[0] < minX) minX = c[0]; if (c[0] > maxX) maxX = c[0];
                        if (c[1] < minY) minY = c[1]; if (c[1] > maxY) maxY = c[1]; };
  for (const f of geojson.features || []) {
    const g = f.geometry; if (!g) continue;
    const coords = g.type === "LineString" ? g.coordinates
                 : g.type === "MultiLineString" ? g.coordinates.flat() : [];
    coords.forEach(eat);
  }
  return maxX >= minX ? [[minX, minY], [maxX, maxY]] : null;
}

async function loadTracks() {
  const [tracks, runs] = await Promise.all([
    api("/api/tracks").then((r) => r.json()),
    api("/api/runs").then((r) => r.json()),
  ]);
  runsByRsd = {};
  for (const run of runs) if (run.rsd_name) runsByRsd[run.rsd_name] = run;

  if (map.getSource("tracks")) map.getSource("tracks").setData(tracks);
  else {
    map.addSource("tracks", { type: "geojson", data: tracks });
    map.addLayer({
      id: "tracks-line", type: "line", source: "tracks",
      paint: { "line-color": "#2563eb", "line-width": 3 },
    });
    map.addLayer({
      id: "tracks-sel", type: "line", source: "tracks",
      filter: ["==", ["get", "file_name"], "__none__"],
      paint: { "line-color": "#f59e0b", "line-width": 5 },
    });
    map.on("click", "tracks-line", (e) => selectTrack(e.features[0]));
    map.on("mouseenter", "tracks-line", () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", "tracks-line", () => (map.getCanvas().style.cursor = ""));
  }

  const bb = bboxOf(tracks);
  const n = (tracks.features || []).length;
  $("status").textContent = n ? `${n} track(s)` : "no tracks yet — run the inventory";
  if (bb) map.fitBounds(bb, { padding: 60, duration: 0 });
}

// --- track selection -> overlay its mosaic COG ---------------------------
async function selectTrack(feature) {
  const p = feature.properties || {};
  const name = p.file_name || "(unknown)";
  if (combineMode) { toggleSelect(name); return; }
  map.setFilter("tracks-sel", ["==", ["get", "file_name"], name]);

  const run = runsByRsd[name];
  const rows = [
    ["File", name],
    ["Source meta", p.source_meta || p.source_meta_name || "—"],
    ["Points", p.point_count || p.points || "—"],
  ];
  let extra;
  if (run) {
    await showCog(run.cog);
    extra = `<span class="badge">mosaic available</span>`;
  } else {
    removeCog();
    extra = `<p class="muted">No mosaic generated for this track yet.</p>`;
  }
  $("panel-title").textContent = name;
  $("panel-body").innerHTML =
    "<dl>" + rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("") + "</dl>" + extra;
  $("panel").hidden = false;
}

async function showCog(cogPath) {
  // Do NOT trust tilejson's `tiles` URLs — TiTiler's url_for has emitted a
  // path (/tiles/{tms}/...) that doesn't match its real route
  // (/tiles/tiles/{tms}/...), 404ing every tile. We build the known-good
  // endpoint ourselves and use tilejson only for bounds/zoom. Cache-bust
  // so a stale cached tilejson can't feed old bounds.
  const enc = encodeURIComponent(cogPath);
  const tj = await api(
    `/tiles/WebMercatorQuad/tilejson.json?url=${enc}&_=${Date.now()}`
  ).then((r) => r.json());

  const tileUrl =
    `${location.origin}/tiles/tiles/WebMercatorQuad/{z}/{x}/{y}` +
    `?url=${enc}&tilesize=512`;

  removeCog();
  map.addSource("cog", {
    type: "raster",
    tiles: [tileUrl],
    tileSize: 256,
    bounds: tj.bounds,
    minzoom: tj.minzoom || 0,
    maxzoom: tj.maxzoom || 24,
  });
  map.addLayer({ id: "cog", type: "raster", source: "cog" }, "tracks-line");
  if (tj.bounds) {
    const [w, s, e, n] = tj.bounds;
    map.fitBounds([[w, s], [e, n]], { padding: 40 });
  }
}
function removeCog() {
  if (map.getLayer("cog")) map.removeLayer("cog");
  if (map.getSource("cog")) map.removeSource("cog");
}

$("panel-close").onclick = () => {
  $("panel").hidden = true;
  removeCog();
  map.setFilter("tracks-sel", ["==", ["get", "file_name"], "__none__"]);
};

// --- auth ----------------------------------------------------------------
async function boot() {
  const me = await api("/api/me").then((r) => r.json());
  if (!me.authed) { $("login").hidden = false; return; }
  $("login").hidden = true;
  $("logout").hidden = false;
  $("new-run").hidden = false;
  $("combine-toggle").hidden = false;
  $("layers-toggle").hidden = false;
  $("data-toggle").hidden = false;
  const init = () => { loadTracks(); loadLayers(); };
  if (map.loaded()) init();
  else map.on("load", init);
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await api("/api/login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ password: $("password").value }),
  });
  if (r.ok) { $("login-error").hidden = true; boot(); }
  else $("login-error").hidden = false;
});

$("logout").onclick = async () => {
  await api("/api/logout", { method: "POST" });
  location.reload();
};

// --- Phase 3b: new-run drawer -------------------------------------------
let cfgFields = [];

async function openDrawer() {
  $("drawer").hidden = false;
  const [rsds, cfg] = await Promise.all([
    api("/api/rsd").then((r) => r.json()),
    api("/api/config/mosaic").then((r) => r.json()),
  ]);
  const sel = $("rsd-select");
  sel.innerHTML = rsds.length
    ? rsds.map((r) => `<option value="${r.path}">${r.name}</option>`).join("")
    : `<option value="">— none uploaded —</option>`;
  cfgFields = cfg.fields;
  renderParams();
}

function renderParams() {
  $("params-body").innerHTML = cfgFields.map((f) => {
    const id = "p_" + f.name;
    if (f.type === "bool")
      return `<div class="pfld"><label for="${id}">${f.name}</label>
        <input type="checkbox" id="${id}" ${f.default ? "checked" : ""}></div>`;
    const isNum = f.type === "int" || f.type === "float";
    const val = f.default === null || f.default === undefined ? "" : f.default;
    return `<div class="pfld"><label for="${id}">${f.name}</label>
      <input type="${isNum ? "number" : "text"}" id="${id}"
        ${isNum ? 'step="any"' : ""} value="${val}"
        placeholder="${f.type === "optional" ? "default" : ""}"></div>`;
  }).join("");
}

$("params-reset").onclick = renderParams;

function collectConfig() {
  const cfg = {};
  for (const f of cfgFields) {
    const el = $("p_" + f.name);
    if (!el) continue;
    if (f.type === "bool") {
      if (el.checked !== f.default) cfg[f.name] = el.checked;
    } else if (el.value === "") {
      continue; // unchanged / use server default
    } else if (f.type === "int") {
      const n = parseInt(el.value, 10);
      if (!Number.isNaN(n) && n !== f.default) cfg[f.name] = n;
    } else if (f.type === "float") {
      const n = parseFloat(el.value);
      if (!Number.isNaN(n) && n !== f.default) cfg[f.name] = n;
    } else {
      if (String(el.value) !== String(f.default ?? "")) cfg[f.name] = el.value;
    }
  }
  return cfg;
}

async function startRun() {
  const btn = $("run-btn");
  btn.disabled = true;

  // Upload first if a file was chosen, else use the dropdown selection.
  let rsdPath = $("rsd-select").value;
  const file = $("rsd-file").files[0];
  if (file) {
    const fd = new FormData();
    fd.append("file", file);
    $("run-desc").textContent = "uploading…";
    $("run-progress").hidden = false;
    const up = await api("/api/rsd", { method: "POST", body: fd });
    if (!up.ok) { $("run-desc").textContent = "upload failed"; btn.disabled = false; return; }
    rsdPath = (await up.json()).path;
  }
  if (!rsdPath) { $("run-desc").textContent = "pick or upload an RSD"; btn.disabled = false; return; }

  const r = await api("/api/jobs/mosaic", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ rsd_path: rsdPath, config: collectConfig() }),
  });
  if (!r.ok) { $("run-desc").textContent = "submit failed"; btn.disabled = false; return; }
  const { job_id } = await r.json();
  streamJob(job_id);
}

function streamJob(jobId, onDone) {
  const usePanel = !onDone; // mosaic-run path uses the drawer progress UI
  if (usePanel) {
    $("run-progress").hidden = false;
    $("run-bar").style.width = "0%";
    $("run-desc").textContent = "queued…";
  }
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  es.onmessage = (ev) => {
    const job = JSON.parse(ev.data);
    if (!job) return;
    const p = job.progress;
    const label = job.status === "running"
      ? (p ? `${p.desc} — ${p.pct ?? "?"}%` : "running…")
      : job.status;
    if (usePanel) {
      if (p && p.pct != null) $("run-bar").style.width = p.pct + "%";
      $("run-desc").textContent = label;
    } else {
      $("status").textContent = `combine: ${label}`;
    }
    if (["done", "error", "cancelled"].includes(job.status)) {
      es.close();
      $("run-btn").disabled = false;
      if (job.status === "done") {
        if (usePanel) {
          $("run-desc").textContent = "done";
          $("run-bar").style.width = "100%";
          loadTracks();
        }
        if (onDone) onDone(job);
      } else {
        const msg = job.error ? "error: " + job.error.split("\n")[0] : job.status;
        if (usePanel) $("run-desc").textContent = msg;
        else $("status").textContent = "combine " + msg;
      }
    }
  };
  es.onerror = () => { es.close(); $("run-btn").disabled = false; };
}

// --- Phase 3c: combine builder (W2 multi-track, W3 polygon) --------------
let combineMode = false;
const selected = new Set();

function highlightSelected() {
  map.setFilter("tracks-sel",
    ["in", ["get", "file_name"], ["literal", Array.from(selected)]]);
  $("combine-go").textContent = `Combine (${selected.size})`;
  $("combine-go").hidden = selected.size === 0;
}

function setCombineMode(on) {
  combineMode = on;
  $("combine-toggle").textContent = `Combine: ${on ? "on" : "off"}`;
  $("clip-label").hidden = !on;
  if (!on) {
    selected.clear();
    map.setFilter("tracks-sel", ["==", ["get", "file_name"], "__none__"]);
    $("combine-go").hidden = true;
  } else {
    $("panel").hidden = true;
    removeCog();
  }
}

function toggleSelect(name) {
  if (selected.has(name)) selected.delete(name);
  else selected.add(name);
  highlightSelected();
}

async function runCombine(body, desc) {
  $("status").textContent = `combine: ${desc}…`;
  const r = await api("/api/jobs/combine", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) { $("status").textContent = "combine submit failed"; return; }
  const { job_id } = await r.json();
  streamJob(job_id, (job) => {
    const cog = job.result && job.result.cog;
    if (cog) {
      showCog(cog);
      $("status").textContent =
        `combined ${job.result.rasters} run(s) (${job.result.mode})`;
    } else {
      $("status").textContent =
        "combine produced nothing: " + (job.result?.reason || "no overlap");
    }
  });
}

$("combine-toggle").onclick = () => setCombineMode(!combineMode);

$("combine-go").onclick = () => {
  const runIds = [];
  const missing = [];
  for (const name of selected) {
    const run = runsByRsd[name];
    if (run) runIds.push(run.job_id);
    else missing.push(name);
  }
  if (!runIds.length) {
    $("status").textContent = "selected tracks have no mosaics to combine";
    return;
  }
  if (missing.length)
    console.warn("skipping tracks with no mosaic:", missing);
  runCombine({ run_ids: runIds }, `${runIds.length} runs`);
};

$("clip-file").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  let gj;
  try { gj = JSON.parse(await f.text()); }
  catch { $("status").textContent = "polygon: not valid JSON"; return; }
  runCombine({ polygon: gj }, "polygon clip");
  e.target.value = "";
};

$("new-run").onclick = openDrawer;
$("drawer-close").onclick = () => ($("drawer").hidden = true);
$("run-btn").onclick = startRun;

// --- Phase 5: area polygon layers + per-area deliverable ----------------
async function loadLayers() {
  const fc = await api("/api/areas.geojson").then((r) => r.json());
  const src = "layer-areas";
  if (map.getSource(src)) { map.getSource(src).setData(fc); return; }
  map.addSource(src, { type: "geojson", data: fc });
  map.addLayer({
    id: src + "-fill", type: "fill", source: src,
    paint: { "fill-color": "#7c3aed", "fill-opacity": 0.07 },
  });
  map.addLayer({
    id: src + "-line", type: "line", source: src,
    paint: { "line-color": "#7c3aed", "line-width": 2 },
  });
  map.on("click", src + "-fill", (e) => selectArea(e.features[0]));
  map.on("mouseenter", src + "-fill",
    () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", src + "-fill",
    () => (map.getCanvas().style.cursor = ""));
}

async function selectArea(feature) {
  const pr = feature.properties || {};
  const id = pr.id;
  $("panel-title").textContent = pr.Our_Name || "Area";
  $("panel-body").innerHTML = "loading coverage…";
  $("panel").hidden = false;
  removeCog();

  const bufM = 30;
  let cov;
  try {
    const r = await api(`/api/areas/${id}/coverage?buffer_m=${bufM}`);
    if (!r.ok) throw new Error();
    cov = await r.json();
  } catch {
    $("panel-body").innerHTML = "Coverage lookup failed.";
    return;
  }
  map.setFilter("tracks-sel",
    ["in", ["get", "file_name"], ["literal", cov.tracks]]);

  $("panel-body").innerHTML =
    `<dl><dt>TPWD App No</dt><dd>${pr.TPWD_App_No ?? "—"}</dd></dl>` +
    `<label style="font-size:12px">Buffer (m)
       <input id="area-buf" type="number" value="${bufM}" min="0" step="5"
              style="width:70px"></label>
     <button id="gen-deliverable" class="badge"
             style="border:0;cursor:pointer;margin-left:8px">
       Generate deliverable</button>
     <div id="dl-slot"></div>`;
  $("gen-deliverable").onclick = () =>
    generateDeliverable(id, parseFloat($("area-buf").value) || 30);
}

async function generateDeliverable(areaId, bufferM) {
  $("status").textContent = "deliverable: submitting…";
  const r = await api(`/api/areas/${areaId}/mosaic`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ buffer_m: bufferM }),
  });
  if (!r.ok) { $("status").textContent = "deliverable submit failed"; return; }
  const { job_id } = await r.json();
  streamJob(job_id, (job) => {
    const res = job.result || {};
    if (res.cog) showCog(res.cog);
    if (res.ok) {
      $("status").textContent =
        `deliverable ready (${res.rasters} run(s), buffer ${bufferM} m)`;
      const slot = $("dl-slot");
      if (slot)
        slot.innerHTML =
          `<a id="dl-link" href="/api/deliverable/${job_id}">` +
          `Download GeoTIFF</a>`;
      // refresh layer + table state (has_mosaic, mosaic_job_id)
      loadLayers();
      if (!$("data-panel").hidden) loadDataTable();
    } else {
      $("status").textContent =
        "deliverable: " + (res.reason || "failed");
    }
  });
}

async function uploadAreas(file) {
  const fd = new FormData(); fd.append("file", file);
  $("layers-status").textContent = "uploading…";
  const r = await api("/api/areas/upload", { method: "POST", body: fd });
  if (!r.ok) { $("layers-status").textContent = "upload failed"; return; }
  const j = await r.json();
  let msg = `added ${j.added}, updated ${j.updated}` +
            (j.skipped ? `, skipped ${j.skipped}` : "");
  // When everything got skipped, show what was wrong + which property
  // keys the file actually had so the mismatch is obvious.
  if (j.added === 0 && j.updated === 0 && j.skipped > 0) {
    msg += `\nFirst issues:\n- ${(j.skipped_reasons || []).join("\n- ")}`;
    msg += `\nProperty keys seen: ${(j.sample_property_keys || []).join(", ")}`;
  }
  $("layers-status").textContent = msg;
  $("layers-status").style.whiteSpace = "pre-wrap";
  loadLayers();
  if (!$("data-panel").hidden) loadDataTable();
}

$("layers-toggle").onclick = () =>
  ($("layers-box").hidden = !$("layers-box").hidden);
$("up-areas").onchange = (e) => {
  const f = e.target.files[0]; if (f) uploadAreas(f);
  e.target.value = "";
};

// --- Phase 6: data table -------------------------------------------------
async function loadDataTable() {
  const rows = await api("/api/areas").then((r) => r.json());
  $("data-meta").textContent =
    `${rows.length} area(s) — ${rows.filter((r) => r.has_mosaic).length} have a mosaic`;
  const tbody = document.querySelector("#data-table tbody");
  tbody.innerHTML = "";
  for (const a of rows) {
    const tr = document.createElement("tr");
    tr.dataset.id = a.id;
    tr.innerHTML = `
      <td>${escapeHtml(a.our_name)}</td>
      <td>${escapeHtml(a.tpwd_app_no)}</td>
      <td><input class="note" type="text" value="${escapeAttr(a.notes || "")}"></td>
      <td><span class="pill ${a.has_mosaic ? "yes" : "no"}">
        ${a.has_mosaic ? "ready" : "—"}</span></td>
      <td class="actions">
        <button class="view">View</button>
        <input class="buf" type="number" value="30" min="0" step="5" title="buffer (m)">
        <button class="gen">Generate</button>
        <a class="dl" ${a.has_mosaic ? `href="/api/deliverable/${a.mosaic_job_id}"`
                                      : 'aria-disabled="true" href="#"'}>
          Download</a>
      </td>`;
    const note = tr.querySelector(".note");
    note.addEventListener("change", () => saveNote(a.id, note.value));
    tr.querySelector(".view").onclick = () => viewArea(a);
    tr.querySelector(".gen").onclick = () =>
      generateDeliverable(a.id,
        parseFloat(tr.querySelector(".buf").value) || 30);
    tbody.appendChild(tr);
  }
}

async function saveNote(id, notes) {
  await api(`/api/areas/${id}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ notes }),
  });
}

async function viewArea(row) {
  $("data-panel").hidden = true;
  // pull the geometry to fit bounds + overlay mosaic if present
  const a = await api(`/api/areas/${row.id}`).then((r) => r.json());
  const bb = geomBBox(a.geometry);
  if (bb) map.fitBounds(bb, { padding: 60 });
  // simulate a map click on this area so the side panel + highlight load
  selectArea({ properties: {
    id: a.id, Our_Name: a.our_name, TPWD_App_No: a.tpwd_app_no
  }});
  if (row.has_mosaic && row.mosaic_job_id) {
    // overlay the existing mosaic immediately
    const j = await api(`/api/jobs/${row.mosaic_job_id}`).then((r) => r.json());
    const cog = j && j.result && j.result.cog;
    if (cog) showCog(cog);
  }
}

function geomBBox(g) {
  if (!g) return null;
  let minX = 180, minY = 90, maxX = -180, maxY = -90;
  const walk = (c) => {
    if (typeof c[0] === "number") {
      if (c[0] < minX) minX = c[0]; if (c[0] > maxX) maxX = c[0];
      if (c[1] < minY) minY = c[1]; if (c[1] > maxY) maxY = c[1];
    } else c.forEach(walk);
  };
  walk(g.coordinates || []);
  return maxX >= minX ? [[minX, minY], [maxX, maxY]] : null;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
              '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

$("data-toggle").onclick = () => {
  const p = $("data-panel");
  p.hidden = !p.hidden;
  if (!p.hidden) loadDataTable();
};
$("data-close").onclick = () => ($("data-panel").hidden = true);

boot();
