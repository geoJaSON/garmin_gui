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
let selectedAreaId = null;
const TRACK_LAYER_IDS = ["tracks-case", "tracks-line", "tracks-sel"];
const AREA_LAYER_IDS = [
  "layer-areas-fill",
  "layer-areas-line",
  "layer-areas-attention-fill",
  "layer-areas-attention-line",
  "layer-areas-label",
];
const activeAreaMosaics = new Set();
let readyAreaMosaicIds = new Set();
let tracksVisible = true;

function setLayerVisibility(ids, visible) {
  const visibility = visible ? "visible" : "none";
  for (const id of ids) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visibility);
  }
}

function applyTrackVisibility() {
  setLayerVisibility(TRACK_LAYER_IDS, tracksVisible);
  const btn = $("tracks-toggle");
  if (btn) {
    btn.textContent = `Tracks: ${tracksVisible ? "On" : "Off"}`;
    btn.classList.toggle("is-off", !tracksVisible);
  }
}

function updateMosaicControls() {
  document.querySelectorAll("#data-table .mosaic-visible").forEach((btn) => {
    const id = btn.closest("tr")?.dataset.id;
    const isOn = activeAreaMosaics.has(id);
    btn.textContent = isOn ? "Hide mosaic" : "Show mosaic";
    btn.classList.toggle("is-on", isOn);
    btn.setAttribute("aria-pressed", isOn ? "true" : "false");
  });

  const allBtn = $("all-mosaics-toggle");
  if (!allBtn) return;
  const readyIds = Array.from(readyAreaMosaicIds);
  const allShown = readyIds.length > 0 &&
    readyIds.every((id) => activeAreaMosaics.has(id));
  allBtn.textContent = allShown ? "Hide all mosaics" : "Show all mosaics";
  allBtn.classList.toggle("is-on", allShown);
}

function raiseAreaLayers() {
  for (const id of AREA_LAYER_IDS) {
    if (map.getLayer(id)) map.moveLayer(id);
  }
}

function firstAreaLayerId() {
  return AREA_LAYER_IDS.find((id) => map.getLayer(id));
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
  raiseAreaLayers();

  const bb = bboxOf(tracks);
  const n = (tracks.features || []).length;
  $("status").textContent = n ? `${n} track(s)` : "no tracks yet — run the inventory";
  if (bb) map.fitBounds(bb, { padding: 60, duration: 0 });
}

