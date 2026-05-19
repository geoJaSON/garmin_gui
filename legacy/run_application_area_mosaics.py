"""
Select Garmin RSD tracks that intersect application areas and build mosaics.

The script reads an existing RSD track inventory GeoJSON, filters tracks that
intersect any polygon in COL_Application_areas.geojson, writes a filtered
GeoJSON for review, and then runs garmin_mosaic.py for each matching track.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

from shapely.geometry import shape
from shapely.prepared import prep


# --- Edit these before running ---
INPUT_FOLDER = Path(r"C:\Users\jason\Documents\RSD_FILES")
TRACKS_PATH = None  # None -> <INPUT_FOLDER>/rsd_tracks.geojson
APPLICATION_AREAS_PATH = Path(__file__).with_name("col_buffered.geojson")
MATCHED_TRACKS_OUTPUT = None  # None -> <TRACKS_PATH stem>_application_area_matches.geojson
MOSAIC_SCRIPT_PATH = Path(__file__).with_name("garmin_mosaic.py")
LOG_DIR = None  # None -> <repo>/mosaic_logs
RUN_MOSAICS = True
SKIP_COMPLETED_MOSAICS = True

REQUIRED_MOSAIC_OUTPUTS = (
    "intensity.tif",
)


def resolve_tracks_path():
    return Path(TRACKS_PATH).expanduser().resolve() if TRACKS_PATH else (INPUT_FOLDER / "rsd_tracks.geojson").resolve()


def resolve_matched_output_path(tracks_path: Path):
    if MATCHED_TRACKS_OUTPUT:
        return Path(MATCHED_TRACKS_OUTPUT).expanduser().resolve()
    return tracks_path.with_name(f"{tracks_path.stem}_application_area_matches.geojson")


def resolve_log_dir():
    if LOG_DIR:
        return Path(LOG_DIR).expanduser().resolve()
    return Path(__file__).resolve().parent / "mosaic_logs"


def load_feature_collection(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("type") != "FeatureCollection":
        raise SystemExit(f"Expected a GeoJSON FeatureCollection: {path}")
    return payload


def write_feature_collection(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(path)


def load_application_areas(path: Path):
    payload = load_feature_collection(path)
    areas = []
    for feature in payload.get("features", []):
        geometry = feature.get("geometry")
        if not geometry:
            continue
        area_geom = shape(geometry)
        if area_geom.is_empty:
            continue
        props = feature.get("properties", {})
        areas.append(
            {
                "geometry": area_geom,
                "prepared": prep(area_geom),
                "name": props.get("Our_Name") or props.get("Name") or f"feature_{feature.get('id', len(areas) + 1)}",
                "object_id": props.get("OBJECTID"),
            }
        )
    return areas


def select_matching_tracks(tracks_payload, areas):
    matched_features = []

    for feature in tracks_payload.get("features", []):
        geometry = feature.get("geometry")
        if not geometry:
            continue

        track_geom = shape(geometry)
        if track_geom.is_empty:
            continue

        matches = []
        for area in areas:
            if area["prepared"].intersects(track_geom):
                matches.append(area)

        if not matches:
            continue

        matched_feature = copy.deepcopy(feature)
        props = matched_feature.setdefault("properties", {})
        props["application_area_names"] = sorted({m["name"] for m in matches})
        props["application_area_ids"] = sorted({m["object_id"] for m in matches if m["object_id"] is not None})
        props["application_area_count"] = len(matches)
        matched_features.append(matched_feature)

    return {
        "type": "FeatureCollection",
        "features": matched_features,
    }


def get_python_executable():
    venv_python = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return venv_python
    return Path(sys.executable).resolve()


def get_output_paths(rsd_file: Path):
    output_base_dir = rsd_file.parent / f"garmin_output_{rsd_file.stem}"
    processed_dir = output_base_dir / "processed"
    return output_base_dir, processed_dir


def mosaic_outputs_exist(processed_dir: Path):
    return all((processed_dir / name).exists() for name in REQUIRED_MOSAIC_OUTPUTS)


def run_mosaic_for_track(rsd_file: Path, python_exe: Path, mosaic_script: Path, log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{rsd_file.stem}.log"

    env = os.environ.copy()
    env["GARMIN_RSD_FILE"] = str(rsd_file)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    with log_path.open("w", encoding="utf-8") as log_handle:
        log_handle.write(f"RSD file: {rsd_file}\n")
        log_handle.write(f"Mosaic script: {mosaic_script}\n\n")
        completed = subprocess.run(
            [str(python_exe), str(mosaic_script)],
            cwd=str(mosaic_script.parent),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    return completed.returncode, log_path


def main():
    tracks_path = resolve_tracks_path()
    matched_output_path = resolve_matched_output_path(tracks_path)
    application_areas_path = APPLICATION_AREAS_PATH.expanduser().resolve()
    mosaic_script_path = MOSAIC_SCRIPT_PATH.expanduser().resolve()
    log_dir = resolve_log_dir()

    if not tracks_path.exists():
        raise SystemExit(f"Track inventory not found: {tracks_path}")
    if not application_areas_path.exists():
        raise SystemExit(f"Application areas file not found: {application_areas_path}")
    if not mosaic_script_path.exists():
        raise SystemExit(f"Mosaic script not found: {mosaic_script_path}")

    print(f"Loading track inventory: {tracks_path}")
    tracks_payload = load_feature_collection(tracks_path)
    print(f"Loading application areas: {application_areas_path}")
    areas = load_application_areas(application_areas_path)
    print(f"Loaded {len(areas)} application area(s)")

    matched_payload = select_matching_tracks(tracks_payload, areas)
    write_feature_collection(matched_output_path, matched_payload)
    print(f"Wrote {len(matched_payload['features'])} matching track(s) to {matched_output_path}")

    if not RUN_MOSAICS:
        return

    python_exe = get_python_executable()
    print(f"Using Python: {python_exe}")

    completed = 0
    skipped = 0
    failed = []

    for feature in matched_payload["features"]:
        props = feature.get("properties", {})
        rsd_path_value = props.get("file_path")
        if not rsd_path_value:
            continue

        rsd_file = Path(rsd_path_value).expanduser().resolve()
        _, processed_dir = get_output_paths(rsd_file)

        if SKIP_COMPLETED_MOSAICS and mosaic_outputs_exist(processed_dir):
            print(f"Skipping {rsd_file.name} (mosaic outputs already exist)")
            skipped += 1
            continue

        print(f"Running mosaic for {rsd_file.name}...")
        returncode, log_path = run_mosaic_for_track(rsd_file, python_exe, mosaic_script_path, log_dir)
        if returncode == 0 and mosaic_outputs_exist(processed_dir):
            completed += 1
            print(f"  Completed. Log: {log_path}")
        else:
            failed.append((rsd_file.name, returncode, log_path))
            print(f"  Failed with exit code {returncode}. Log: {log_path}")

    print(f"Completed mosaics: {completed}")
    print(f"Skipped existing mosaics: {skipped}")
    if failed:
        print(f"Failed mosaics: {len(failed)}")
        for file_name, returncode, log_path in failed:
            print(f"  - {file_name}: exit code {returncode}, log {log_path}")


if __name__ == "__main__":
    main()
