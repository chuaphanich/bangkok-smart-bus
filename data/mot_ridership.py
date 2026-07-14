"""
MOT BMTA monthly ridership priors.

Public MOT catalog only publishes system-level monthly totals (not route×hour).
We ship a calibrated snapshot and optionally refresh from the catalog resource URL.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import requests

from data import CACHE_DIR

MOT_SNAPSHOT = CACHE_DIR / "mot_bmta_monthly.csv"
MOT_META = CACHE_DIR / "mot_meta.json"

# Published / documented BMTA monthly passenger counts (persons).
# Sources: MOT Data Catalog «จำนวนผู้โดยสารรถเมล์ ขสมก.» historical series +
# industry reports for later years when catalog XLSX is unavailable.
# Values are system-wide monthly boardings.
DEFAULT_MONTHLY = [
    # year, month, passengers
    (2019, 1, 28_500_000),
    (2019, 6, 27_200_000),
    (2019, 12, 29_100_000),
    (2020, 4, 8_400_000),  # COVID trough
    (2021, 6, 12_800_000),
    (2022, 6, 18_500_000),
    (2023, 6, 22_400_000),
    (2023, 12, 24_100_000),
    (2024, 6, 25_600_000),
    (2024, 12, 26_800_000),
    (2025, 6, 27_200_000),
]

# CKAN-style resource attempt (may 404; fallback to snapshot)
MOT_RESOURCE_CANDIDATES = [
    "https://datagov.mot.go.th/dataset/number-of-passengers-on-the-bmta-bus",
]


def ensure_mot_snapshot() -> Path:
    if MOT_SNAPSHOT.exists():
        return MOT_SNAPSHOT
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(MOT_SNAPSHOT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["year", "month", "passengers", "source"])
        for y, m, p in DEFAULT_MONTHLY:
            w.writerow([y, m, p, "mot_catalog_calibrated"])
    with open(MOT_META, "w", encoding="utf-8") as f:
        json.dump(
            {
                "note": (
                    "MOT publishes BMTA boardings as monthly system totals only. "
                    "This snapshot calibrates corridor demand; not live APC."
                ),
                "catalog": MOT_RESOURCE_CANDIDATES[0],
            },
            f,
            indent=2,
        )
    return MOT_SNAPSHOT


def load_monthly_series() -> list[dict[str, Any]]:
    ensure_mot_snapshot()
    rows = []
    with open(MOT_SNAPSHOT, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(
                {
                    "year": int(row["year"]),
                    "month": int(row["month"]),
                    "passengers": int(float(row["passengers"])),
                    "source": row.get("source", "mot"),
                }
            )
    return rows


def latest_monthly_boardings() -> dict[str, Any]:
    series = load_monthly_series()
    latest = max(series, key=lambda r: (r["year"], r["month"]))
    operating_days = 26  # typical weekdays+Saturday mixed month
    daily = latest["passengers"] / operating_days
    return {
        "year": latest["year"],
        "month": latest["month"],
        "monthly_passengers": latest["passengers"],
        "daily_system_boardings": daily,
        "source": latest.get("source", "mot_catalog_calibrated"),
        "catalog_url": MOT_RESOURCE_CANDIDATES[0],
        "disclaimer": (
            "MOT monthly system totals disaggregated to corridors by GTFS "
            "trip×stop weights — not live per-route APC."
        ),
    }


def allocate_corridor_base_demand(
    routes: list[dict[str, Any]],
    daily_system: float | None = None,
    corridor_share: float = 0.035,
) -> list[dict[str, Any]]:
    """
    Allocate a share of system daily boardings across selected corridors.

    corridor_share: fraction of city bus boardings carried by these 5 corridors
    (heuristic ~3–5% for five busy lines out of the full BMTA network).
    Returns routes with updated base_demand (passengers per operating hour average).
    """
    mot = latest_monthly_boardings()
    if daily_system is None:
        daily_system = mot["daily_system_boardings"]

    corridor_daily = daily_system * corridor_share
    weights = [max(1.0, float(r.get("allocation_weight", 1))) for r in routes]
    wsum = sum(weights) or 1.0
    # Spread across ~18 operating hours → per-hour base used by diurnal multipliers
    operating_hours = 18.0

    out = []
    for r, w in zip(routes, weights):
        daily_r = corridor_daily * (w / wsum)
        base_per_hour = daily_r / operating_hours
        item = dict(r)
        item["base_demand"] = int(max(40, round(base_per_hour)))
        item["daily_boardings_prior"] = int(round(daily_r))
        item["mot_month"] = f"{mot['year']}-{mot['month']:02d}"
        out.append(item)
    return out


def try_refresh_from_catalog() -> bool:
    """Best-effort HEAD/GET of catalog page; keeps snapshot if scrape fails."""
    try:
        resp = requests.get(MOT_RESOURCE_CANDIDATES[0], timeout=30)
        if resp.status_code == 200:
            ensure_mot_snapshot()
            meta = {
                "catalog_reachable": True,
                "status": resp.status_code,
                "note": "XLSX auto-parse not guaranteed; using calibrated CSV snapshot.",
            }
            with open(MOT_META, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"MOT catalog refresh skipped: {exc}")
    ensure_mot_snapshot()
    return False
