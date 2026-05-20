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
    glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
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
const TRACK_LAYER_IDS = ["tracks-case", "tracks-line", "tracks-sel"];

function setLayerVisibility(ids, visible) {
  const visibility = visible ? "visible" : "none";
  for (const id of ids) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visibility);
  }
}

function applyTrackVisibility() {
  const ctl = $("tracks-visible");
  setLayerVisibility(TRACK_LAYER_IDS, !ctl || ctl.checked);
}

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
      id: "tracks-case", type: "line", source: "tracks",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#0f172a",
        "line-width": 5,
        "line-opacity": 0.35,
      },
    });
    map.addLayer({
      id: "tracks-line", type: "line", source: "tracks",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#38bdf8",
        "line-width": 3,
        "line-opacity": 0.95,
        "line-dasharray": [0.4, 1.4],
      },
    });
    map.addLayer({
      id: "tracks-sel", type: "line", source: "tracks",
      filter: ["==", ["get", "file_name"], "__none__"],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#f97316", "line-width": 6, "line-opacity": 0.9 },
    });
    map.on("click", "tracks-line", (e) => selectTrack(e.features[0]));
    map.on("mouseenter", "tracks-line", () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", "tracks-line", () => (map.getCanvas().style.cursor = ""));
  }
  applyTrackVisibility();

  const bb = bboxOf(tracks);
  const n = (tracks.features || []).length;
  $("status").textContent = n ? `${n} track(s)` : "no tracks yet — run the inventory";
  if (bb) map.fitBounds(bb, { padding: 60, duration: 0 });
}

// --- track selection -> overlay its mosaic COG ---------------------------
async function selectTrack(feature) {
  const p = feature.properties || {};
  const name = p.file_name || "(unknown)";
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
    "<dl>" + rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("") + "</dl>" + extra +
    `<div id="meta-slot" class="muted" style="margin-top:14px">loading survey metadata…</div>
     <div style="margin-top:16px;display:flex;gap:8px">
       <button id="del-track" class="danger ghost" title="Remove just the track feature from the inventory">Delete track</button>
       <button id="del-rsd" class="danger" title="Delete RSD file, track, and every mosaic run for it">Delete RSD + runs</button>
     </div>`;
  $("panel").hidden = false;
  loadTrackMetadata(name);
  $("del-track").onclick = () => deleteTrackOnly(name);
  $("del-rsd").onclick = () => deleteRsdCascade(name);
}

async function deleteTrackOnly(name) {
  if (!confirm(`Remove "${name}" from the track inventory only?\n` +
               `The RSD file and any mosaic runs stay.`)) return;
  const r = await api(`/api/tracks/${encodeURIComponent(name)}`,
                      { method: "DELETE" });
  if (!r.ok) { alert("delete failed"); return; }
  $("panel").hidden = true;
  loadTracks();
}

async function deleteRsdCascade(name) {
  if (!confirm(`Delete "${name}" completely?\n\n` +
               `This removes the RSD file, the inventory entry, and ALL ` +
               `mosaic runs (and their COGs) made from it. Existing ` +
               `area deliverables that used it remain on disk but the ` +
               `source data is gone.`)) return;
  const r = await api(`/api/rsd/${encodeURIComponent(name)}`,
                      { method: "DELETE" });
  if (!r.ok) { alert("delete failed"); return; }
  const j = await r.json();
  $("status").textContent =
    `deleted ${name}: ${j.mosaic_runs} run(s), ${j.track_features} track entr(y/ies)` +
    (j.rsd_file ? ", RSD file" : "");
  $("panel").hidden = true;
  removeCog();
  loadTracks();
  loadLayers();
}

