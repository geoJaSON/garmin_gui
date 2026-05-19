"""Summarize per-RSD survey metadata from the pingverter CSVs.

The pipeline / tracks job leaves a `meta/` directory beside each RSD with
several CSVs. This module reads the relevant columns and returns small
summary stats (depth, range, ping count, Garmin unit info) suitable for
display in the track-click panel.

Column names vary across pingverter versions, so each field is looked up
defensively — missing fields are simply omitted from the result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd


def _stats(series) -> Optional[dict]:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return None
    return {
        "min": round(float(s.min()), 2),
        "mean": round(float(s.mean()), 2),
        "max": round(float(s.max()), 2),
    }


def _first_present_col(df: pd.DataFrame, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


def summarize_meta_dir(meta_dir: Path) -> Optional[dict]:
    """Return a small summary dict for one RSD, or None if no meta found."""
    meta_dir = Path(meta_dir)
    if not meta_dir.is_dir():
        return None

    out: dict = {}

    # --- All-Garmin-Sonar-MetaData.csv -> ping/depth/range stats -----------
    all_meta = meta_dir / "All-Garmin-Sonar-MetaData.csv"
    if all_meta.exists():
        try:
            df = pd.read_csv(all_meta)
        except Exception:
            df = None
        if df is not None and len(df):
            out["ping_count"] = int(len(df))
            depth_col = _first_present_col(
                df, ["inst_dep_m", "depth_m", "depth", "bottom_depth"]
            )
            if depth_col:
                out["depth_m"] = _stats(df[depth_col])
            range_col = _first_present_col(
                df, ["max_range", "max_range_m", "range_m"]
            )
            if range_col:
                out["range_m"] = _stats(df[range_col])
            if "utm_zone" in df.columns:
                try:
                    out["utm_zone"] = int(
                        df["utm_zone"].dropna().mode().iloc[0]
                    )
                except Exception:
                    pass
            # Crude duration estimate when timestamps exist.
            ts_col = _first_present_col(
                df, ["timestamp", "time", "unix_time", "first_byte"]
            )
            if ts_col:
                try:
                    ts = pd.to_numeric(df[ts_col], errors="coerce").dropna()
                    if len(ts) > 1:
                        out["duration_s"] = round(float(ts.max() - ts.min()), 1)
                except Exception:
                    pass

    # --- DAT_meta.csv -> Garmin unit identity ------------------------------
    dat = meta_dir / "DAT_meta.csv"
    if dat.exists():
        try:
            df = pd.read_csv(dat)
        except Exception:
            df = None
        if df is not None and len(df):
            row = df.iloc[0]
            unit = {}
            for k in ("product_number", "unit_id", "software_version",
                     "channel_count", "unit_id_type"):
                if k in df.columns:
                    v = row.get(k)
                    if pd.notna(v):
                        unit[k] = str(v)
            if unit:
                out["unit"] = unit

    return out or None
