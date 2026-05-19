"""GeoTIFF -> Cloud-Optimized GeoTIFF, so TiTiler can tile it efficiently.

Converted on write (once per run) rather than on demand: a run happens once,
but the map will request many tiles from it.
"""

from __future__ import annotations

from pathlib import Path


def to_cog(src: Path, dst: Path) -> Path:
    """Rewrite `src` as a COG at `dst`. Returns dst."""
    from rio_cogeo.cogeo import cog_translate
    from rio_cogeo.profiles import cog_profiles

    dst.parent.mkdir(parents=True, exist_ok=True)
    cog_translate(
        str(src),
        str(dst),
        cog_profiles.get("deflate"),
        in_memory=False,
        quiet=True,
        web_optimized=True,
    )
    return dst
