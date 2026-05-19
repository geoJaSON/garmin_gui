"""Build the plain-text per-deliverable metadata report (Phase 7c)."""

from __future__ import annotations

import datetime as _dt
from typing import Iterable


def _line(label: str, value, unit: str = "") -> str:
    if value is None or value == "":
        value = "—"
    return f"  {label:<22} : {value}{(' ' + unit) if unit and value != '—' else ''}"


def _fmt_num(v, prec: int = 1):
    if v is None:
        return None
    try:
        return f"{float(v):.{prec}f}"
    except Exception:
        return str(v)


def build_metadata_txt(area: dict, runs: Iterable[dict],
                       buffer_m: float, mode: str, cog_path: str) -> str:
    """Compose the human-readable .txt that ships with a deliverable.

    `area` is a row from the areas table; `runs` is an iterable of mosaic
    job dicts (db.get_job results) for the contributing tracks.
    """
    props = area.get("properties") or {}
    name = area.get("our_name") or "—"
    app_no = area.get("tpwd_app_no") or "—"
    notes = area.get("notes") or ""
    ft = round(buffer_m / 0.3048)

    parts = []
    parts.append("Garmin Sidescan Survey Report")
    parts.append("=" * 60)
    parts.append(f"Area              : {name}  (TPWD App No {app_no})")
    parts.append(f"Generated         : {_dt.datetime.utcnow():%Y-%m-%d %H:%M} UTC")
    parts.append(f"Mode              : {mode}")
    parts.append(f"Buffer applied    : {ft} ft ({buffer_m:.2f} m)")
    parts.append(f"COG               : {cog_path}")
    parts.append("")

    parts.append("Area properties")
    parts.append("-" * 60)
    for k in ("Status", "Bay System", "Acreage", "Applicant/Company Name",
              "Applicant", "Corner Coordinates", "Comments", "sheet", "label"):
        if k in props:
            parts.append(_line(k, props[k]))
    parts.append(_line("Notes", notes or "—"))
    parts.append("")

    run_list = list(runs)
    parts.append(f"Contributing runs ({len(run_list)})")
    parts.append("-" * 60)
    if not run_list:
        parts.append("  (none — no intersecting completed mosaics)")
    for i, j in enumerate(run_list, 1):
        res = j.get("result") or {}
        params = j.get("params") or {}
        rsd = (res.get("rsd_name")
               or (params.get("rsd_path") or "").rsplit("/", 1)[-1]
               or "unknown.RSD")
        sd = res.get("survey_datetime") or "—"
        parts.append(f"  {i}. {rsd}   ({sd})")
        m = res.get("meta") or {}
        depth = m.get("depth_m") or {}
        rng = m.get("range_m") or {}
        unit = m.get("unit") or {}
        if m.get("ping_count") is not None:
            dur = m.get("duration_s")
            dur_s = f"{int(round(dur / 60))} min" if dur else "—"
            parts.append(
                f"     pings: {m['ping_count']:,}   duration: {dur_s}"
                f"   utm: {m.get('utm_zone', '—')}"
            )
        if depth:
            parts.append(
                f"     depth (m): mean {_fmt_num(depth.get('mean'),2)}, "
                f"min {_fmt_num(depth.get('min'),2)}, "
                f"max {_fmt_num(depth.get('max'),2)}"
            )
        if rng:
            parts.append(
                f"     range (m): mean {_fmt_num(rng.get('mean'),1)}, "
                f"max {_fmt_num(rng.get('max'),1)}"
            )
        if unit:
            bits = []
            if unit.get("product_number"): bits.append(f"product {unit['product_number']}")
            if unit.get("software_version"): bits.append(f"sw {unit['software_version']}")
            if unit.get("channel_count"): bits.append(f"{unit['channel_count']} ch")
            if bits:
                parts.append("     Garmin: " + " · ".join(bits))
        w = res.get("weather") or {}
        if w:
            bits = []
            t = _fmt_num(w.get("temperature_2m_mean_c"), 1)
            if t: bits.append(f"air {t} °C")
            ws = _fmt_num(w.get("wind_speed_max_ms"), 1)
            wg = _fmt_num(w.get("wind_gusts_max_ms"), 1)
            if ws or wg:
                bits.append(f"wind {ws or '—'} m/s (gust {wg or '—'})")
            wh = _fmt_num(w.get("wave_height_max_m"), 2)
            if wh: bits.append(f"wave {wh} m")
            sst = _fmt_num(w.get("sea_surface_temperature_mean_c"), 1)
            if sst: bits.append(f"sst {sst} °C")
            precip = _fmt_num(w.get("precipitation_sum_mm"), 1)
            if precip: bits.append(f"rain {precip} mm")
            if bits:
                parts.append("     Weather: " + ", ".join(bits))
        else:
            parts.append("     Weather: unavailable")

    parts.append("")
    parts.append("End of report")
    return "\n".join(parts) + "\n"
