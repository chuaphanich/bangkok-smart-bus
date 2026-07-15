"""
Per-stop passenger wait estimates modulated by along-corridor traffic.

Base wait ≈ half headway (from frequency). Congestion raises irregularity
and delay accumulation toward downstream stops — so waits differ by stop.

Traffic sources (in order):
  1. Google Routes speedReadingIntervals when GOOGLE_MAPS_API_KEY is set
  2. Route-hour traffic profile + Bangkok spatial congestion surface
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv

from data import CACHE_DIR, PROJECT_ROOT
from data.traffic import TRAFFIC_CACHE, _heuristic_index, traffic_index_for

load_dotenv(PROJECT_ROOT / ".env")

SEGMENT_CACHE = CACHE_DIR / "corridor_traffic_segments.json"

# Dense Bangkok core — jam hotspot for spatial layer
CBD = {"lat": 13.736, "lon": 100.550}


def _haversine_m(a: list[float], b: list[float]) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dphi = math.radians(b[0] - a[0])
    dlmb = math.radians(b[1] - a[1])
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def stops_along_path(path: list[list[float]], n_stops: int, max_display: int = 20) -> list[list[float]]:
    if not path:
        return []
    if len(path) == 1:
        return [path[0]]
    cum = [0.0]
    for i in range(1, len(path)):
        cum.append(cum[-1] + _haversine_m(path[i - 1], path[i]))
    total = cum[-1] or 1.0
    count = max(2, min(int(n_stops or 12), max_display))
    out: list[list[float]] = []
    for s in range(count):
        target = (s / (count - 1)) * total
        i = 1
        while i < len(cum) and cum[i] < target:
            i += 1
        i0 = max(0, i - 1)
        i1 = min(len(cum) - 1, i)
        seg = cum[i1] - cum[i0] or 1.0
        t = (target - cum[i0]) / seg
        a, b = path[i0], path[i1]
        out.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
    return out


def _spatial_congestion(lat: float, lon: float, hour: int, day_of_week: int) -> float:
    """0.7–1.6 multiplier from distance to CBD + peak hours."""
    dist_km = _haversine_m([lat, lon], [CBD["lat"], CBD["lon"]]) / 1000.0
    # Closer to CBD → more congestion
    prox = math.exp(-dist_km / 6.5)
    peak = 1.0
    if day_of_week < 5:
        if 7 <= hour <= 9 or 16 <= hour <= 19:
            peak = 1.35
        elif 11 <= hour <= 13:
            peak = 1.1
        else:
            peak = 0.95
    else:
        peak = 1.05 if 10 <= hour <= 18 else 0.85
    return float(0.75 + 0.85 * prox * peak)


def _speed_to_index(speed_label: str) -> float:
    return {
        "NORMAL": 0.85,
        "SLOW": 1.35,
        "TRAFFIC_JAM": 1.85,
    }.get(speed_label, 1.1)


def _google_segment_indices(path: list[list[float]], hour: int, day_of_week: int) -> list[float] | None:
    key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not key or len(path) < 2:
        return None
    tz = timezone(timedelta(hours=7))
    today = datetime.now(tz).date()
    # next matching weekday type
    target_dow = day_of_week
    delta = (target_dow - today.weekday()) % 7
    day = today + timedelta(days=delta or 7)
    dep = datetime(day.year, day.month, day.day, hour, 0, tzinfo=tz).astimezone(timezone.utc)

    origin, dest = path[0], path[-1]
    mid = path[len(path) // 2]
    body = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {
            "location": {"latLng": {"latitude": dest[0], "longitude": dest[1]}}
        },
        "intermediates": [
            {"location": {"latLng": {"latitude": mid[0], "longitude": mid[1]}}}
        ],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "departureTime": dep.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": (
            "routes.duration,routes.staticDuration,"
            "routes.travelAdvisory.speedReadingIntervals,"
            "routes.polyline.encodedPolyline"
        ),
    }
    try:
        resp = requests.post(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            headers=headers,
            json=body,
            timeout=35,
        )
        if resp.status_code != 200:
            return None
        routes = resp.json().get("routes") or []
        if not routes:
            return None
        advisory = (routes[0].get("travelAdvisory") or {}).get("speedReadingIntervals") or []
        if not advisory:
            # Fall back to single ratio applied with mild spatial noise later
            dur = routes[0].get("duration", "0s")
            static = routes[0].get("staticDuration", dur)
            def _sec(x: str) -> float:
                return float(str(x).rstrip("s") or 0)

            if _sec(static) <= 0:
                return None
            ratio = max(0.5, min(2.5, _sec(dur) / _sec(static)))
            return [ratio] * max(8, len(path) // 4)

        # Map interval midpoints onto [0,1] progress along encoded points count
        n_pts = max(
            (iv.get("endPolylinePointIndex") or 0) for iv in advisory
        ) + 1
        n_pts = max(n_pts, 8)
        series = [1.0] * n_pts
        for iv in advisory:
            a = int(iv.get("startPolylinePointIndex") or 0)
            b = int(iv.get("endPolylinePointIndex") or a)
            idx = _speed_to_index(str(iv.get("speed") or "NORMAL"))
            for k in range(a, min(b + 1, n_pts)):
                series[k] = idx
        return series
    except Exception:
        return None


def _load_segment_cache() -> dict[str, Any]:
    if SEGMENT_CACHE.exists():
        with open(SEGMENT_CACHE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_segment_cache(cache: dict[str, Any]) -> None:
    with open(SEGMENT_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def corridor_traffic_series(
    route_id: str,
    path: list[list[float]],
    hour: int,
    day_of_week: int,
    rain: float,
    route_traffic: float,
    force_google: bool = False,
) -> tuple[list[float], str]:
    """
    Return traffic index samples along the corridor + source label.
    """
    cache = _load_segment_cache()
    cache_key = f"{route_id}|{hour}|{day_of_week}|{int(rain)}"
    if not force_google and cache_key in cache:
        entry = cache[cache_key]
        return entry["series"], entry.get("source", "cache")

    source = "spatial+profile"
    series = _google_segment_indices(path, hour, day_of_week) if os.getenv("GOOGLE_MAPS_API_KEY") else None
    if series:
        source = "google_speed_readings"
        # blend with route hour profile + rain
        series = [float(max(0.5, min(2.5, v * (1 + 0.15 * rain) * (0.5 + 0.5 * route_traffic)))) for v in series]
        time.sleep(0.12)
    else:
        # Spatial surface × corridor hour profile
        n = max(10, min(24, len(path)))
        series = []
        for i in range(n):
            t = i / (n - 1)
            # sample lat/lon along path by index
            j = int(t * (len(path) - 1))
            lat, lon = path[j]
            spatial = _spatial_congestion(lat, lon, hour, day_of_week)
            # Mid-route often denser junctions
            mid_bias = 1.0 + 0.12 * math.sin(t * math.pi)
            idx = route_traffic * spatial * mid_bias * (1 + 0.12 * rain)
            series.append(float(max(0.5, min(2.5, idx))))

    cache[cache_key] = {"series": series, "source": source}
    _save_segment_cache(cache)
    return series, source


def traffic_at_stop(series: list[float], stop_index: int, n_stops: int) -> float:
    if not series:
        return 1.0
    t = stop_index / max(n_stops - 1, 1)
    pos = t * (len(series) - 1)
    i0 = int(pos)
    i1 = min(len(series) - 1, i0 + 1)
    frac = pos - i0
    return series[i0] * (1 - frac) + series[i1] * frac


def effective_wait_min(base_wait: float, local_traffic: float, upstream_mean: float) -> float:
    """
    Congestion → bunched / irregular arrivals → higher effective wait.
    Downstream of jams gets an extra reliability penalty.
    """
    irregularity = 1.0 + 0.55 * max(0.0, local_traffic - 0.9)
    cascade = 1.0 + 0.35 * max(0.0, upstream_mean - 0.95)
    return round(max(0.8, base_wait * irregularity * cascade), 2)


def attach_stop_waits(
    routes: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    day_of_week: int,
    rain: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Add `stops` to each plan: [{lat, lon, traffic, wait_ai, wait_fixed}, ...]
    """
    profiles: dict[str, Any] = {}
    if TRAFFIC_CACHE.exists():
        with open(TRAFFIC_CACHE, encoding="utf-8") as f:
            profiles = json.load(f)

    route_by_id = {r["route_id"]: r for r in routes}
    meta = {"stop_traffic_source": "spatial+profile", "google_used": False}

    enriched = []
    for plan in plans:
        rid = plan["route_id"]
        route = route_by_id.get(rid)
        if not route or not route.get("path"):
            enriched.append(plan)
            continue

        hour = int(plan["hour"])
        path = route["path"]
        route_tf = traffic_index_for(profiles, rid, hour, day_of_week, rain)
        series, source = corridor_traffic_series(
            rid, path, hour, day_of_week, rain, route_tf
        )
        if source.startswith("google"):
            meta["google_used"] = True
            meta["stop_traffic_source"] = source

        stops_xy = stops_along_path(path, int(route.get("n_stops") or 16))
        base_ai = 60.0 / max(1, int(plan["frequency_per_hour"])) / 2.0
        base_fixed = 60.0 / max(1, int(plan.get("fixed_frequency") or bb_baseline(hour))) / 2.0

        stop_rows = []
        running = []
        for i, xy in enumerate(stops_xy):
            local = traffic_at_stop(series, i, len(stops_xy))
            running.append(local)
            up_mean = sum(running) / len(running)
            stop_rows.append(
                {
                    "lat": round(xy[0], 6),
                    "lon": round(xy[1], 6),
                    "traffic_index": round(local, 3),
                    "wait_ai": effective_wait_min(base_ai, local, up_mean),
                    "wait_fixed": effective_wait_min(base_fixed, local, up_mean),
                }
            )
        row = dict(plan)
        row["stops"] = stop_rows
        enriched.append(row)

    return enriched, meta


def bb_baseline(hour: int) -> int:
    if 6 <= hour <= 9 or 16 <= hour <= 19:
        return 6
    if 10 <= hour <= 15:
        return 4
    return 2
