"""
Build cached real-world training dataset + route network for Bangkok Smart Bus.

Usage:
    python -m data.build_dataset
    python -m data.build_dataset --force
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import requests

from data import CACHE_DIR
from data.gtfs_loader import ROUTES_JSON, load_or_build_routes
from data.mot_ridership import (
    allocate_corridor_base_demand,
    latest_monthly_boardings,
    try_refresh_from_catalog,
)
from data.osm_routes import enrich_routes_with_osm
from data.traffic import build_traffic_profiles, free_flow_min, traffic_index_for

HISTORICAL_CSV = CACHE_DIR / "historical.csv"
META_JSON = CACHE_DIR / "dataset_meta.json"
HOURS = list(range(5, 23))
RNG = np.random.default_rng(42)


def bangkok_demand_multiplier(hour: int, day_of_week: int) -> float:
    if day_of_week < 5:
        if 6 <= hour <= 9:
            return 1.6 + 0.3 * (hour == 8)
        if 16 <= hour <= 19:
            return 1.5 + 0.25 * (hour in (17, 18))
        if 11 <= hour <= 14:
            return 0.95
        if hour >= 21:
            return 0.45
        return 0.70
    if 10 <= hour <= 20:
        return 0.85
    return 0.40


def fetch_bangkok_rain_days(n_days: int = 60) -> list[float]:
    """Daily rain flags from Open-Meteo (free, no key)."""
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=n_days - 1)
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude=13.7563&longitude=100.5018"
        f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
        "&daily=precipitation_sum&timezone=Asia%2FBangkok"
    )
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        precip = resp.json().get("daily", {}).get("precipitation_sum") or []
        flags = [1.0 if (p or 0) >= 1.0 else 0.0 for p in precip]
        if len(flags) >= n_days:
            return flags[-n_days:]
        # pad
        while len(flags) < n_days:
            flags.insert(0, float(RNG.random() < 0.18))
        return flags
    except Exception as exc:  # noqa: BLE001
        print(f"Open-Meteo rain fetch failed ({exc}); using climatic rain days.")
        return [float(RNG.random() < 0.18) for _ in range(n_days)]


def build_historical(
    routes: list[dict[str, Any]],
    traffic: dict[str, Any],
    n_days: int = 60,
) -> pd.DataFrame:
    rain_days = fetch_bangkok_rain_days(n_days)
    rows = []
    for day in range(n_days):
        dow = day % 7
        rain = rain_days[day]
        for hour in HOURS:
            for r in routes:
                rid = r["route_id"]
                length_km = float(r["length_km"])
                stops = int(r["n_stops"])
                base_dem = int(r["base_demand"])
                fare = float(r["fare_thb"])
                diesel = float(r["diesel_l_per_km"])

                traffic_idx = traffic_index_for(traffic, rid, hour, dow, rain)
                traffic_idx += float(RNG.normal(0, 0.04))
                traffic_idx = float(np.clip(traffic_idx, 0.4, 2.5))

                demand_mult = bangkok_demand_multiplier(hour, dow)
                demand_mult *= 1.0 - 0.12 * rain
                demand_mult += float(RNG.normal(0, 0.07))
                demand_mult = float(np.clip(demand_mult, 0.2, 2.2))
                passengers = max(
                    0,
                    int(base_dem * demand_mult * (1 + float(RNG.normal(0, 0.05)))),
                )

                ff = free_flow_min(traffic, rid, length_km)
                dwell = stops * 0.35 * (0.8 + 0.4 * (passengers / (base_dem + 1)))
                travel = ff * traffic_idx + dwell + float(RNG.normal(0, 2.0))
                travel = max(length_km * 1.5, travel)

                rows.append(
                    {
                        "day": day,
                        "day_of_week": dow,
                        "hour": hour,
                        "route_id": rid,
                        "length_km": length_km,
                        "n_stops": stops,
                        "traffic_index": round(traffic_idx, 3),
                        "rain": rain,
                        "passengers": passengers,
                        "travel_time_min": round(travel, 1),
                        "fare_thb": fare,
                        "diesel_l_per_km": diesel,
                    }
                )
    return pd.DataFrame(rows)


def build_all(force: bool = False) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try_refresh_from_catalog()

    routes = load_or_build_routes(force_download=force, force_rebuild=force)
    routes = enrich_routes_with_osm(routes, force=force)
    routes = allocate_corridor_base_demand(routes)

    # Persist enriched routes
    mot = latest_monthly_boardings()
    with open(ROUTES_JSON, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source": "namtang-gtfs+osm+mot",
                "mot": mot,
                "routes": routes,
            },
            f,
            ensure_ascii=False,
        )

    traffic = build_traffic_profiles(routes, force=force)
    hist = build_historical(routes, traffic, n_days=60)
    hist.to_csv(HISTORICAL_CSV, index=False)

    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "n_rows": len(hist),
        "n_routes": len(routes),
        "routes": [r["route_id"] for r in routes],
        "mot_month": mot.get("mot_month") or f"{mot['year']}-{mot['month']:02d}",
        "mot_daily_system_boardings": mot["daily_system_boardings"],
        "traffic_provider": traffic.get("provider"),
        "sources": {
            "network": "Namtang GTFS (OTP Thailand)",
            "geometry": "GTFS shapes + OpenStreetMap Overpass",
            "ridership": "MOT BMTA monthly totals (disaggregated)",
            "traffic": traffic.get("note"),
            "weather": "Open-Meteo Bangkok precipitation",
        },
        "disclaimer": mot.get("disclaimer"),
    }
    with open(META_JSON, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"Historical rows: {len(hist):,} → {HISTORICAL_CSV}")
    print(f"Meta → {META_JSON}")
    return {"routes": routes, "historical": hist, "meta": meta, "traffic": traffic}


def load_cached_routes() -> list[dict[str, Any]] | None:
    if not ROUTES_JSON.exists():
        return None
    with open(ROUTES_JSON, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("routes")


def load_cached_historical() -> pd.DataFrame | None:
    if not HISTORICAL_CSV.exists():
        return None
    return pd.read_csv(HISTORICAL_CSV)


def load_dataset_meta() -> dict[str, Any]:
    if META_JSON.exists():
        with open(META_JSON, encoding="utf-8") as f:
            return json.load(f)
    return {}


def ensure_dataset(force: bool = False) -> dict[str, Any]:
    if (
        not force
        and ROUTES_JSON.exists()
        and HISTORICAL_CSV.exists()
        and META_JSON.exists()
    ):
        routes = load_cached_routes() or []
        hist = load_cached_historical()
        meta = load_dataset_meta()
        if routes and hist is not None and len(hist) > 100:
            return {"routes": routes, "historical": hist, "meta": meta}
    return build_all(force=force)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build real Bangkok bus dataset")
    parser.add_argument("--force", action="store_true", help="Redownload / rebuild")
    args = parser.parse_args()
    build_all(force=args.force)


if __name__ == "__main__":
    main()