// --- track selection -> overlay its mosaic COG ---------------------------
async function selectTrack(feature) {
  const p = feature.properties || {};
  const name = p.file_name || "(unknown)";
  selectedAreaId = null;
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

  // Weather (optional — present after backfill or a fresh-mosaic run).
  const w = m.weather;
  const num = (v, d = 1) =>
    v == null || v === "" ? null : Number(v).toFixed(d);
  let weatherHtml = "";
  if (w) {
    const wsMax = num(w.wind_speed_max_ms, 1);
    const wsGust = num(w.wind_gusts_max_ms, 1);
    const windLine =
      wsMax || wsGust
        ? `${wsMax ?? "—"} m/s (gust ${wsGust ?? "—"})`
        : "—";
    weatherHtml = `
      <h3 style="margin:14px 0 8px;font-size:13px;color:#374151">
        Weather${w.date ? ` <span style="color:#9ca3af;font-weight:normal">(${w.date})</span>` : ""}
      </h3>
      <dl>
        <dt>Air temp</dt><dd>${num(w.temperature_2m_mean_c, 1) ?? "—"} °C</dd>
        <dt>Wind max</dt><dd>${windLine}</dd>
        <dt>Wave max</dt><dd>${num(w.wave_height_max_m, 2) ?? "—"} m</dd>
        <dt>Sea surface temp</dt><dd>${num(w.sea_surface_temperature_mean_c, 1) ?? "—"} °C</dd>
        <dt>Precipitation</dt><dd>${num(w.precipitation_sum_mm, 1) ?? "—"} mm</dd>
        <dt>Cloud cover</dt><dd>${num(w.cloud_cover_mean_pct, 0) ?? "—"} %</dd>
      </dl>`;
  }

  const surveyLine = m.survey_datetime
    ? `<p class="muted" style="font-size:11px;margin:6px 0 0">
         survey: ${m.survey_datetime} · source: ${m.source}</p>`
    : `<p class="muted" style="font-size:11px;margin:6px 0 0">
         source: ${m.source}</p>`;

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
    ${weatherHtml}
    ${surveyLine}`;
}

async function showCog(cogPath, isCurrent = () => true) {
  // Do NOT trust tilejson's `tiles` URLs — TiTiler's url_for has emitted a
  // path (/tiles/{tms}/...) that doesn't match its real route
  // (/tiles/tiles/{tms}/...), 404ing every tile. We build the known-good
  // endpoint ourselves and use tilejson only for bounds/zoom. Cache-bust
  // so a stale cached tilejson can't feed old bounds.
  const enc = encodeURIComponent(cogPath);
  const tj = await api(
    `/tiles/WebMercatorQuad/tilejson.json?url=${enc}&_=${Date.now()}`
  ).then((r) => r.json());
  if (!isCurrent()) return;

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
  map.addLayer(
    { id: "cog", type: "raster", source: "cog" },
    firstAreaLayerId() || "tracks-line"
  );
  raiseAreaLayers();
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
  selectedAreaId = null;
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
  $("tracks-toggle").hidden = false;
  $("new-run").hidden = false;
  $("all-mosaics-toggle").hidden = false;
  $("files-toggle").hidden = false;
  $("queue-toggle").hidden = false;
  $("plan-toggle").hidden = false;
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
    : `<option value="" disabled>— none uploaded —</option>`;
  updateRsdCount();
  cfgFields = cfg.fields;
  renderParams();
}

function updateRsdCount() {
  const n = $("rsd-select").selectedOptions.length;
  $("rsd-count").textContent = `${n} selected`;
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
  $("run-progress").hidden = false;
  $("run-bar").style.width = "0%";
  $("run-queue").textContent = "";

  // 1) Build the queue: any uploaded file goes first, then every selected
  //    item from the multi-select.
  const queue = [];
  const file = $("rsd-file").files[0];
  if (file) {
    const fd = new FormData();
    fd.append("file", file);
    $("run-desc").textContent = `uploading ${file.name}…`;
    const up = await api("/api/rsd", { method: "POST", body: fd });
    if (!up.ok) {
      $("run-desc").textContent = "upload failed"; btn.disabled = false; return;
    }
    const u = await up.json();
    queue.push({ path: u.path, name: u.name });
  }
  for (const o of $("rsd-select").selectedOptions) {
    if (!o.value) continue;
    queue.push({ path: o.value, name: o.textContent });
  }
  if (!queue.length) {
    $("run-desc").textContent = "pick or upload at least one RSD";
    btn.disabled = false; return;
  }

  const cfg = collectConfig();
  const total = queue.length;
  let done = 0, failed = 0;

  // 2) Sequential: submit, await done, move on. The serial worker would
  //    process them in order anyway; awaiting lets us mirror per-RSD
  //    progress in the drawer instead of a queued blob.
  for (const item of queue) {
    const remaining = total - done - failed;
    $("run-queue").textContent =
      `Batch: ${done + failed + 1} of ${total}  ·  ${remaining - 1} queued after this`;
    $("run-desc").textContent = `submitting ${item.name}…`;
    $("run-bar").style.width = "0%";

    const r = await api("/api/jobs/mosaic", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ rsd_path: item.path, config: cfg }),
    });
    if (!r.ok) {
      $("run-desc").textContent = `submit failed: ${item.name}`;
      failed++;
      continue;
    }
    const { job_id } = await r.json();
    const ok = await awaitStreamJob(job_id, item.name);
    if (ok) done++; else failed++;
  }

  $("run-bar").style.width = "100%";
  $("run-desc").textContent =
    failed
      ? `batch done: ${done}/${total} succeeded, ${failed} failed`
      : `batch done: ${total}/${total} succeeded`;
  $("run-queue").textContent = "";
  btn.disabled = false;
  loadTracks();
}

// Promise-based streamJob used by the batch loop: mirrors the drawer's
// progress bar/desc onto the currently-running job and resolves true on
// done, false on error/cancelled. Console-log onError so the user can
// still see what went wrong.
function awaitStreamJob(jobId, label) {
  return new Promise((resolve) => {
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    es.onmessage = (ev) => {
      const job = JSON.parse(ev.data);
      if (!job) return;
      const p = job.progress;
      if (p && p.pct != null) $("run-bar").style.width = p.pct + "%";
      $("run-desc").textContent =
        job.status === "running"
          ? `${label} — ${p ? `${p.desc} (${p.pct ?? "?"}%)` : "running…"}`
          : `${label}: ${job.status}`;
      if (["done", "error", "cancelled"].includes(job.status)) {
        es.close();
        if (job.status === "error" && job.error) {
          console.warn(`[${label}]`, job.error.split("\n")[0]);
        }
        resolve(job.status === "done");
      }
    };
    es.onerror = () => { es.close(); resolve(false); };
  });
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
$("rsd-select").addEventListener("change", updateRsdCount);
$("rsd-all").onclick = () => {
  for (const o of $("rsd-select").options) o.selected = !o.disabled;
  updateRsdCount();
};
$("rsd-none").onclick = () => {
  for (const o of $("rsd-select").options) o.selected = false;
  updateRsdCount();
};

// --- Queue panel --------------------------------------------------------
let _queueTimer = null;

function relTime(t) {
  if (!t) return "—";
  const s = Math.max(0, (Date.now() / 1000) - Number(t));
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function jobLabel(j) {
  const p = j.params || {};
  const r = j.result || {};
  if (j.kind === "mosaic") {
    return r.rsd_name
      || (p.rsd_path ? p.rsd_path.split("/").pop() : "(mosaic)");
  }
  if (j.kind === "tracks") {
    return `inventory · ${p.input_folder || ""}`;
  }
  if (j.kind === "combine") {
    if (r.area_name) return `area · ${r.area_name}`;
    if (p.area_id || (p.area && p.area.Our_Name))
      return `area · ${(p.area && p.area.Our_Name) || p.area_id}`;
    if (p.polygon) return "polygon clip";
    if ((p.run_ids || []).length) return `merge ${p.run_ids.length} runs`;
    return "(combine)";
  }
  return j.kind || "(unknown)";
}

async function loadQueueTable() {
  const jobs = await api("/api/jobs").then((r) => r.json()).catch(() => []);
  if (!Array.isArray(jobs)) return;
  const counts = { queued: 0, running: 0, done: 0, error: 0, cancelled: 0 };
  for (const j of jobs) if (j.status in counts) counts[j.status]++;
  $("queue-meta").textContent =
    `${jobs.length} job(s) · ` +
    `${counts.running} running · ${counts.queued} queued · ` +
    `${counts.done} done · ${counts.error} error`;

  const tbody = document.querySelector("#queue-table tbody");
  tbody.innerHTML = "";
  for (const j of jobs) {
    const tr = document.createElement("tr");
    const p = j.progress || {};
    const pct = j.status === "done" ? 100 : (p.pct ?? 0);
    const bar = (j.status === "running" || j.status === "done")
      ? `<div class="qbar"><div style="width:${pct}%"></div></div>
         <small>${p.desc || ""}</small>`
      : "—";
    const when = relTime(j.finished_at || j.started_at || j.created_at);
    const canDel = ["done", "error", "cancelled"].includes(j.status);
    tr.innerHTML = `
      <td>${j.kind}</td>
      <td class="item">${escapeHtml(jobLabel(j))}
        ${j.error ? `<br><small style="color:#b91c1c">${escapeHtml(j.error.split("\\n")[0])}</small>` : ""}</td>
      <td><span class="pill ${j.status}">${j.status}</span></td>
      <td>${bar}</td>
      <td>${when}</td>
      <td>${canDel ? `<button class="x" data-jid="${j.id}">✕</button>` : ""}</td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("button.x").forEach((b) => {
    b.onclick = async () => {
      if (!confirm("Remove this job from the queue (and any on-disk run dir)?")) return;
      await api(`/api/runs/${b.dataset.jid}`, { method: "DELETE" });
      loadQueueTable();
    };
  });
}

