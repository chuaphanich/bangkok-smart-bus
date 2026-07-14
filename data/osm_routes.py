"""
OpenStreetMap Overpass enrichment for BMTA bus routes.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from data import BANGKOK_BBOX, CACHE_DIR
from data.gtfs_loader import downsample_path, path_length_km

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSM_CACHE = CACHE_DIR / "osm_routes.json"


def _overpass_query(refs: list[str]) -> str:
    bbox = (
        f"{BANGKOK_BBOX['south']},{BANGKOK_BBOX['west']},"
        f"{BANGKOK_BBOX['north']},{BANGKOK_BBOX['east']}"
    )
    parts = []
    for ref in refs:
        parts.append(
            f'relation["route"="bus"]["ref"="{ref}"]({bbox});'
        )
        parts.append(
            f'relation["route"="bus"]["network"="BMTA"]["ref"="{ref}"]({bbox});'
        )
    body = "\n  ".join(parts)
    return f"""
[out:json][timeout:90];
(
  {body}
);
out body;
>;
out skel qt;
"""


def fetch_osm_geometries(refs: list[str], force: bool = False) -> dict[str, dict[str, Any]]:
    """Return {ref: {path, n_stops, name}} from OSM."""
    if OSM_CACHE.exists() and not force:
        with open(OSM_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        if all(r in cached for r in refs):
            return {r: cached[r] for r in refs if r in cached}

    print("Querying OpenStreetMap Overpass for BMTA route geometries…")
    query = _overpass_query(refs)
    headers = {
        "User-Agent": "BangkokSmartBus/1.0 (research; contact local)",
        "Accept": "application/json",
    }
    endpoints = [
        OVERPASS_URL,
        "https://overpass.kumi.systems/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    ]
    data = None
    last_exc: Exception | None = None
    for url in endpoints:
        try:
            resp = requests.post(
                url, data={"data": query}, headers=headers, timeout=120
            )
            resp.raise_for_status()
            data = resp.json()
            print(f"OSM OK via {url}")
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            print(f"OSM endpoint failed ({url}): {exc}")
            continue
    if data is None:
        print(f"OSM Overpass failed: {last_exc}")
        if OSM_CACHE.exists():
            with open(OSM_CACHE, encoding="utf-8") as f:
                return json.load(f)
        return {}

    elements = data.get("elements", [])
    nodes = {
        e["id"]: (e["lat"], e["lon"])
        for e in elements
        if e.get("type") == "node" and "lat" in e
    }
    ways = {
        e["id"]: e.get("nodes", [])
        for e in elements
        if e.get("type") == "way"
    }

    by_ref: dict[str, dict[str, Any]] = {}
    for e in elements:
        if e.get("type") != "relation":
            continue
        tags = e.get("tags") or {}
        ref = str(tags.get("ref", "")).strip()
        if ref not in refs:
            continue
        # Prefer first complete relation per ref
        if ref in by_ref and by_ref[ref].get("path"):
            continue

        path: list[list[float]] = []
        n_stops = 0
        for m in e.get("members", []):
            role = m.get("role", "")
            if m.get("type") == "node" and role in ("stop", "platform", ""):
                nid = m.get("ref")
                if nid in nodes:
                    n_stops += 1
            if m.get("type") == "way" and role in ("", "forward", "backward", "route"):
                wid = m.get("ref")
                for nid in ways.get(wid, []):
                    if nid in nodes:
                        lat, lon = nodes[nid]
                        path.append([lat, lon])

        path = downsample_path(path, max_points=100)
        if len(path) < 2:
            continue
        by_ref[ref] = {
            "path": path,
            "n_stops": max(n_stops, 2),
            "name": tags.get("name") or tags.get("name:en") or f"BMTA {ref}",
            "length_km": round(path_length_km(path), 2),
            "source": "osm",
        }
        time.sleep(0.2)

    # merge with previous cache
    merged = {}
    if OSM_CACHE.exists():
        with open(OSM_CACHE, encoding="utf-8") as f:
            merged = json.load(f)
    merged.update(by_ref)
    with open(OSM_CACHE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)
    print(f"OSM geometries for refs: {list(by_ref.keys())}")
    return by_ref


def enrich_routes_with_osm(routes: list[dict[str, Any]], force: bool = False) -> list[dict[str, Any]]:
    refs: list[str] = []
    for r in routes:
        ref = str(r.get("ref") or r["route_id"].lstrip("R"))
        refs.append(ref)
        # Also try parenthetical legacy numbers in the display name
        import re

        m = re.search(r"\((\d{1,4}[A-Za-z]?)\)", str(r.get("name", "")))
        if m:
            refs.append(m.group(1))
        # Prefer digitation from preferred set
        for p in ("8", "29", "39", "509", "554"):
            if p == ref or f"({p})" in str(r.get("name", "")):
                refs.append(p)
    # unique preserve order
    seen = set()
    uniq = []
    for x in refs:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    osm = fetch_osm_geometries(uniq, force=force)
    out = []
    for r in routes:
        ref = str(r.get("ref") or r["route_id"].lstrip("R"))
        item = dict(r)
        alt = None
        import re

        m = re.search(r"\((\d{1,4}[A-Za-z]?)\)", str(r.get("name", "")))
        legacy = m.group(1) if m else None
        o = osm.get(ref) or (osm.get(legacy) if legacy else None)
        if o:
            if len(item.get("path") or []) < 5 and len(o["path"]) >= 5:
                item["path"] = o["path"]
                item["shape_source"] = "osm"
                item["length_km"] = o["length_km"] or item["length_km"]
            elif not item.get("path") and o.get("path"):
                item["path"] = o["path"]
                item["shape_source"] = "osm"
                item["length_km"] = o["length_km"] or item["length_km"]
            if o.get("name") and len(item.get("name", "")) < 12:
                item["name"] = f"{ref} · {o['name']}"[:80]
            if o.get("n_stops", 0) > item.get("n_stops", 0):
                item["n_stops"] = o["n_stops"]
            item["osm_enriched"] = True
        else:
            item["osm_enriched"] = False
        out.append(item)
    return out
