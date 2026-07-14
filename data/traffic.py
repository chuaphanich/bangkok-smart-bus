"""
Traffic duration profiles via Google Routes API or OpenRouteService.

traffic_index = duration_in_traffic / static_duration (clamped).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from data import CACHE_DIR, PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

TRAFFIC_CACHE = CACHE_DIR / "traffic_profiles.json"
HOURS = list(range(5, 23))


def _provider() -> str:
    if os.getenv("GOOGLE_MAPS_API_KEY"):
        return "google"
    if os.getenv("ORS_API_KEY"):
        return "ors"
    return "heuristic"


def _sample_points(path: list[list[float]]) -> tuple[list[float], list[float], list[float] | None]:
    if len(path) < 2:
        raise ValueError("path too short")
    origin = path[0]
    dest = path[-1]
    mid = path[len(path) // 2] if len(path) >= 3 else None
    return origin, dest, mid


def _google_duration(
    origin: list[float],
    dest: list[float],
    mid: list[float] | None,
    departure: datetime,
    traffic_aware: bool,
) -> float | None:
    key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not key:
        return None
    body: dict[str, Any] = {
        "origin": {
            "location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}
        },
        "destination": {
            "location": {"latLng": {"latitude": dest[0], "longitude": dest[1]}}
        },
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE" if traffic_aware else "TRAFFIC_UNAWARE",
        "departureTime": departure.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if mid:
        body["intermediates"] = [
            {"location": {"latLng": {"latitude": mid[0], "longitude": mid[1]}}}
        ]
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "routes.duration,routes.staticDuration",
    }
    resp = requests.post(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        headers=headers,
        json=body,
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Google Routes {resp.status_code}: {resp.text[:200]}")
        return None
    data = resp.json()
    routes = data.get("routes") or []
    if not routes:
        return None
    dur = routes[0].get("duration") or routes[0].get("staticDuration")
    if isinstance(dur, str) and dur.endswith("s"):
        return float(dur[:-1])
    return None


def _ors_duration(origin: list[float], dest: list[float]) -> float | None:
    key = os.getenv("ORS_API_KEY")
    if not key:
        return None
    # ORS expects lon,lat
    url = "https://api.openrouteservice.org/v2/directions/driving-car"
    headers = {"Authorization": key, "Content-Type": "application/json"}
    body = {
        "coordinates": [
            [origin[1], origin[0]],
            [dest[1], dest[0]],
        ]
    }
    resp = requests.post(url, headers=headers, json=body, timeout=30)
    if resp.status_code != 200:
        print(f"ORS {resp.status_code}: {resp.text[:200]}")
        return None
    data = resp.json()
    try:
        return float(data["features"][0]["properties"]["summary"]["duration"])
    except (KeyError, IndexError, TypeError):
        return None


def _heuristic_index(hour: int, day_of_week: int) -> float:
    """Bangkok-like congestion when no API key is available."""
    if day_of_week < 5:
        if 7 <= hour <= 9:
            return 1.45 + 0.12 * ((hour - 7) / 2)
        if 16 <= hour <= 19:
            return 1.55 + 0.1 * ((hour - 16) / 3)
        if 11 <= hour <= 13:
            return 1.05
        return 0.78
    if 10 <= hour <= 18:
        return 1.10
    return 0.68


def build_traffic_profiles(
    routes: list[dict[str, Any]],
    force: bool = False,
) -> dict[str, Any]:
    """
    Profile per route_id:
      {hour: traffic_index} for a representative weekday + Saturday,
      plus free_flow_min estimate.
    """
    provider = _provider()
    if TRAFFIC_CACHE.exists() and not force:
        with open(TRAFFIC_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("provider") == provider and cached.get("profiles"):
            # ensure all routes present
            if all(r["route_id"] in cached["profiles"] for r in routes):
                return cached

    print(f"Building traffic profiles via provider={provider}…")
    profiles: dict[str, Any] = {}
    # Use next Tuesday and Saturday UTC+7 ≈ Asia/Bangkok
    tz = timezone(timedelta(hours=7))
    today = datetime.now(tz).date()
    # find next Tuesday (1) and Saturday (5)
    tuesday = today + timedelta(days=(1 - today.weekday()) % 7 or 7)
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)

    for r in routes:
        rid = r["route_id"]
        path = r.get("path") or []
        length_km = float(r.get("length_km") or 15.0)
        free_flow_sec = max(600.0, length_km * 2.1 * 60.0)  # ~2.1 min/km baseline

        weekday: dict[str, float] = {}
        weekend: dict[str, float] = {}

        if provider == "google" and len(path) >= 2:
            origin, dest, mid = _sample_points(path)
            # static once
            static_dep = datetime(tuesday.year, tuesday.month, tuesday.day, 3, 0, tzinfo=tz)
            static_sec = _google_duration(origin, dest, mid, static_dep.astimezone(timezone.utc), False)
            if static_sec and static_sec > 0:
                free_flow_sec = static_sec
            for hour in HOURS:
                dep_local = datetime(
                    tuesday.year, tuesday.month, tuesday.day, hour, 0, tzinfo=tz
                )
                dur = _google_duration(
                    origin, dest, mid, dep_local.astimezone(timezone.utc), True
                )
                if dur and free_flow_sec > 0:
                    weekday[str(hour)] = round(max(0.5, min(2.5, dur / free_flow_sec)), 3)
                else:
                    weekday[str(hour)] = round(_heuristic_index(hour, 1), 3)
                time.sleep(0.15)
            for hour in (8, 12, 17):
                dep_local = datetime(
                    saturday.year, saturday.month, saturday.day, hour, 0, tzinfo=tz
                )
                dur = _google_duration(
                    origin, dest, mid, dep_local.astimezone(timezone.utc), True
                )
                if dur and free_flow_sec > 0:
                    weekend[str(hour)] = round(max(0.5, min(2.5, dur / free_flow_sec)), 3)
                else:
                    weekend[str(hour)] = round(_heuristic_index(hour, 5), 3)
                time.sleep(0.15)
            # fill remaining weekend hours heuristically scaled
            for hour in HOURS:
                if str(hour) not in weekend:
                    weekend[str(hour)] = round(_heuristic_index(hour, 5), 3)

        elif provider == "ors" and len(path) >= 2:
            origin, dest, _mid = _sample_points(path)
            dur = _ors_duration(origin, dest)
            if dur and dur > 0:
                # ORS is typically "current-ish"; scale hourly by heuristic ratio
                base_idx = dur / free_flow_sec
                for hour in HOURS:
                    h = _heuristic_index(hour, 1)
                    weekday[str(hour)] = round(max(0.5, min(2.5, base_idx * (h / 1.0))), 3)
                    weekend[str(hour)] = round(
                        max(0.5, min(2.5, base_idx * (_heuristic_index(hour, 5) / 1.0))), 3
                    )
            else:
                for hour in HOURS:
                    weekday[str(hour)] = round(_heuristic_index(hour, 1), 3)
                    weekend[str(hour)] = round(_heuristic_index(hour, 5), 3)
            time.sleep(0.3)
        else:
            for hour in HOURS:
                weekday[str(hour)] = round(_heuristic_index(hour, 1), 3)
                weekend[str(hour)] = round(_heuristic_index(hour, 5), 3)

        profiles[rid] = {
            "weekday": weekday,
            "weekend": weekend,
            "free_flow_min": round(free_flow_sec / 60.0, 1),
            "length_km": length_km,
        }

    payload = {
        "provider": provider,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profiles": profiles,
        "note": (
            "Google TRAFFIC_AWARE when GOOGLE_MAPS_API_KEY set; "
            "else OpenRouteService if ORS_API_KEY; else Bangkok heuristic index."
        ),
    }
    with open(TRAFFIC_CACHE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {TRAFFIC_CACHE}")
    return payload


def traffic_index_for(
    profiles: dict[str, Any],
    route_id: str,
    hour: int,
    day_of_week: int,
    rain: float = 0.0,
) -> float:
    prof = profiles.get("profiles", {}).get(route_id)
    if not prof:
        idx = _heuristic_index(hour, day_of_week)
    else:
        bucket = "weekend" if day_of_week >= 5 else "weekday"
        idx = float(prof[bucket].get(str(hour), _heuristic_index(hour, day_of_week)))
    idx *= 1.0 + 0.22 * rain
    return float(max(0.4, min(2.5, idx)))


def free_flow_min(profiles: dict[str, Any], route_id: str, length_km: float) -> float:
    prof = profiles.get("profiles", {}).get(route_id)
    if prof and prof.get("free_flow_min"):
        return float(prof["free_flow_min"])
    return length_km * 2.1
