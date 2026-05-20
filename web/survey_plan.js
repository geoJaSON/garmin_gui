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
  // The polygon being drawn (open) and the finalized one.
  let drawing = false;
  let drawVerts = [];          // [[lon,lat],...]
  let polygon = [];            // finalized closed ring
  let route = [];

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
        paint: { "line-color": "#ef4444", "line-width": 2, "line-dasharray": [2, 2] },
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

  function setPolygonData(map) {
    const data = drawing
      ? polyFC(drawVerts)
      : (polygon.length ? polyFC(polygon) : emptyFC());
    map.getSource("plan-poly").setData(data);
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
        Click on the map to add vertices · double-click to finish · Esc to cancel.
      </p>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <button id="plan-draw">Draw polygon</button>
        <button id="plan-clear">Clear</button>
      </div>
      <label class="fld">Line spacing (ft)
        <input id="plan-spacing" type="number" value="50" min="1" step="5" style="width:100%">
      </label>
      <label class="fld">Heading (° true) — boat direction along each line
        <input id="plan-heading" type="number" value="0" min="0" max="359" step="5" style="width:100%">
      </label>
      <label class="fld">Plan name (for the GPX file)
        <input id="plan-name" type="text" value="survey" style="width:100%">
      </label>
      <p id="plan-summary" class="muted" style="font-size:12px;margin:8px 0">—</p>
      <button id="plan-download" disabled>Download GPX</button>
    `;
    document.body.appendChild(el);
  }

  function regenerate(map) {
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
    };
    const onDbl = (e) => {
      if (!drawing) return;
      e.preventDefault();
      finishDraw(map);
    };
    const onKey = (e) => {
      if (e.key === "Escape" && drawing) {
        drawing = false; drawVerts = []; polygon = []; route = [];
        setPolygonData(map); setRouteData(map);
        map.getCanvas().style.cursor = "";
        document.getElementById("plan-summary").textContent = "—";
        document.getElementById("plan-download").disabled = true;
      }
    };
    map.on("click", onClick);
    map.on("dblclick", onDbl);
    window.addEventListener("keydown", onKey);
  }

  function startDraw(map) {
    drawing = true; drawVerts = []; polygon = []; route = [];
    setPolygonData(map); setRouteData(map);
    map.doubleClickZoom.disable();
    map.getCanvas().style.cursor = "crosshair";
  }

  function finishDraw(map) {
    drawing = false;
    map.doubleClickZoom.enable();
    map.getCanvas().style.cursor = "";
    if (drawVerts.length >= 3) {
      polygon = drawVerts.slice();
      setPolygonData(map);
      regenerate(map);
    } else {
      drawVerts = []; setPolygonData(map);
    }
  }

  // --- public API ----------------------------------------------------------
  function open(map) {
    ensureSources(map);
    buildPanel();
    const panel = document.getElementById("plan-panel");
    panel.hidden = false;
    document.getElementById("plan-close").onclick = () => close(map);
    document.getElementById("plan-draw").onclick = () => startDraw(map);
    document.getElementById("plan-clear").onclick = () => {
      drawVerts = []; polygon = []; route = [];
      setPolygonData(map); setRouteData(map);
      document.getElementById("plan-summary").textContent = "—";
      document.getElementById("plan-download").disabled = true;
    };
    document.getElementById("plan-spacing").oninput = () => regenerate(map);
    document.getElementById("plan-heading").oninput = () => regenerate(map);
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
    drawVerts = []; polygon = []; route = [];
    if (map.getSource("plan-poly")) setPolygonData(map);
    if (map.getSource("plan-route")) setRouteData(map);
  }

  window.SurveyPlan = { open, close };
})();