async function loadTrackMetadata(fileName) {
  const slot = $("meta-slot");
  if (!slot) return;
  const r = await api(`/api/tracks/${encodeURIComponent(fileName)}/metadata`);
  if (!r.ok) {
    slot.textContent =
      r.status === 404 ? "no survey metadata on disk for this RSD"
                       : "metadata fetch failed";
    return;
  }
  const m = await r.json();
  slot.classList.remove("muted");
  const stat = (s, suffix = "") =>
    s ? `${s.mean}${suffix} (${s.min}–${s.max})` : "—";
  const dur = m.duration_s
    ? `${Math.round(m.duration_s / 60)} min`
    : "—";
  const u = m.unit || {};
  const unitLine = [
    u.product_number ? `product ${u.product_number}` : null,
    u.software_version ? `sw ${u.software_version}` : null,
    u.channel_count ? `${u.channel_count} ch` : null,
  ].filter(Boolean).join(" · ") || "—";
  slot.innerHTML = `
    <h3 style="margin:0 0 8px;font-size:13px;color:#374151">Survey metadata</h3>
    <dl>
      <dt>Pings</dt><dd>${m.ping_count ?? "—"}</dd>
      <dt>Duration</dt><dd>${dur}</dd>
      <dt>Depth (m)</dt><dd>${stat(m.depth_m)}</dd>
      <dt>Range (m)</dt><dd>${stat(m.range_m)}</dd>
      <dt>UTM zone</dt><dd>${m.utm_zone ?? "—"}</dd>
      <dt>Garmin unit</dt><dd>${unitLine}</dd>
    </dl>
    <p class="muted" style="font-size:11px;margin:6px 0 0">
      source: ${m.source}</p>`;
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
  $("data-toggle").hidden = false;
  $("new-run").hidden = false;
  $("layers-toggle").hidden = false;
  $("files-toggle").hidden = false;
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
    paint: { "fill-color": "#facc15", "fill-opacity": 0.36 },
  });
  map.addLayer({
    id: src + "-line", type: "line", source: src,
    paint: { "line-color": "#92400e", "line-width": 3 },
  });
  map.addLayer({
    id: src + "-attention-fill", type: "fill", source: src,
    filter: ["==", ["get", "needs_attention"], true],
    paint: { "fill-color": "#ef4444", "fill-opacity": 0.34 },
  });
  map.addLayer({
    id: src + "-attention-line", type: "line", source: src,
    filter: ["==", ["get", "needs_attention"], true],
    paint: {
      "line-color": "#dc2626",
      "line-width": 4,
      "line-dasharray": [1.2, 0.8],
    },
  });
  map.addLayer({
    id: src + "-label", type: "symbol", source: src,
    layout: {
      "text-field": [
        "concat",
        ["case", ["==", ["get", "needs_attention"], true], "REDO: ", ""],
        ["case", ["has", "Our_Name"], ["to-string", ["get", "Our_Name"]], "Area"],
        " (",
        ["case", ["has", "TPWD_App_No"], ["to-string", ["get", "TPWD_App_No"]], "-"],
        ")",
      ],
      "text-font": ["Noto Sans Regular"],
      "text-size": 12,
      "text-offset": [0, 0.2],
      "text-anchor": "center",
    },
    paint: {
      "text-color": [
        "case",
        ["==", ["get", "needs_attention"], true],
        "#991b1b",
        "#713f12",
      ],
      "text-halo-color": [
        "case",
        ["==", ["get", "needs_attention"], true],
        "#fee2e2",
        "#fffbeb",
      ],
      "text-halo-width": 1.6,
    },
  });
  map.on("click", src + "-fill", (e) => selectArea(e.features[0]));
  map.on("mouseenter", src + "-fill",
    () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", src + "-fill",
    () => (map.getCanvas().style.cursor = ""));
}

// Backend takes meters; UI works in feet for the survey/permitting workflow.
const FT_TO_M = 0.3048;
const DEFAULT_BUFFER_FT = 200;

async function selectArea(feature) {
  const pr = feature.properties || {};
  const id = pr.id;
  $("panel-title").textContent = pr.Our_Name || "Area";
  $("panel-body").innerHTML = "loading coverage…";
  $("panel").hidden = false;
  removeCog();

  const bufFt = DEFAULT_BUFFER_FT;
  let cov;
  try {
    const r = await api(
      `/api/areas/${id}/coverage?buffer_m=${bufFt * FT_TO_M}`
    );
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
    `<label style="font-size:12px">Buffer (ft)
       <input id="area-buf" type="number" value="${bufFt}" min="0" step="10"
              style="width:70px"></label>
     <button id="gen-deliverable" class="badge"
             style="border:0;cursor:pointer;margin-left:8px">
       Generate deliverable</button>
     <div id="dl-slot"></div>`;
  $("gen-deliverable").onclick = () =>
    generateDeliverable(id,
      (parseFloat($("area-buf").value) || DEFAULT_BUFFER_FT) * FT_TO_M);
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
      const ft = Math.round(bufferM / FT_TO_M);
      $("status").textContent =
        `deliverable ready (${res.rasters} run(s), buffer ${ft} ft)`;
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
$("tracks-visible").onchange = applyTrackVisibility;
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
        <input class="buf" type="number" value="${DEFAULT_BUFFER_FT}" min="0" step="10" title="buffer (ft)">
        <button class="gen">Generate</button>
        <a class="dl" ${a.has_mosaic ? `href="/api/deliverable/${a.mosaic_job_id}"`
                                      : 'aria-disabled="true" href="#"'}>
          GeoTIFF</a>
        <a class="dl" ${a.has_mosaic ? `href="/api/deliverable/${a.mosaic_job_id}/metadata.txt"`
                                      : 'aria-disabled="true" href="#"'}>
          Metadata</a>
        <button class="del" title="Delete this area">✕</button>
      </td>`;
    const note = tr.querySelector(".note");
    note.addEventListener("change", () => saveNote(a.id, note.value));
    tr.querySelector(".view").onclick = () => viewArea(a);
    tr.querySelector(".gen").onclick = () =>
      generateDeliverable(a.id,
        (parseFloat(tr.querySelector(".buf").value) || DEFAULT_BUFFER_FT)
          * FT_TO_M);
    tr.querySelector(".del").onclick = () => deleteArea(a);
    tbody.appendChild(tr);
  }
}

async function deleteArea(a) {
  if (!confirm(
    `Delete area "${a.our_name}" (TPWD ${a.tpwd_app_no})?\n\n` +
    `This removes the row, its notes, and the link to its last ` +
    `deliverable. Existing deliverable files on disk are left alone.`)) return;
  const r = await api(`/api/areas/${a.id}`, { method: "DELETE" });
  if (!r.ok) { alert("delete failed"); return; }
  loadDataTable();
  loadLayers();
}

async function saveNote(id, notes) {
  const r = await api(`/api/areas/${id}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ notes }),
  });
  if (r.ok) loadLayers();
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

