"use strict";

// Survey planner widget: draw a polygon on the map, choose line spacing
// + heading, preview a serpentine survey route, download as GPX.
//
// Self-contained; depends on the maplibre `map` already created in app.js.
// Exposes window.SurveyPlan = { open, close } so app.js can wire the
// topbar button.

(function () {
  const FT_TO_M = 0.3048;

  // --- math: equirectangular meters at a reference latitude ---------------
  // Accurate to <0.1% over survey-scale polygons (km), zero dependencies.
  function projector(refLon, refLat) {
    const kLat = 111319.49;            // meters per degree latitude
    const kLon = kLat * Math.cos((refLat * Math.PI) / 180);
    return {
      to: (lon, lat) => [(lon - refLon) * kLon, (lat - refLat) * kLat],
      from: (x, y) => [refLon + x / kLon, refLat + y / kLat],
    };
  }

  function polygonCentroid(coords) {
    // simple area-weighted centroid (ring [[x,y],...] closed or not)
    let A = 0, cx = 0, cy = 0;
    const n = coords.length - (sameXY(coords[0], coords[coords.length - 1]) ? 1 : 0);
    for (let i = 0; i < n; i++) {
      const [x0, y0] = coords[i];
      const [x1, y1] = coords[(i + 1) % n];
      const cross = x0 * y1 - x1 * y0;
      A += cross; cx += (x0 + x1) * cross; cy += (y0 + y1) * cross;
    }
    if (Math.abs(A) < 1e-12) {           // degenerate -> average
      let sx = 0, sy = 0;
      for (const [x, y] of coords) { sx += x; sy += y; }
      return [sx / coords.length, sy / coords.length];
    }
    A *= 0.5;
    return [cx / (6 * A), cy / (6 * A)];
  }

  function sameXY(a, b) { return a[0] === b[0] && a[1] === b[1]; }

  function rotate(points, cx, cy, angleDeg) {
    const a = (angleDeg * Math.PI) / 180;
    const c = Math.cos(a), s = Math.sin(a);
    return points.map(([x, y]) => {
      const dx = x - cx, dy = y - cy;
      return [cx + dx * c - dy * s, cy + dx * s + dy * c];
    });
  }

  // --- route generation ----------------------------------------------------
  // polygonLL: [[lon,lat],...] closed ring (last == first OK)
  // spacingM:  meters between adjacent sweep lines
  // headingDeg: 0 = lines run N-S, 90 = lines run E-W (boat heading along a line)
  //
  // Returns {route: [[lon,lat],...], totalM, lineCount}.
  function generateRoute(polygonLL, spacingM, headingDeg) {
    if (polygonLL.length < 3 || spacingM <= 0) {
      return { route: [], totalM: 0, lineCount: 0 };
    }
    // Project to meters around polygon centroid (use a quick avg as ref).
    let lon = 0, lat = 0;
    for (const [x, y] of polygonLL) { lon += x; lat += y; }
    lon /= polygonLL.length; lat /= polygonLL.length;
    const proj = projector(lon, lat);

    const ring = polygonLL.map(([lo, la]) => proj.to(lo, la));
    const [cx, cy] = polygonCentroid(ring);

    // Sweep lines are horizontal (along +x) in the rotated frame, which
    // is compass bearing 90. We want the final lines at bearing `heading`,
    // so post-rotate by (90 - heading) in CCW-math; pre-rotate inversely.
    const pre = headingDeg - 90;
    const post = 90 - headingDeg;
    const rot = rotate(ring, cx, cy, pre);

    let minY = +Infinity, maxY = -Infinity;
    for (const [, y] of rot) { if (y < minY) minY = y; if (y > maxY) maxY = y; }

    // Half-spacing inset so coverage is symmetric at the boundary.
    const ys = [];
    for (let y = minY + spacingM / 2; y < maxY; y += spacingM) ys.push(y);

    // For each sweep y, intersect with polygon edges -> sorted x list -> pairs.
    const rows = [];
    const n = rot.length - (sameXY(rot[0], rot[rot.length - 1]) ? 1 : 0);
    for (const y of ys) {
      const xs = [];
      for (let i = 0; i < n; i++) {
        const [x0, y0] = rot[i];
        const [x1, y1] = rot[(i + 1) % n];
        if ((y0 > y) !== (y1 > y)) {   // strict crossing
          const t = (y - y0) / (y1 - y0);
          xs.push(x0 + t * (x1 - x0));
        }
      }
      xs.sort((a, b) => a - b);
      const segs = [];
      for (let k = 0; k + 1 < xs.length; k += 2) {
        segs.push([[xs[k], y], [xs[k + 1], y]]);
      }
      if (segs.length) rows.push(segs);
    }

    // Serpentine across rows; on flipped rows reverse segments + endpoints
    // so the boat doesn't make a U-turn between every pair.
    const pts = [];
    let flip = false;
    for (const segs of rows) {
      const ordered = flip
        ? segs.slice().reverse().map((s) => [s[1], s[0]])
        : segs;
      for (const [a, b] of ordered) {
        pts.push(a);
        pts.push(b);
      }
      flip = !flip;
    }
    // Un-rotate (post) into the real-world meters frame, then un-project.
    const back = rotate(pts, cx, cy, post);
    const route = back.map(([x, y]) => proj.from(x, y));

    // Total meters along route (in projected frame is fine, same units).
    let totalM = 0;
    for (let i = 1; i < back.length; i++) {
      const dx = back[i][0] - back[i - 1][0];
      const dy = back[i][1] - back[i - 1][1];
      totalM += Math.hypot(dx, dy);
    }
    return { route, totalM: Math.round(totalM), lineCount: rows.length };
  }

  // Total length (meters) of an open path [[lon,lat],...], projected around
  // its own average position. Used for the raw line-draw summary.
  function lineLengthM(coordsLL) {
    if (coordsLL.length < 2) return 0;
    let lon = 0, lat = 0;
    for (const [x, y] of coordsLL) { lon += x; lat += y; }
    lon /= coordsLL.length; lat /= coordsLL.length;
    const proj = projector(lon, lat);
    const pts = coordsLL.map(([lo, la]) => proj.to(lo, la));
    let total = 0;
    for (let i = 1; i < pts.length; i++) {
      total += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    }
    return total;
  }

  // --- GPX output (matches your old <trk><trkseg> format) ----------------
  function toGPX(route, name) {
    const safe = (s) =>
      String(s ?? "").replace(/[&<>"']/g,
        (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
                   '"': "&quot;", "'": "&#39;" }[c]));
    const pts = route.map(([lon, lat]) =>
      `      <trkpt lon="${lon.toFixed(8)}" lat="${lat.toFixed(8)}">` +
      `<ele>0</ele></trkpt>`).join("\n");
    return `<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Garmin Sidescan GUI"
     xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>${safe(name)}</name>
    <trkseg>
${pts}
    </trkseg>
  </trk>
</gpx>
`;
  }

  function downloadGPX(filename, text) {
    const blob = new Blob([text], { type: "application/gpx+xml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 0);
  }

  // --- map state -----------------------------------------------------------
  // The shape being drawn (open) and the finalized one.
  let drawing = false;
  let mode = "polygon";        // "polygon" (serpentine fill) | "line" (raw path)
  let drawVerts = [];          // [[lon,lat],...]
  let polygon = [];            // finalized closed ring (polygon mode)
  let line = [];               // finalized open path (line mode)
  let route = [];              // serpentine route (polygon) OR the path itself (line)

  function emptyFC() { return { type: "FeatureCollection", features: [] }; }
  function lineFC(coords) {
    return coords.length < 2 ? emptyFC()
      : { type: "FeatureCollection", features: [
          { type: "Feature", geometry: { type: "LineString", coordinates: coords }, properties: {} },
        ]};
  }
  function polyFC(coords) {
    if (coords.length < 3) return emptyFC();
    const ring = coords[0] === coords[coords.length - 1] || sameXY(coords[0], coords[coords.length - 1])
      ? coords : coords.concat([coords[0]]);
    return { type: "FeatureCollection", features: [
      { type: "Feature", geometry: { type: "Polygon", coordinates: [ring] }, properties: {} },
    ]};
  }

  function ensureSources(map) {
    if (!map.getSource("plan-poly")) {
      map.addSource("plan-poly", { type: "geojson", data: emptyFC() });
      map.addLayer({
        id: "plan-poly-fill", type: "fill", source: "plan-poly",
        paint: { "fill-color": "#ef4444", "fill-opacity": 0.08 },
      });
      map.addLayer({
        id: "plan-poly-line", type: "line", source: "plan-poly",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#ef4444", "line-width": 2, "line-dasharray": [2, 2] },
      });
    }
    if (!map.getSource("plan-verts")) {
      map.addSource("plan-verts", { type: "geojson", data: emptyFC() });
      map.addLayer({
        id: "plan-verts-dot", type: "circle", source: "plan-verts",
        paint: {
          "circle-radius": 4,
          "circle-color": "#ef4444",
          "circle-stroke-color": "#fff",
          "circle-stroke-width": 1.5,
        },
      });
    }
    if (!map.getSource("plan-ghost")) {
      map.addSource("plan-ghost", { type: "geojson", data: emptyFC() });
      map.addLayer({
        id: "plan-ghost-line", type: "line", source: "plan-ghost",
        paint: {
          "line-color": "#ef4444", "line-width": 1.5,
          "line-dasharray": [1, 1.5], "line-opacity": 0.6,
        },
      });
    }
    if (!map.getSource("plan-route")) {
      map.addSource("plan-route", { type: "geojson", data: emptyFC() });
      map.addLayer({
        id: "plan-route-line", type: "line", source: "plan-route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#f59e0b", "line-width": 2.5, "line-opacity": 0.95 },
      });
    }
  }

  function lineFromVerts(verts) {
    if (verts.length < 2) return emptyFC();
    return { type: "FeatureCollection", features: [{
      type: "Feature",
      geometry: { type: "LineString", coordinates: verts },
      properties: {},
    }]};
  }
  function pointsFC(verts) {
    return { type: "FeatureCollection", features: verts.map((c) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: c },
      properties: {},
    }))};
  }

  function setPolygonData(map) {
    // While drawing: show vertices + an OPEN line through them (segments
    // appear from the 2nd click onwards). After finishing in polygon mode:
    // show the closed Polygon (fill + outline). In line mode the finished
    // path is rendered as the orange route instead (see setRouteData), so
    // here we only keep its vertices visible.
    const verts = drawing ? drawVerts : (mode === "line" ? line : polygon);
    let data;
    if (drawing) {
      data = lineFromVerts(verts);
    } else if (mode === "polygon" && verts.length >= 3) {
      data = polyFC(verts);
    } else {
      data = emptyFC();
    }
    map.getSource("plan-poly").setData(data);
    map.getSource("plan-verts").setData(pointsFC(verts));
  }

  function setGhost(map, fromXY, toXY) {
    if (!fromXY || !toXY) {
      map.getSource("plan-ghost").setData(emptyFC());
      return;
    }
    map.getSource("plan-ghost").setData({
      type: "FeatureCollection",
      features: [{
        type: "Feature",
        geometry: { type: "LineString", coordinates: [fromXY, toXY] },
        properties: {},
      }],
    });
  }
  function setRouteData(map) {
    map.getSource("plan-route").setData(lineFC(route));
  }

  // --- UI panel ------------------------------------------------------------
  function buildPanel() {
    if (document.getElementById("plan-panel")) return;
    const el = document.createElement("aside");
    el.id = "plan-panel"; el.hidden = true;
    el.innerHTML = `
      <button id="plan-close" title="close">×</button>
      <h2>Plan survey</h2>
      <p class="muted" style="font-size:12px;margin:0 0 10px">
        Click on the map to add points · double-click to finish · Esc to cancel.
        <br><strong>Polygon</strong> fills the area with survey lines;
        <strong>Line</strong> exports the path you draw as-is.
      </p>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <button id="plan-draw">Draw polygon</button>
        <button id="plan-draw-line">Draw line</button>
        <button id="plan-clear">Clear</button>
      </div>
      <div id="plan-poly-opts">
      <label class="fld">Line spacing (ft)
        <input id="plan-spacing" type="number" value="85" min="1" step="5" style="width:100%">
      </label>
      <div class="fld">Heading (° true) — boat direction along each line
        <div class="compass-row">
          <svg id="plan-compass" viewBox="-60 -60 120 120" width="120" height="120">
            <circle r="58" fill="#f8fafc" stroke="#cbd5e1" stroke-width="1.5" />
            <g id="plan-compass-ticks"></g>
            <text x="0" y="-42" text-anchor="middle" font-size="11" font-weight="700" fill="#0f172a">N</text>
            <text x="44" y="4"  text-anchor="middle" font-size="11" font-weight="700" fill="#475569">E</text>
            <text x="0"  y="49" text-anchor="middle" font-size="11" font-weight="700" fill="#475569">S</text>
            <text x="-44" y="4" text-anchor="middle" font-size="11" font-weight="700" fill="#475569">W</text>
            <g id="plan-needle">
              <polygon points="0,-50 -5,-38 5,-38" fill="#ef4444" />
              <line x1="0" y1="0" x2="0" y2="-38" stroke="#ef4444" stroke-width="3" stroke-linecap="round" />
              <line x1="0" y1="0" x2="0" y2="42" stroke="#94a3b8" stroke-width="2" stroke-linecap="round" />
            </g>
            <circle r="3.5" fill="#0f172a" />
          </svg>
          <div class="compass-side">
            <input id="plan-heading" type="number" value="0" min="0" max="359" step="1">
            <div id="plan-heading-card" class="muted">N · 0°</div>
            <p class="muted" style="font-size:11px;line-height:1.3;margin-top:6px">
              Click or drag on the dial, or type degrees.
            </p>
          </div>
        </div>
      </div>
      </div><!-- /plan-poly-opts -->
      <label class="fld">Plan name (for the GPX file)
        <input id="plan-name" type="text" value="survey" style="width:100%">
      </label>
      <p id="plan-summary" class="muted" style="font-size:12px;margin:8px 0">—</p>
      <button id="plan-download" disabled>Download GPX</button>
    `;
    document.body.appendChild(el);
  }

  // 16-point cardinal name from compass degrees.
  function cardinal(deg) {
    const d = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
               "S","SSW","SW","WSW","W","WNW","NW","NNW"];
    return d[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16];
  }

  function setHeading(rawDeg, map, fromInput) {
    let d = Math.round((((rawDeg % 360) + 360) % 360));
    if (d === 360) d = 0;
    const inp = document.getElementById("plan-heading");
    if (inp && !fromInput) inp.value = d;
    const needle = document.getElementById("plan-needle");
    if (needle) needle.setAttribute("transform", `rotate(${d})`);
    const lbl = document.getElementById("plan-heading-card");
    if (lbl) lbl.textContent = `${cardinal(d)} · ${d}°`;
    regenerate(map);
  }

  function renderCompassTicks() {
    const g = document.getElementById("plan-compass-ticks");
    if (!g || g.childNodes.length) return;
    const out = [];
    for (let i = 0; i < 36; i++) {
      const a = (i * 10) * Math.PI / 180;
      const major = i % 9 === 0;        // N/E/S/W
      const mid   = !major && i % 3 === 0;  // every 30°
      const r2 = major ? 40 : mid ? 44 : 47;
      const x1 = Math.sin(a) * 50, y1 = -Math.cos(a) * 50;
      const x2 = Math.sin(a) * r2,  y2 = -Math.cos(a) * r2;
      const w  = major ? 1.6 : mid ? 1.2 : 0.8;
      const c  = major ? "#0f172a" : "#94a3b8";
      out.push(`<line x1="${x1.toFixed(2)}" y1="${y1.toFixed(2)}"
                       x2="${x2.toFixed(2)}" y2="${y2.toFixed(2)}"
                       stroke="${c}" stroke-width="${w}" />`);
    }
    g.innerHTML = out.join("");
  }

  let _compassBound = false;
  function setupCompass(map) {
    renderCompassTicks();
    const svg = document.getElementById("plan-compass");
    const inp = document.getElementById("plan-heading");
    if (!_compassBound) {
      let dragging = false;
      const bearingFromEvent = (ev) => {
        const r = svg.getBoundingClientRect();
        const dx = ev.clientX - (r.left + r.width / 2);
        const dy = ev.clientY - (r.top + r.height / 2);
        return (Math.atan2(dx, -dy) * 180 / Math.PI + 360) % 360;
      };
      svg.addEventListener("mousedown", (e) => {
        dragging = true; setHeading(bearingFromEvent(e), map);
        e.preventDefault();
      });
      window.addEventListener("mousemove", (e) => {
        if (dragging) setHeading(bearingFromEvent(e), map);
      });
      window.addEventListener("mouseup", () => { dragging = false; });
      inp.addEventListener("input", () => {
        const v = parseFloat(inp.value);
        if (isFinite(v)) setHeading(v, map, true);
      });
      _compassBound = true;
    }
    setHeading(parseFloat(inp.value) || 0, map);
  }

  // Show/hide the polygon-only controls (spacing + heading) and relabel the
  // active draw button so the current mode is obvious.
  function applyModeUI() {
    const opts = document.getElementById("plan-poly-opts");
    if (opts) opts.style.display = mode === "line" ? "none" : "";
    const pBtn = document.getElementById("plan-draw");
    const lBtn = document.getElementById("plan-draw-line");
    if (pBtn) pBtn.classList.toggle("active", mode === "polygon" && (drawing || polygon.length));
    if (lBtn) lBtn.classList.toggle("active", mode === "line" && (drawing || line.length));
  }

  // Line mode: the drawn path IS the deliverable. Mirror it into the route
  // source (orange) and summarize its length.
  function updateLineSummary(map) {
    route = line.slice();
    setRouteData(map);
    const summary = document.getElementById("plan-summary");
    const dl = document.getElementById("plan-download");
    if (line.length < 2) {
      route = []; setRouteData(map);
      summary.textContent = "—";
      dl.disabled = true;
      return;
    }
    const total = lineLengthM(line);
    const distFt = Math.round(total / FT_TO_M);
    const minutes = Math.round(total / 5 / 60);  // assume ~5 m/s = ~10 kts
    summary.textContent =
      `${line.length} pts · ${distFt.toLocaleString()} ft ` +
      `(${Math.round(total).toLocaleString()} m) · ~${minutes} min at 5 m/s`;
    dl.disabled = false;
  }

  function regenerate(map) {
    if (mode === "line") { updateLineSummary(map); return; }
    const spacingFt = parseFloat(document.getElementById("plan-spacing").value);
    const heading = parseFloat(document.getElementById("plan-heading").value);
    if (polygon.length < 3 || !(spacingFt > 0)) {
      route = []; setRouteData(map);
      document.getElementById("plan-summary").textContent = "—";
      document.getElementById("plan-download").disabled = true;
      return;
    }
    const spacingM = spacingFt * FT_TO_M;
    const out = generateRoute(polygon, spacingM, isFinite(heading) ? heading : 0);
    route = out.route; setRouteData(map);
    const total = out.totalM;
    const distFt = Math.round(total / FT_TO_M);
    const minutes = Math.round(total / 5 / 60);  // assume ~5 m/s = ~10 kts
    document.getElementById("plan-summary").textContent =
      out.lineCount
        ? `${out.lineCount} lines · ${distFt.toLocaleString()} ft (${Math.round(total).toLocaleString()} m) · ~${minutes} min at 5 m/s`
        : "polygon too small for this spacing";
    document.getElementById("plan-download").disabled = !out.lineCount;
  }

  // --- draw lifecycle ------------------------------------------------------
  function bindDrawHandlers(map) {
    const onClick = (e) => {
      if (!drawing) return;
      drawVerts.push([e.lngLat.lng, e.lngLat.lat]);
      setPolygonData(map);
      // The ghost now anchors to the new last vertex on the next mousemove.
    };
    const onMove = (e) => {
      if (!drawing || !drawVerts.length) return;
      const last = drawVerts[drawVerts.length - 1];
      setGhost(map, last, [e.lngLat.lng, e.lngLat.lat]);
    };
    const onDbl = (e) => {
      if (!drawing) return;
      e.preventDefault();
      finishDraw(map);
    };
    const onKey = (e) => {
      if (e.key === "Escape" && drawing) {
        drawing = false; drawVerts = []; polygon = []; line = []; route = [];
        setPolygonData(map); setRouteData(map); setGhost(map);
        map.getCanvas().style.cursor = "";
        applyModeUI();
        document.getElementById("plan-summary").textContent = "—";
        document.getElementById("plan-download").disabled = true;
      }
    };
    map.on("click", onClick);
    map.on("mousemove", onMove);
    map.on("dblclick", onDbl);
    window.addEventListener("keydown", onKey);
  }

  function startDraw(map, drawMode) {
    mode = drawMode || "polygon";
    drawing = true; drawVerts = []; polygon = []; line = []; route = [];
    applyModeUI();
    setPolygonData(map); setRouteData(map);
    map.doubleClickZoom.disable();
    map.getCanvas().style.cursor = "crosshair";
  }

  function finishDraw(map) {
    drawing = false;
    map.doubleClickZoom.enable();
    map.getCanvas().style.cursor = "";
    setGhost(map);
    const min = mode === "line" ? 2 : 3;
    if (drawVerts.length >= min) {
      if (mode === "line") line = drawVerts.slice();
      else polygon = drawVerts.slice();
      setPolygonData(map);
      regenerate(map);
    } else {
      drawVerts = []; setPolygonData(map);
    }
    applyModeUI();
  }

  // --- public API ----------------------------------------------------------
  function open(map) {
    ensureSources(map);
    buildPanel();
    const panel = document.getElementById("plan-panel");
    panel.hidden = false;
    document.getElementById("plan-close").onclick = () => close(map);
    document.getElementById("plan-draw").onclick = () => startDraw(map, "polygon");
    document.getElementById("plan-draw-line").onclick = () => startDraw(map, "line");
    document.getElementById("plan-clear").onclick = () => {
      drawVerts = []; polygon = []; line = []; route = [];
      setPolygonData(map); setRouteData(map); setGhost(map);
      applyModeUI();
      document.getElementById("plan-summary").textContent = "—";
      document.getElementById("plan-download").disabled = true;
    };
    document.getElementById("plan-spacing").oninput = () => regenerate(map);
    setupCompass(map);
    applyModeUI();
    document.getElementById("plan-download").onclick = () => {
      const name = document.getElementById("plan-name").value || "survey";
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      downloadGPX(`${name}_${stamp}.gpx`, toGPX(route, name));
    };
    if (!map._planBound) {
      bindDrawHandlers(map);
      map._planBound = true;
    }
  }

  function close(map) {
    const panel = document.getElementById("plan-panel");
    if (panel) panel.hidden = true;
    if (drawing) {
      drawing = false;
      map.doubleClickZoom.enable();
      map.getCanvas().style.cursor = "";
    }
    drawVerts = []; polygon = []; line = []; route = [];
    if (map.getSource("plan-poly")) setPolygonData(map);
    if (map.getSource("plan-route")) setRouteData(map);
    if (map.getSource("plan-ghost")) setGhost(map);
  }

  window.SurveyPlan = { open, close };
})();
