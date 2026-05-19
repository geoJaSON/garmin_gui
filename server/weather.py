"""Open-Meteo historical weather + marine fetch, with on-disk cache.

A single daily summary per (lat, lon, date) is plenty for survey
documentation; the cached file is keyed by rounded coords + date so a
re-run of the same day in the same area doesn't re-hit the network.
Failures are non-fatal: callers get None and continue.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .settings import DATA_DIR

WEATHER_DIR = DATA_DIR / "weather"

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG",
     "SEP", "OCT", "NOV", "DEC"]
)}
# Matches names like "08MAY26-1446-01" or "24APR26-1329-01".
_RSD_RE = re.compile(
    r"^(?P<day>\d{1,2})(?P<mon>[A-Z]{3})(?P<year>\d{2,4})-(?P<hhmm>\d{4})",
    re.IGNORECASE,
)


def parse_rsd_datetime(name: str) -> Optional[_dt.datetime]:
    """Parse a Garmin RSD stem like '08MAY26-1446-01' -> 2026-05-08 14:46."""
    m = _RSD_RE.match(Path(name).stem.upper())
    if not m:
        return None
    try:
        d = int(m["day"])
        mon = _MONTHS.get(m["mon"].upper())
        if not mon:
            return None
        y = int(m["year"])
        if y < 100:
            y += 2000  # "26" -> 2026
        hh = int(m["hhmm"][:2])
        mm = int(m["hhmm"][2:])
        return _dt.datetime(y, mon, d, hh, mm)
    except Exception:
        return None


def _fetch_json(url: str, timeout: float = 15.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _daily0(payload: Optional[dict], key: str):
    try:
        return payload["daily"][key][0]
    except Exception:
        return None


def fetch_daily(lat: float, lon: float, date: _dt.date) -> Optional[dict]:
    """Return a small daily weather+marine summary; cached on disk.

    None only if both the weather and marine fetches fail.
    """
    WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    date_str = date.isoformat()
    cache = WEATHER_DIR / f"{round(lat, 3)}_{round(lon, 3)}_{date_str}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass

    common = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "start_date": date_str,
        "end_date": date_str,
        "timezone": "auto",
    }
    w_url = (
        "https://archive-api.open-meteo.com/v1/archive?"
        + urllib.parse.urlencode({
            **common,
            "wind_speed_unit": "ms",   # Open-Meteo defaults to km/h
            "daily": ",".join([
                "temperature_2m_mean",
                "wind_speed_10m_max",
                "wind_gusts_10m_max",
                "wind_direction_10m_dominant",
                "precipitation_sum",
                "cloud_cover_mean",
            ]),
        })
    )
    m_url = (
        "https://marine-api.open-meteo.com/v1/marine?"
        + urllib.parse.urlencode({
            **common,
            "daily": ",".join([
                "wave_height_max",
                "wave_period_max",
                "wave_direction_dominant",
                "sea_surface_temperature_mean",
            ]),
        })
    )
    w = _fetch_json(w_url)
    mar = _fetch_json(m_url)
    if not w and not mar:
        return None

    out = {
        "date": date_str,
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "temperature_2m_mean_c": _daily0(w, "temperature_2m_mean"),
        "wind_speed_max_ms": _daily0(w, "wind_speed_10m_max"),
        "wind_gusts_max_ms": _daily0(w, "wind_gusts_10m_max"),
        "wind_direction_dominant_deg": _daily0(w, "wind_direction_10m_dominant"),
        "precipitation_sum_mm": _daily0(w, "precipitation_sum"),
        "cloud_cover_mean_pct": _daily0(w, "cloud_cover_mean"),
        "wave_height_max_m": _daily0(mar, "wave_height_max"),
        "wave_period_max_s": _daily0(mar, "wave_period_max"),
        "wave_direction_dominant_deg": _daily0(mar, "wave_direction_dominant"),
        "sea_surface_temperature_mean_c":
            _daily0(mar, "sea_surface_temperature_mean"),
    }
    try:
        cache.write_text(json.dumps(out))
    except Exception:
        pass
    return out
