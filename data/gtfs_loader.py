"""
Download and parse Namtang GTFS; select five BMTA Bangkok corridors.
"""

from __future__ import annotations

import json
import math
import re
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from data import BANGKOK_BBOX, CACHE_DIR, PREFERRED_REFS, ROUTE_COLORS

GTFS_URL = "https://namtang-api.otp.go.th/download/namtang-gtfs.zip"
GTFS_ZIP = CACHE_DIR / "namtang-gtfs.zip"
ROUTES_JSON = CACHE_DIR / "routes.json"

# Thai Smile Bus / BMTA agency hints in Namtang feed
BMTA_AGENCY_HINTS = (
    "bmta",
    "ขสมก",
    "bangkok mass transit authority",
    "thai smile bus",
    "tsb",
)
PREFERRED_AGENCY_IDS = ("BMTA", "TSB", "DLT")


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def path_length_km(path: list[list[float]]) -> float:
    if len(path) < 2:
        return 0.0
    total = 0.0
    for (a, b), (c, d) in zip(path, path[1:]):
        total += _haversine_km(a, b, c, d)
    return total


def downsample_path(path: list[list[float]], max_points: int = 80) -> list[list[float]]:
    if len(path) <= max_points:
        return path
    step = max(1, len(path) // max_points)
    out = path[::step]
    if out[-1] != path[-1]:
        out.append(path[-1])
    return out


def download_gtfs(force: bool = False) -> Path:
    if GTFS_ZIP.exists() and not force and GTFS_ZIP.stat().st_size > 1_000_000:
        return GTFS_ZIP
    print(f"Downloading Namtang GTFS from {GTFS_URL} …")
    resp = requests.get(GTFS_URL, timeout=180, stream=True)
    resp.raise_for_status()
    with open(GTFS_ZIP, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)
    print(f"Saved {GTFS_ZIP} ({GTFS_ZIP.stat().st_size / 1e6:.1f} MB)")
    return GTFS_ZIP


def _read_gtfs_csv(zf: zipfile.ZipFile, name: str, usecols: list[str] | None = None) -> pd.DataFrame:
    # Handle nested zip layouts
    names = zf.namelist()
    match = next((n for n in names if n.endswith(name) or n.endswith("/" + name)), None)
    if match is None:
        raise FileNotFoundError(f"{name} not found in GTFS zip")
    with zf.open(match) as fh:
        return pd.read_csv(fh, usecols=usecols, dtype=str, low_memory=False)


def _norm_ref(ref: str) -> str:
    ref = (ref or "").strip().upper()
    # Strip Thai joint-service prefix "ต." / "T."
    ref = re.sub(r"^[ตT]\.?\s*", "", ref, flags=re.IGNORECASE)
    ref = re.sub(r"^R", "", ref)
    ref = re.split(r"\s+", ref)[0]
    ref = re.sub(r"[^0-9A-Z\-()]", "", ref)
    return ref


def _is_bmta_agency(agency_name: str, agency_id: str) -> bool:
    aid = str(agency_id).strip().upper()
    if aid in {a.upper() for a in PREFERRED_AGENCY_IDS}:
        return True
    blob = f"{agency_name} {agency_id}".lower()
    return any(h in blob for h in BMTA_AGENCY_HINTS)


def _in_bangkok(lat: float, lon: float) -> bool:
    return (
        BANGKOK_BBOX["south"] <= lat <= BANGKOK_BBOX["north"]
        and BANGKOK_BBOX["west"] <= lon <= BANGKOK_BBOX["east"]
    )


def _shape_path(shapes: pd.DataFrame, shape_id: str) -> list[list[float]]:
    sdf = shapes[shapes["shape_id"] == shape_id].copy()
    if sdf.empty:
        return []
    sdf["shape_pt_sequence"] = pd.to_numeric(sdf["shape_pt_sequence"], errors="coerce")
    sdf = sdf.dropna(subset=["shape_pt_sequence"]).sort_values("shape_pt_sequence")
    path: list[list[float]] = []
    for _, row in sdf.iterrows():
        try:
            path.append([float(row["shape_pt_lat"]), float(row["shape_pt_lon"])])
        except (TypeError, ValueError):
            continue
    return downsample_path(path)


def _is_bus_route(row: pd.Series) -> bool:
    """Namtang includes rail; keep road buses only (GTFS route_type 3)."""
    rt = str(row.get("route_type", "3")).strip()
    if rt and rt != "3":
        return False
    name = f"{row.get('route_short_name', '')} {row.get('route_long_name', '')}".lower()
    # Exclude known rail branding that sometimes shares numeric refs
    rail_hints = ("bts", "mrt", "arl", "srt", "sukhumvit", "silom", "airport rail")
    if any(h in name for h in rail_hints) and "bus" not in name:
        # Still allow if clearly a bus long name with BMTA-style Thai origins
        if "ขสมก" in name or "bmta" in name:
            return True
        if "sukhumvit" in name or "silom" in name or "bts" in name or "mrt" in name:
            return False
    return True


def _safe_route_id(ref: str, gtfs_id: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z\-]", "", ref) or re.sub(r"[^0-9A-Za-z]", "", gtfs_id)[-8:]
    return f"R{clean}" if not clean.startswith("R") else clean


def select_corridors(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    agencies = _read_gtfs_csv(zf, "agency.txt")
    routes = _read_gtfs_csv(zf, "routes.txt")
    trips = _read_gtfs_csv(zf, "trips.txt")
    stops = _read_gtfs_csv(zf, "stops.txt")
    stop_times = _read_gtfs_csv(
        zf, "stop_times.txt", usecols=["trip_id", "stop_id", "stop_sequence"]
    )

    agency_ids = set()
    for _, a in agencies.iterrows():
        if _is_bmta_agency(str(a.get("agency_name", "")), str(a.get("agency_id", ""))):
            agency_ids.add(str(a["agency_id"]))
    # Always include BMTA/TSB if present
    for aid in PREFERRED_AGENCY_IDS:
        if (agencies["agency_id"].astype(str).str.upper() == aid).any():
            real = agencies.loc[
                agencies["agency_id"].astype(str).str.upper() == aid, "agency_id"
            ].iloc[0]
            agency_ids.add(str(real))

    routes["route_type"] = routes.get("route_type", "3").fillna("3")
    # Prefer BMTA then TSB; keep DLT available for legacy ref lookup
    city_ids = {a for a in agency_ids if a.upper() in ("BMTA", "TSB", "DLT")}
    if city_ids:
        cand = routes[routes["agency_id"].astype(str).isin(city_ids)].copy()
    elif agency_ids:
        cand = routes[routes["agency_id"].astype(str).isin(agency_ids)].copy()
    else:
        cand = routes.copy()

    cand = cand[cand.apply(_is_bus_route, axis=1)].copy()
    if cand.empty:
        cand = routes[routes["route_type"].astype(str) == "3"].copy()

    cand["ref"] = cand["route_short_name"].fillna("").map(_norm_ref)
    # Also parse parenthetical legacy numbers: "2-14 (70)" → secondary ref 70
    def _legacy_from_name(row: pd.Series) -> str:
        short = str(row.get("route_short_name") or "")
        m = re.search(r"\((\d{1,4}[A-Z]?)\)", short.upper())
        if m:
            return m.group(1)
        return ""

    cand["legacy_ref"] = cand.apply(_legacy_from_name, axis=1)

    trip_counts = trips.groupby("route_id").size().rename("n_trips")
    cand = cand.merge(trip_counts, left_on="route_id", right_index=True, how="left")
    cand["n_trips"] = cand["n_trips"].fillna(0).astype(int)

    # Prefer BMTA/TSB rows when sorting
    cand["_prio"] = cand["agency_id"].astype(str).str.upper().map(
        {"BMTA": 0, "TSB": 1, "DLT": 2}
    ).fillna(5)

    selected: list[pd.Series] = []
    used_route_ids: set[str] = set()

    for pref in PREFERRED_REFS:
        hits = cand[
            (cand["ref"] == pref) | (cand["legacy_ref"] == pref)
        ].sort_values(["_prio", "n_trips"], ascending=[True, False])
        if hits.empty:
            continue
        row = hits.iloc[0]
        selected.append(row)
        used_route_ids.add(str(row["route_id"]))

    if len(selected) < 5:
        # Fill with busiest BMTA/TSB corridors
        rest = cand[
            (~cand["route_id"].isin(used_route_ids))
            & (cand["agency_id"].astype(str).str.upper().isin(["BMTA", "TSB"]))
        ].sort_values("n_trips", ascending=False)
        if rest.empty:
            rest = cand[~cand["route_id"].isin(used_route_ids)].sort_values(
                "n_trips", ascending=False
            )
        for _, row in rest.iterrows():
            if len(selected) >= 5:
                break
            if not str(row["ref"]):
                continue
            selected.append(row)
            used_route_ids.add(str(row["route_id"]))

    shape_ids_needed: set[str] = set()
    route_trip_meta: dict[str, dict[str, Any]] = {}
    for row in selected:
        rid = str(row["route_id"])
        rt = trips[trips["route_id"] == rid]
        if rt.empty:
            continue
        if "shape_id" in rt.columns and rt["shape_id"].notna().any():
            shape_id = str(rt["shape_id"].dropna().mode().iloc[0])
            shape_ids_needed.add(shape_id)
        else:
            shape_id = ""
        trip_ids = set(rt["trip_id"].astype(str))
        st = stop_times[stop_times["trip_id"].isin(list(trip_ids)[:200])]
        n_stops = int(st["stop_id"].nunique()) if not st.empty else 20
        sample_trip = str(rt.iloc[0]["trip_id"])
        st_one = stop_times[stop_times["trip_id"] == sample_trip].copy()
        st_one["stop_sequence"] = pd.to_numeric(st_one["stop_sequence"], errors="coerce")
        st_one = st_one.dropna().sort_values("stop_sequence")
        stop_ids = st_one["stop_id"].astype(str).tolist()
        route_trip_meta[rid] = {
            "shape_id": shape_id,
            "n_stops": max(n_stops, 2),
            "n_trips": int(row["n_trips"]),
            "stop_ids": stop_ids,
        }

    shapes_needed = pd.DataFrame()
    if shape_ids_needed:
        print(f"Loading shapes for {len(shape_ids_needed)} corridors…")
        shapes_all = _read_gtfs_csv(
            zf,
            "shapes.txt",
            usecols=["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
        )
        shapes_needed = shapes_all[shapes_all["shape_id"].isin(shape_ids_needed)]

    stops["stop_lat"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
    stops["stop_lon"] = pd.to_numeric(stops["stop_lon"], errors="coerce")
    stop_lookup = stops.set_index("stop_id")

    fare_default = 15.0
    try:
        fares = _read_gtfs_csv(zf, "fare_attributes.txt")
        if "price" in fares.columns:
            prices = pd.to_numeric(fares["price"], errors="coerce").dropna()
            if not prices.empty:
                fare_default = float(prices.median())
                if fare_default > 100:
                    fare_default = fare_default / 100.0
    except Exception:
        pass

    corridors: list[dict[str, Any]] = []
    for i, row in enumerate(selected[:5]):
        rid = str(row["route_id"])
        ref = str(row["ref"]) or rid
        meta = route_trip_meta.get(rid, {})
        shape_id = meta.get("shape_id", "")
        path = _shape_path(shapes_needed, shape_id) if shape_id else []

        if len(path) < 2:
            pts = []
            for sid in meta.get("stop_ids", []):
                if sid not in stop_lookup.index:
                    continue
                srow = stop_lookup.loc[sid]
                if isinstance(srow, pd.DataFrame):
                    srow = srow.iloc[0]
                try:
                    lat, lon = float(srow["stop_lat"]), float(srow["stop_lon"])
                except (TypeError, ValueError):
                    continue
                if math.isnan(lat) or math.isnan(lon):
                    continue
                pts.append([lat, lon])
            path = downsample_path(pts)

        if path and not any(_in_bangkok(p[0], p[1]) for p in path):
            continue

        length_km = round(path_length_km(path), 2) if path else 15.0
        n_stops = int(meta.get("n_stops", 20))
        long_name = str(row.get("route_long_name") or row.get("route_desc") or ref)
        short = str(row.get("route_short_name") or ref)
        # Prefer Latin part of bilingual names
        if ";" in long_name:
            parts = [p.strip() for p in long_name.split(";") if p.strip()]
            long_name = parts[-1] if parts else long_name
        route_id = _safe_route_id(ref, rid)

        weight = max(1, int(meta.get("n_trips", 1))) * max(1, n_stops)
        legacy = str(row.get("legacy_ref") or "")
        color_key = legacy if legacy in ROUTE_COLORS else (ref if ref in ROUTE_COLORS else None)

        corridors.append(
            {
                "route_id": route_id,
                "gtfs_route_id": rid,
                "ref": ref,
                "legacy_ref": legacy or None,
                "name": f"{short} · {long_name}"[:90],
                "color": color_key
                and ROUTE_COLORS.get(color_key)
                or ["#E85D04", "#2A9D8F", "#4C6EF5", "#D62828", "#9B5DE5"][i % 5],
                "path": path,
                "length_km": length_km or 15.0,
                "n_stops": n_stops,
                "n_trips": int(meta.get("n_trips", 0)),
                "fare_thb": round(min(max(fare_default, 8.0), 40.0), 1),
                "diesel_l_per_km": round(0.38 + 0.004 * length_km, 3),
                "base_demand": 120,
                "allocation_weight": weight,
                "shape_source": "gtfs" if shape_id else "stops",
                "agency_hint": str(row.get("agency_id", "BMTA")),
            }
        )

    if len(corridors) < 3:
        raise RuntimeError(
            f"Only found {len(corridors)} Bangkok corridors in GTFS; check feed."
        )
    return corridors


def load_or_build_routes(force_download: bool = False, force_rebuild: bool = False) -> list[dict[str, Any]]:
    if ROUTES_JSON.exists() and not force_rebuild:
        with open(ROUTES_JSON, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("routes"):
            return data["routes"]

    download_gtfs(force=force_download)
    with zipfile.ZipFile(GTFS_ZIP, "r") as zf:
        corridors = select_corridors(zf)

    payload = {
        "source": "namtang-gtfs",
        "url": GTFS_URL,
        "routes": corridors,
    }
    with open(ROUTES_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"Wrote {ROUTES_JSON} with {len(corridors)} corridors")
    return corridors


if __name__ == "__main__":
    routes = load_or_build_routes()
    for r in routes:
        print(
            r["route_id"],
            r["name"][:40],
            f"{r['length_km']} km",
            f"{r['n_stops']} stops",
            f"path={len(r['path'])}",
        )
