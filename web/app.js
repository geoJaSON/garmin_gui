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
  // titiler.core namespaces tile routes by TileMatrixSet; WebMercatorQuad
  // matches the OSM basemap. tilejson carries the tile URL templates+bounds.
  const tj = await api(
    "/tiles/WebMercatorQuad/tilejson.json?url=" + encodeURIComponent(cogPath)
  ).then((r) => r.json());
  removeCog();
  map.addSource("cog", { type: "raster", tiles: tj.tiles, tileSize: 256,
                         bounds: tj.bounds });
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
  if (map.loaded()) loadTracks();
  else map.on("load", loadTracks);
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

function streamJob(jobId) {
  $("run-progress").hidden = false;
  $("run-bar").style.width = "0%";
  $("run-desc").textContent = "queued…";
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  es.onmessage = (ev) => {
    const job = JSON.parse(ev.data);
    if (!job) return;
    const p = job.progress;
    if (p && p.pct != null) $("run-bar").style.width = p.pct + "%";
    $("run-desc").textContent =
      job.status === "running"
        ? `${p ? p.desc + " — " + (p.pct ?? "?") + "%" : "running…"}`
        : job.status;
    if (["done", "error", "cancelled"].includes(job.status)) {
      es.close();
      $("run-btn").disabled = false;
      if (job.status === "done") {
        $("run-desc").textContent = "done";
        $("run-bar").style.width = "100%";
        loadTracks(); // refresh inventory + runs; click the track to view it
      } else {
        $("run-desc").textContent = job.error ? "error: " + job.error.split("\n")[0] : job.status;
      }
    }
  };
  es.onerror = () => { es.close(); $("run-btn").disabled = false; };
}

$("new-run").onclick = openDrawer;
$("drawer-close").onclick = () => ($("drawer").hidden = true);
$("run-btn").onclick = startRun;

boot();