function openQueue() {
  $("queue-panel").hidden = false;
  loadQueueTable();
  if (!_queueTimer) _queueTimer = setInterval(loadQueueTable, 1500);
}
function closeQueue() {
  $("queue-panel").hidden = true;
  if (_queueTimer) { clearInterval(_queueTimer); _queueTimer = null; }
}

$("queue-toggle").onclick = () =>
  $("queue-panel").hidden ? openQueue() : closeQueue();
$("queue-close").onclick = closeQueue;

// --- Plan survey widget (web/survey_plan.js) ----------------------------
$("plan-toggle").onclick = () => {
  const open = !document.getElementById("plan-panel") ||
               document.getElementById("plan-panel").hidden;
  open ? window.SurveyPlan.open(map) : window.SurveyPlan.close(map);
};

// --- Phase 5: area polygon layers + per-area deliverable ----------------
async function loadLayers() {
  const fc = await api("/api/areas.geojson").then((r) => r.json());
  const src = "layer-areas";
  if (map.getSource(src)) {
    map.getSource(src).setData(fc);
    raiseAreaLayers();
    return;
  }
  map.addSource(src, { type: "geojson", data: fc });
  map.addLayer({
    id: src + "-fill", type: "fill", source: src,
    paint: { "fill-color": "#facc15", "fill-opacity": 0 },
  });
  map.addLayer({
    id: src + "-line", type: "line", source: src,
    paint: { "line-color": "#fef08a", "line-width": 4 },
  });
  map.addLayer({
    id: src + "-attention-fill", type: "fill", source: src,
    filter: ["==", ["get", "needs_attention"], true],
    paint: { "fill-color": "#ef4444", "fill-opacity": 0 },
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
      "text-color": "#000",
      "text-halo-color": "#fff",
      "text-halo-width": 1.8,
    },
  });
  map.on("click", src + "-fill", (e) => selectArea(e.features[0]));
  map.on("mouseenter", src + "-fill",
    () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", src + "-fill",
    () => (map.getCanvas().style.cursor = ""));
  raiseAreaLayers();
}

