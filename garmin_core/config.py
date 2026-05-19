"""
MosaicConfig — the ~45 tuning constants that used to live at the top of
garmin_mosaic.py, lifted into one typed, serializable object.

Defaults are byte-faithful to legacy/garmin_mosaic.py (lines 46-127). The
processing pipeline still reads these as module globals; MosaicConfig.to_globals()
is what run_mosaic() injects so behavior is identical to the original script
when constructed with defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Optional, List, Set

# Payload decoding mode tables (also config, but list-valued).
DEFAULT_PAYLOAD_MODES: List[str] = [
    "u8_tail", "u8_even", "u8_odd", "u16_le", "u16_be", "u16_le_hi", "u16_le_lo",
    "son_u8", "son_even", "son_odd", "son_u16_le", "son_u16_be",
]
DEFAULT_PAYLOAD_EXTRA_MODES: List[str] = [
    "u8_tail_s16", "u8_even_s16", "u8_odd_s16", "u16_le_s16", "u16_be_s16",
    "u8_tail_s23", "u8_even_s23", "u8_odd_s23", "u16_le_s23", "u16_be_s23",
    "u8_tail_s24", "u8_even_s24", "u8_odd_s24", "u16_le_s24", "u16_be_s24",
    "u8_tail_s32", "u8_even_s32", "u8_odd_s32", "u16_le_s32", "u16_be_s32",
]


@dataclass
class MosaicConfig:
    """All tunable parameters for a single-RSD mosaic run.

    Field names are intentionally the original UPPERCASE constant names so
    to_globals() is a trivial, auditable 1:1 injection.
    """

    # --- Processing parameters ---
    OUTPUT_RESOLUTION: float = 0.05            # meters/pixel (5 cm)
    TEXTURE_WINDOW_SIZE: int = 15              # texture window (~1.5 m at 5 cm)
    MAX_RANGE_FALLBACK: Optional[float] = None
    PORT_MAX_RANGE_OVERRIDE_M: Optional[float] = None
    STARBOARD_MAX_RANGE_OVERRIDE_M: Optional[float] = None
    PORT_RANGE_SCALE: float = 1.0
    STARBOARD_RANGE_SCALE: float = 1.0

    # --- Radiometric corrections ---
    APPLY_TVG: bool = True
    TVG_SPREADING_DB: float = 30.0            # spreading loss (20-40 dB typical)
    ABSORPTION_COEFF: float = 0.05            # dB/m (0.03-0.06 fresh, 0.1-0.2 salt)
    APPLY_DESPECKLE: bool = True
    DESPECKLE_SIZE: int = 3                   # median kernel (3x3)
    APPLY_LEE_FILTER: bool = True
    LEE_WINDOW_SIZE: int = 7

    # --- Navigation ---
    HEADING_SMOOTH_WINDOW: int = 100          # pings
    MIN_SPEED_MS: float = 0.3                 # m/s; 0 disables

    # --- Gap filling ---
    FILL_GAPS: bool = True
    MAX_FILL_DISTANCE: float = 2.0            # meters
    OVERLAP_POLICY: str = "first"             # "first" | "last"

    # --- Nadir zone ---
    NADIR_MASK_BINS: int = 2
    NADIR_ALTITUDE_FACTOR: float = 0.0

    # --- Downscan-based nadir fill ---
    APPLY_DOWNSCAN_NADIR_FILL: bool = True
    DOWNSCAN_META_NAME: str = "B001_ds_hifreq_meta.csv"
    DOWNSCAN_STRIP_WIDTH_M: float = 0.6
    DOWNSCAN_BOTTOM_WINDOW_M: float = 0.25
    DOWNSCAN_PAYLOAD_MODE_OVERRIDE: str = "auto"

    # --- Raster gap fill ---
    GAP_FILL_PASSES: int = 3

    # --- Contrast stretch & color calibration ---
    STRETCH_LOW_PCT: int = 2
    STRETCH_HIGH_PCT: int = 98
    APPLY_LOG_COMPRESSION: bool = True
    LOG_COMPRESSION_SCALE: float = 6.0
    FIXED_STRETCH_LO: Optional[float] = None
    FIXED_STRETCH_HI: Optional[float] = None

    # --- Record/payload decoding ---
    PAYLOAD_PROBE_PINGS: int = 150
    FALLBACK_SAMPLE_COUNT: int = 2048
    PAYLOAD_MODES: List[str] = field(default_factory=lambda: list(DEFAULT_PAYLOAD_MODES))
    PAYLOAD_EXTRA_MODES: List[str] = field(default_factory=lambda: list(DEFAULT_PAYLOAD_EXTRA_MODES))
    PAYLOAD_MODE_OVERRIDE_PORT: str = "son_u16_le"
    PAYLOAD_MODE_OVERRIDE_STARBOARD: str = "son_u16_le"

    # --- Side-specific sample ordering ---
    REVERSE_PORT_SAMPLES: bool = False
    REVERSE_STARBOARD_SAMPLES: bool = False
    PORT_HEADING_OFFSET_DEG: float = 0.0
    STARBOARD_HEADING_OFFSET_DEG: float = 0.0
    USE_PORT_CHANNEL_OVERRIDE: bool = True
    PORT_CHANNEL_OVERRIDE_ID: int = 1

    # --- EGN stabilization ---
    EGN_SMOOTH_WINDOW: int = 250
    EGN_GAIN_MIN: float = 0.45
    EGN_GAIN_MAX: float = 2.20

    # --- Ping-to-ping leveling ---
    APPLY_ROW_BALANCE: bool = False

    # --- Output selection ---
    # intensity.tif is always written. The per-side and texture rasters are
    # rarely used downstream, so they default OFF (saves write time + disk).
    # This is an intentional behavior change from legacy/garmin_mosaic.py,
    # which always wrote all four.
    SAVE_PORT_INTENSITY: bool = False
    SAVE_STAR_INTENSITY: bool = False
    SAVE_TEXTURE: bool = False

    def to_globals(self) -> dict:
        """Return {CONSTANT_NAME: value} for injection into the pipeline module.

        Lists are copied so a run cannot mutate the config instance.
        """
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = list(v) if isinstance(v, list) else v
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "MosaicConfig":
        """Build from a (possibly partial) dict, ignoring unknown keys."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


DEFAULT_REQUIRED_META_COLUMNS: Set[str] = {"index", "lon", "lat", "e", "n", "utm_zone"}


@dataclass
class TracksConfig:
    """Tuning for the RSD track-inventory builder.

    Defaults are byte-faithful to legacy/rsd_tracks_to_geojson.py (lines 22-28).
    INPUT_FOLDER / OUTPUT_PATH are NOT here — they are call arguments to
    build_track_inventory(), not tuning knobs.
    """

    SCAN_SUBFOLDERS: bool = True
    FORCE_REGENERATE_METADATA: bool = False
    TRACK_ONLY_MODE: bool = True       # skip full sonar metadata; GPS/nav only
    SKIP_TRACKS_ALREADY_WRITTEN: bool = True
    MAX_TRACK_POINTS: Optional[int] = 2000  # None keeps every point
    REQUIRED_META_COLUMNS: Set[str] = field(
        default_factory=lambda: set(DEFAULT_REQUIRED_META_COLUMNS)
    )

    def to_globals(self) -> dict:
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = set(v) if isinstance(v, set) else v
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "TracksConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