// --- Files & tracks management page -------------------------------------
function fmtBytes(n) {
  if (!n) return "0";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

let _filesCache = [];

async function loadFilesTable() {
  _filesCache = await api("/api/files").then((r) => r.json());
  renderFilesTable();
}

function renderFilesTable() {
  const filter = $("files-filter").value.trim().toLowerCase();
  const dupesOnly = $("files-dupes").checked;
  const rows = _filesCache.filter((r) =>
    (!filter || r.file_name.toLowerCase().includes(filter)) &&
    (!dupesOnly || r.duplicate));
  const dups = _filesCache.filter((r) => r.duplicate).length;
  const totalDisk = _filesCache.reduce((s, r) => s + r.disk_bytes, 0);
  $("files-meta").textContent =
    `${_filesCache.length} entr(y/ies)  ·  ${dups} with duplicates  ·  ` +
    `${fmtBytes(totalDisk)} used`;

  const tbody = document.querySelector("#files-table tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    if (r.duplicate) tr.classList.add("dup");

    const rsd = r.rsd_file.present
      ? `<div class="item">${fmtBytes(r.rsd_file.size)}
           <button class="x" title="Delete the RSD file only"
                   data-act="rsd-file" data-name="${r.file_name}">file ✕</button></div>`
      : `<span class="muted-cell">—</span>`;

    const tracks = r.tracks.length
      ? r.tracks.map((t) =>
          `<div class="item">
             <code>#${t.index}</code>
             ${t.point_count ?? "—"} pts · ${escapeHtml(t.metadata_source || "")}
             <button class="x" data-act="track-idx"
                     data-name="${r.file_name}" data-idx="${t.index}">✕</button>
           </div>`).join("")
      : `<span class="muted-cell">—</span>`;

    const runs = r.runs.length
      ? r.runs.map((j) =>
          `<div class="item">
             <code>${j.job_id.slice(0, 8)}</code>
             ${fmtBytes(j.disk_size)}${j.imported ? " · imp" : ""}
             ${j.has_cog ? "" : ' <span class="muted-cell">no cog</span>'}
             <button class="x" data-act="run" data-jid="${j.job_id}">✕</button>
           </div>`).join("")
      : `<span class="muted-cell">—</span>`;

    tr.innerHTML = `
      <td><strong>${escapeHtml(r.file_name)}</strong></td>
      <td>${rsd}</td>
      <td>${tracks}</td>
      <td>${runs}</td>
      <td>${fmtBytes(r.disk_bytes)}</td>
      <td><button class="nuke" data-act="nuke" data-name="${r.file_name}">
        delete everything</button></td>`;
    tbody.appendChild(tr);
  }

  tbody.querySelectorAll("button[data-act]").forEach((b) => {
    b.onclick = () => filesAction(b.dataset);
  });
}

async function filesAction(d) {
  const ask = (msg) => confirm(msg);
  let url, method = "DELETE", desc;
  if (d.act === "rsd-file") {
    if (!ask(`Delete the RSD file "${d.name}" only? Inventory + runs stay.`)) return;
    url = `/api/rsd/${encodeURIComponent(d.name)}/file`;
    desc = `RSD file ${d.name}`;
  } else if (d.act === "track-idx") {
    if (!ask(`Remove inventory entry #${d.idx} for "${d.name}"?`)) return;
    url = `/api/tracks/${encodeURIComponent(d.name)}/${d.idx}`;
    desc = `track ${d.name}#${d.idx}`;
  } else if (d.act === "run") {
    if (!ask(`Delete mosaic run ${d.jid.slice(0, 8)} (run dir + DB row)?`)) return;
    url = `/api/runs/${d.jid}`;
    desc = `run ${d.jid.slice(0, 8)}`;
  } else if (d.act === "nuke") {
    if (!ask(`Delete EVERYTHING for "${d.name}"?\n\nRSD file + all inventory entries + all mosaic runs.`)) return;
    url = `/api/rsd/${encodeURIComponent(d.name)}`;
    desc = `everything for ${d.name}`;
  } else return;

  const r = await api(url, { method });
  if (!r.ok) { alert(`delete failed: ${desc}`); return; }
  await loadFilesTable();
  loadTracks();
  loadLayers();
}

$("files-toggle").onclick = () => {
  const p = $("files-panel");
  p.hidden = !p.hidden;
  if (!p.hidden) loadFilesTable();
};
$("files-close").onclick = () => ($("files-panel").hidden = true);
$("files-filter").addEventListener("input", renderFilesTable);
$("files-dupes").addEventListener("change", renderFilesTable);
$("files-reload").onclick = loadFilesTable;

boot();