// Backend takes meters; UI works in feet for the survey/permitting workflow.
const FT_TO_M = 0.3048;
const DEFAULT_BUFFER_FT = 200;

async function selectArea(feature) {
  const pr = feature.properties || {};
  const id = pr.id;
  selectedAreaId = id;
  $("panel-title").textContent = pr.Our_Name || "Area";
  $("panel-body").innerHTML = "loading coverage…";
  $("panel").hidden = false;
  removeCog();
  if (pr.mosaic_job_id) showAreaMosaicPreview(id, pr.mosaic_job_id);

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

async function showAreaMosaicPreview(areaId, jobId) {
  const job = await api(`/api/jobs/${jobId}`).then((r) => r.json());
  if (selectedAreaId !== areaId) return;
  const cog = job && job.result && job.result.cog;
  if (cog) showCog(cog, () => selectedAreaId === areaId);
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

async function toggleAreaMosaic(row, show) {
  const sourceId = `area-mosaic-${row.id}`;
  const layerId = `${sourceId}-layer`;
  if (!show) {
    activeAreaMosaics.delete(row.id);
    if (map.getLayer(layerId)) map.removeLayer(layerId);
    if (map.getSource(sourceId)) map.removeSource(sourceId);
    updateMosaicControls();
    return;
  }

  if (!row.mosaic_job_id) return;
  activeAreaMosaics.add(row.id);
  if (map.getLayer(layerId)) {
    map.setLayoutProperty(layerId, "visibility", "visible");
    raiseAreaLayers();
    updateMosaicControls();
    return;
  }

  $("status").textContent = "area mosaic: loading…";
  const job = await api(`/api/jobs/${row.mosaic_job_id}`).then((r) => r.json());
  const cog = job && job.result && job.result.cog;
  if (!cog) {
    activeAreaMosaics.delete(row.id);
    $("status").textContent = "area mosaic unavailable";
    updateMosaicControls();
    return;
  }

  const enc = encodeURIComponent(cog);
  const tj = await api(
    `/tiles/WebMercatorQuad/tilejson.json?url=${enc}&_=${Date.now()}`
  ).then((r) => r.json());
  if (!activeAreaMosaics.has(row.id)) return;
  const tileUrl =
    `${location.origin}/tiles/tiles/WebMercatorQuad/{z}/{x}/{y}` +
    `?url=${enc}&tilesize=512`;

  map.addSource(sourceId, {
    type: "raster",
    tiles: [tileUrl],
    tileSize: 256,
    bounds: tj.bounds,
    minzoom: tj.minzoom || 0,
    maxzoom: tj.maxzoom || 24,
  });
  map.addLayer(
    {
      id: layerId,
      type: "raster",
      source: sourceId,
      paint: { "raster-opacity": 0.82 },
    },
    firstAreaLayerId()
  );
  raiseAreaLayers();
  $("status").textContent = "area mosaic shown";
  updateMosaicControls();
}

async function toggleAllAreaMosaics() {
  const btn = $("all-mosaics-toggle");
  btn.disabled = true;

  const rows = await api("/api/areas").then((r) => r.json());
  const readyRows = rows.filter((r) => r.has_mosaic && r.mosaic_job_id);
  readyAreaMosaicIds = new Set(readyRows.map((r) => r.id));
  if (!readyRows.length) {
    $("status").textContent = "no ready mosaics yet";
    btn.disabled = false;
    updateMosaicControls();
    return;
  }

  const show = !readyRows.every((r) => activeAreaMosaics.has(r.id));
  btn.textContent = show ? "Loading mosaics..." : "Hiding mosaics...";
  if (show) {
    await Promise.all(readyRows.map((r) => toggleAreaMosaic(r, true)));
    $("status").textContent = `showing ${readyRows.length} mosaic(s)`;
  } else {
    await Promise.all(readyRows.map((r) => toggleAreaMosaic(r, false)));
    $("status").textContent = "all mosaics hidden";
  }

  btn.disabled = false;
  updateMosaicControls();
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

$("tracks-toggle").onclick = () => {
  tracksVisible = !tracksVisible;
  applyTrackVisibility();
};
$("all-mosaics-toggle").onclick = toggleAllAreaMosaics;
$("up-areas").onchange = (e) => {
  const f = e.target.files[0]; if (f) uploadAreas(f);
  e.target.value = "";
};

// --- Phase 6: data table -------------------------------------------------
async function loadDataTable() {
  const rows = await api("/api/areas").then((r) => r.json());
  readyAreaMosaicIds = new Set(
    rows.filter((r) => r.has_mosaic && r.mosaic_job_id).map((r) => r.id)
  );
  $("data-meta").textContent =
    `${rows.length} area(s) — ${rows.filter((r) => r.has_mosaic).length} have a mosaic`;
  const tbody = document.querySelector("#data-table tbody");
  tbody.innerHTML = "";
  for (const a of rows) {
    const tr = document.createElement("tr");
    tr.dataset.id = a.id;
    tr.innerHTML = `
      <td data-label="Name">${escapeHtml(a.our_name)}</td>
      <td data-label="TPWD App No">${escapeHtml(a.tpwd_app_no)}</td>
      <td data-label="Notes"><input class="note" type="text" value="${escapeAttr(a.notes || "")}"></td>
      <td data-label="Mosaic">
        <span class="pill ${a.has_mosaic ? "yes" : "no"}">
          ${a.has_mosaic ? "ready" : "—"}</span>
        ${a.has_mosaic ? `<button class="mosaic-visible ${activeAreaMosaics.has(a.id) ? "is-on" : ""}"
          type="button" aria-pressed="${activeAreaMosaics.has(a.id) ? "true" : "false"}">
          ${activeAreaMosaics.has(a.id) ? "Hide mosaic" : "Show mosaic"}</button>` : ""}
      </td>
      <td class="actions" data-label="Actions">
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
    const mosaicToggle = tr.querySelector(".mosaic-visible");
    if (mosaicToggle) {
      mosaicToggle.onclick = async () => {
        mosaicToggle.disabled = true;
        await toggleAreaMosaic(a, !activeAreaMosaics.has(a.id));
        mosaicToggle.disabled = false;
      };
    }
    tr.querySelector(".view").onclick = () => viewArea(a);
    tr.querySelector(".gen").onclick = () =>
      generateDeliverable(a.id,
        (parseFloat(tr.querySelector(".buf").value) || DEFAULT_BUFFER_FT)
          * FT_TO_M);
    tr.querySelector(".del").onclick = () => deleteArea(a);
    tbody.appendChild(tr);
  }
  updateMosaicControls();
}

async function deleteArea(a) {
  if (!confirm(
    `Delete area "${a.our_name}" (TPWD ${a.tpwd_app_no})?\n\n` +
    `This removes the row, its notes, and the link to its last ` +
    `deliverable. Existing deliverable files on disk are left alone.`)) return;
  const r = await api(`/api/areas/${a.id}`, { method: "DELETE" });
  if (!r.ok) { alert("delete failed"); return; }
  await toggleAreaMosaic(a, false);
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
