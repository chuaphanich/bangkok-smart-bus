"""Real Bangkok transit data loaders (GTFS, OSM, MOT, traffic)."""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
CACHE_DIR = PACKAGE_DIR / "cache"
PROJECT_ROOT = PACKAGE_DIR.parent

CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Preferred BMTA line refs (legacy numbering still used on many maps).
PREFERRED_REFS = ("8", "29", "39", "509", "554")

ROUTE_COLORS = {
    "8": "#E85D04",
    "29": "#2A9D8F",
    "39": "#4C6EF5",
    "509": "#D62828",
    "554": "#9B5DE5",
}

BANGKOK_BBOX = {
    "south": 13.45,
    "west": 100.30,
    "north": 14.05,
    "east": 100.95,
}
