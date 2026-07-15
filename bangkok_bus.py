"""
Bangkok Smart Bus Optimiser
---------------------------
Predicts passenger demand & travel times from traffic + historical ridership,
then optimises dynamic bus frequency under EV battery constraints and
quantifies fuel/electricity, fleet, labour, ridership, and revenue impact.

Network/ridership/traffic prefer real Namtang GTFS + OSM + MOT + Google/ORS
(see data/); falls back to synthetic corridors if cache/build fails.

Run:
    python bangkok_bus.py
    python -m data.build_dataset
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split

MODELS_CACHE = Path(__file__).resolve().parent / "data" / "cache" / "models.joblib"

warnings.filterwarnings("ignore", category=UserWarning)

RNG = np.random.default_rng(42)

# ---------------------------------------------------------------------------
# 1. Network data — real BMTA corridors (GTFS/OSM/MOT) with synthetic fallback
# ---------------------------------------------------------------------------

# Tuple: route_id, length_km, stops, base_demand/hour, fare_thb, diesel_l_per_km
SYNTHETIC_ROUTES = [
    ("R8", 22.0, 28, 180, 15.0, 0.42),
    ("R29", 18.5, 24, 160, 14.0, 0.40),
    ("R39", 26.0, 32, 210, 16.0, 0.45),
    ("R509", 15.0, 20, 140, 13.0, 0.38),
    ("R554", 30.0, 36, 120, 18.0, 0.48),
]

ROUTES: list[tuple] = list(SYNTHETIC_ROUTES)
ROUTE_RECORDS: list[dict[str, Any]] = []
DATA_SOURCES: dict[str, Any] = {
    "mode": "synthetic",
    "note": "Using synthetic fallback until real dataset is built.",
}
_TRAFFIC_PROFILES: dict[str, Any] | None = None

HOURS = list(range(5, 23))  # 05:00–22:00 operating day


def _records_to_tuples(records: list[dict[str, Any]]) -> list[tuple]:
    out = []
    for r in records:
        out.append(
            (
                r["route_id"],
                float(r["length_km"]),
                int(r["n_stops"]),
                int(r["base_demand"]),
                float(r["fare_thb"]),
                float(r["diesel_l_per_km"]),
            )
        )
    return out


def load_real_network(force: bool = False) -> bool:
    """Load Namtang GTFS + OSM + MOT dataset into ROUTES. Returns True on success."""
    global ROUTES, ROUTE_RECORDS, DATA_SOURCES, _TRAFFIC_PROFILES
    try:
        from data.build_dataset import ensure_dataset
        from data.traffic import TRAFFIC_CACHE
        import json

        ds = ensure_dataset(force=force)
        records = ds["routes"]
        if not records:
            return False
        ROUTES = _records_to_tuples(records)
        ROUTE_RECORDS = records
        meta = ds.get("meta") or {}
        DATA_SOURCES = {
            "mode": "real",
            "sources": meta.get("sources", {}),
            "mot_month": meta.get("mot_month"),
            "traffic_provider": meta.get("traffic_provider"),
            "disclaimer": meta.get("disclaimer"),
            "routes": [r["route_id"] for r in records],
            "n_historical_rows": meta.get("n_rows"),
        }
        if TRAFFIC_CACHE.exists():
            with open(TRAFFIC_CACHE, encoding="utf-8") as f:
                _TRAFFIC_PROFILES = json.load(f)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"Real network load failed ({exc}); keeping synthetic ROUTES.")
        DATA_SOURCES = {
            "mode": "synthetic",
            "error": str(exc),
            "note": "Fell back to synthetic BMTA-style corridors.",
        }
        return False


def bangkok_traffic_index(hour: int, day_of_week: int, route_id: str | None = None) -> float:
    """Traffic congestion index ~0.4 (free flow) to ~2.0 (severe jam)."""
    if _TRAFFIC_PROFILES and route_id:
        try:
            from data.traffic import traffic_index_for

            return traffic_index_for(_TRAFFIC_PROFILES, route_id, hour, day_of_week, 0.0)
        except Exception:
            pass
    if day_of_week < 5:
        if 7 <= hour <= 9:
            return 1.45 + 0.15 * np.sin((hour - 7) * np.pi / 2)
        if 16 <= hour <= 19:
            return 1.55 + 0.12 * np.sin((hour - 16) * np.pi / 3)
        if 11 <= hour <= 13:
            return 1.05
        return 0.75
    if 10 <= hour <= 18:
        return 1.10
    return 0.65


def bangkok_demand_multiplier(hour: int, day_of_week: int) -> float:
    """Ridership intensity relative to route base demand."""
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


def _generate_synthetic_historical(n_days: int = 60) -> pd.DataFrame:
    """Legacy synthetic panel (fallback)."""
    rows = []
    for day in range(n_days):
        dow = day % 7
        rain = float(RNG.random() < 0.18)
        for hour in HOURS:
            for route_id, length_km, stops, base_dem, fare, diesel in ROUTES:
                traffic = bangkok_traffic_index(hour, dow, route_id)
                traffic *= 1.0 + 0.22 * rain
                traffic += float(RNG.normal(0, 0.05))
                traffic = float(np.clip(traffic, 0.4, 2.0))

                demand_mult = bangkok_demand_multiplier(hour, dow)
                demand_mult *= 1.0 - 0.12 * rain
                demand_mult += float(RNG.normal(0, 0.08))
                demand_mult = float(np.clip(demand_mult, 0.2, 2.2))

                passengers = max(
                    0,
                    int(base_dem * demand_mult * (1 + float(RNG.normal(0, 0.06)))),
                )
                free_flow_min = length_km * 2.1
                dwell_min = stops * 0.35 * (0.8 + 0.4 * (passengers / (base_dem + 1)))
                travel_min = (free_flow_min * traffic) + dwell_min + float(
                    RNG.normal(0, 2.5)
                )
                travel_min = max(length_km * 1.6, travel_min)

                rows.append(
                    {
                        "day": day,
                        "day_of_week": dow,
                        "hour": hour,
                        "route_id": route_id,
                        "length_km": length_km,
                        "n_stops": stops,
                        "traffic_index": round(traffic, 3),
                        "rain": rain,
                        "passengers": passengers,
                        "travel_time_min": round(travel_min, 1),
                        "fare_thb": fare,
                        "diesel_l_per_km": diesel,
                    }
                )
    return pd.DataFrame(rows)


def generate_historical_data(n_days: int = 60) -> pd.DataFrame:
    """
    Prefer real cached panel (MOT-calibrated demand + traffic profiles + Open-Meteo rain).
    Falls back to synthetic if real dataset unavailable.
    """
    try:
        from data.build_dataset import ensure_dataset

        if not ROUTE_RECORDS:
            load_real_network()
        ds = ensure_dataset()
        hist = ds.get("historical")
        if hist is not None and len(hist) > 100:
            DATA_SOURCES["mode"] = DATA_SOURCES.get("mode") or "real"
            return hist
    except Exception as e:  # noqa: BLE001
        print(f"Real historical unavailable ({e}); using synthetic.")
    return _generate_synthetic_historical(n_days)


# ---------------------------------------------------------------------------
# 2. AI models: demand + travel-time prediction
# ---------------------------------------------------------------------------

FEATURE_COLS = [
    "hour",
    "day_of_week",
    "length_km",
    "n_stops",
    "traffic_index",
    "rain",
    "route_code",
]


def encode_routes(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    route_map = {r[0]: i for i, r in enumerate(ROUTES)}
    out["route_code"] = out["route_id"].map(route_map)
    return out


@dataclass
class PredictionModels:
    demand_model: RandomForestRegressor
    travel_model: GradientBoostingRegressor
    demand_mae: float
    demand_r2: float
    travel_mae: float
    travel_r2: float


def train_models(df: pd.DataFrame) -> PredictionModels:
    data = encode_routes(df)
    X = data[FEATURE_COLS]
    y_demand = data["passengers"]
    y_travel = data["travel_time_min"]

    Xd_tr, Xd_te, yd_tr, yd_te = train_test_split(
        X, y_demand, test_size=0.2, random_state=42
    )
    Xt_tr, Xt_te, yt_tr, yt_te = train_test_split(
        X, y_travel, test_size=0.2, random_state=42
    )

    demand_model = RandomForestRegressor(
        n_estimators=120, max_depth=12, random_state=42, n_jobs=-1
    )
    travel_model = GradientBoostingRegressor(
        n_estimators=100, max_depth=4, learning_rate=0.08, random_state=42
    )
    demand_model.fit(Xd_tr, yd_tr)
    travel_model.fit(Xt_tr, yt_tr)

    yd_pred = demand_model.predict(Xd_te)
    yt_pred = travel_model.predict(Xt_te)

    return PredictionModels(
        demand_model=demand_model,
        travel_model=travel_model,
        demand_mae=float(mean_absolute_error(yd_te, yd_pred)),
        demand_r2=float(r2_score(yd_te, yd_pred)),
        travel_mae=float(mean_absolute_error(yt_te, yt_pred)),
        travel_r2=float(r2_score(yt_te, yt_pred)),
    )


def save_models(models: PredictionModels, path: Path | None = None) -> Path:
    """Persist trained models for fast cold starts (Render / Docker)."""
    dest = path or MODELS_CACHE
    dest.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "demand_model": models.demand_model,
            "travel_model": models.travel_model,
            "demand_mae": models.demand_mae,
            "demand_r2": models.demand_r2,
            "travel_mae": models.travel_mae,
            "travel_r2": models.travel_r2,
            "route_ids": [r[0] for r in ROUTES],
        },
        dest,
    )
    return dest


def load_models(path: Path | None = None) -> PredictionModels | None:
    """Load cached models if present and route set still matches."""
    src = path or MODELS_CACHE
    if not src.exists():
        return None
    try:
        blob = joblib.load(src)
        cached_routes = blob.get("route_ids") or []
        current = [r[0] for r in ROUTES]
        if cached_routes and cached_routes != current:
            print("Cached models route set differs — will retrain.")
            return None
        return PredictionModels(
            demand_model=blob["demand_model"],
            travel_model=blob["travel_model"],
            demand_mae=float(blob["demand_mae"]),
            demand_r2=float(blob["demand_r2"]),
            travel_mae=float(blob["travel_mae"]),
            travel_r2=float(blob["travel_r2"]),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Could not load model cache ({exc}); will retrain.")
        return None


def predict_horizon(
    models: PredictionModels,
    day_of_week: int = 1,
    rain: float = 0.0,
) -> pd.DataFrame:
    """Forecast demand & travel time for every route × hour tomorrow."""
    rows = []
    route_map = {r[0]: i for i, r in enumerate(ROUTES)}
    for hour in HOURS:
        for route_id, length_km, stops, *_rest in ROUTES:
            traffic = bangkok_traffic_index(hour, day_of_week, route_id) * (
                1 + 0.22 * rain
            )
            traffic = float(np.clip(traffic, 0.4, 2.5))
            feat = pd.DataFrame(
                [
                    {
                        "hour": hour,
                        "day_of_week": day_of_week,
                        "length_km": length_km,
                        "n_stops": stops,
                        "traffic_index": traffic,
                        "rain": rain,
                        "route_code": route_map[route_id],
                    }
                ]
            )
            dem = float(models.demand_model.predict(feat[FEATURE_COLS])[0])
            tt = float(models.travel_model.predict(feat[FEATURE_COLS])[0])
            rows.append(
                {
                    "route_id": route_id,
                    "hour": hour,
                    "traffic_index": round(traffic, 3),
                    "pred_passengers": max(0, round(dem)),
                    "pred_travel_min": round(max(5.0, tt), 1),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3. EV-aware frequency / dispatch optimisation
# ---------------------------------------------------------------------------

@dataclass
class EVBusSpec:
    battery_kwh: float = 300.0
    energy_kwh_per_km: float = 1.35  # loaded urban BEB typical range
    min_soc: float = 0.20  # keep 20% reserve
    charge_kw: float = 150.0  # depot DC fast charge
    # BMTA 10–12 m EV/AC TOR: seated ≥31; crush load modelled at 60
    bus_capacity: int = 60
    max_hours_shift: float = 8.0


try:
    from data.thai_assumptions import BMTA_BUS_CAPACITY as _BMTA_CAP

    EV = EVBusSpec(bus_capacity=int(_BMTA_CAP))
except Exception:
    EV = EVBusSpec()


@dataclass
class HourPlan:
    route_id: str
    hour: int
    frequency_per_hour: int  # buses dispatched this hour
    predicted_demand: int
    travel_min: float
    trips_needed_by_demand: int
    energy_kwh: float
    load_factor: float


def trips_from_demand(passengers: int, capacity: int, target_load: float = 0.75) -> int:
    """Minimum trips so average occupancy stays under target_load × capacity."""
    effective = max(1, int(capacity * target_load))
    return max(1, int(np.ceil(passengers / effective)))


def baseline_frequency(hour: int) -> int:
    """Fixed timetable many BMTA corridors still use (no AI)."""
    if 6 <= hour <= 9 or 16 <= hour <= 19:
        return 6
    if 10 <= hour <= 15:
        return 4
    return 2


def is_peak_hour(hour: int) -> bool:
    return (6 <= hour <= 9) or (16 <= hour <= 19)


def _buses_for_freq(freq: int, cycle_h: float) -> int:
    """Concurrent buses needed for a continuous loop at this frequency."""
    if freq <= 0:
        return 0
    return max(1, int(np.ceil(freq * cycle_h)))


def allocate_frequencies(
    forecast: pd.DataFrame,
    ideals_fn,
    fleet_size: int = 110,
    daily_energy_budget_kwh: float | None = None,
    enforce_energy: bool = True,
    peak_floor: bool = True,
) -> tuple[dict[tuple[str, int], int], dict]:
    """
    Allocate (route, hour) → frequency under concurrent fleet (+ optional energy).

    ideals_fn(row, hour) → desired trips/hr before capping.
    Cuts lowest-demand routes first when the fleet is tight.
    """
    route_meta = {r[0]: r for r in ROUTES}
    usable_kwh = EV.battery_kwh * (1.0 - EV.min_soc)
    if daily_energy_budget_kwh is None:
        daily_energy_budget_kwh = fleet_size * (usable_kwh + EV.charge_kw * 1.5)

    energy_remaining = float(daily_energy_budget_kwh) if enforce_energy else float("inf")
    busy_until: list[float] = []
    assigned: dict[tuple[str, int], int] = {}
    peak_buses = 0
    energy_used = 0.0

    rows_by_hour: dict[int, list[pd.Series]] = {h: [] for h in HOURS}
    for _, row in forecast.iterrows():
        rows_by_hour[int(row["hour"])].append(row)

    for hour in HOURS:
        busy_until = [t for t in busy_until if t > hour]
        available = max(0, fleet_size - len(busy_until))

        hour_rows = sorted(
            rows_by_hour[hour],
            key=lambda r: int(r["pred_passengers"]),
            reverse=True,
        )
        ideals: list[tuple[pd.Series, int, float, float]] = []
        for row in hour_rows:
            travel = float(row["pred_travel_min"])
            length_km = route_meta[row["route_id"]][1]
            need = int(ideals_fn(row, hour))
            cycle_h = (travel * 1.10) / 60.0
            e_trip = length_km * EV.energy_kwh_per_km * 1.05
            ideals.append((row, need, cycle_h, e_trip))

        freqs = [need for _, need, _, _ in ideals]

        def buses_for(freq_list: list[int]) -> int:
            return sum(
                _buses_for_freq(freq, cycle_h)
                for (_, _, cycle_h, _), freq in zip(ideals, freq_list)
            )

        def energy_for(freq_list: list[int]) -> float:
            return sum(f * e for (_, _, _, e), f in zip(ideals, freq_list))

        while True:
            over_fleet = buses_for(freqs) > available
            over_energy = energy_for(freqs) > energy_remaining
            if not over_fleet and not over_energy:
                break
            cut_idx = None
            if peak_floor and is_peak_hour(hour):
                floor = baseline_frequency(hour)
                for i in range(len(freqs) - 1, -1, -1):
                    if freqs[i] > floor:
                        cut_idx = i
                        break
            if cut_idx is None:
                for i in range(len(freqs) - 1, -1, -1):
                    if freqs[i] > 0:
                        cut_idx = i
                        break
            if cut_idx is None:
                break
            freqs[cut_idx] -= 1

        # If still over after cutting to zero on some routes, drop more zeros already handled.
        # Claim buses only for what we actually dispatch; never exceed remaining slots.
        claimed = 0
        for (row, _need, cycle_h, e_trip), freq in zip(ideals, freqs):
            n_buses = _buses_for_freq(freq, cycle_h)
            if claimed + n_buses > available:
                # Scale this route down until it fits (or shut it for the hour).
                while freq > 0 and claimed + _buses_for_freq(freq, cycle_h) > available:
                    freq -= 1
                n_buses = _buses_for_freq(freq, cycle_h)
            assigned[(row["route_id"], hour)] = freq
            energy_used += freq * e_trip
            energy_remaining -= freq * e_trip
            if n_buses:
                release = hour + cycle_h
                busy_until.extend([release] * n_buses)
                claimed += n_buses
        peak_buses = max(peak_buses, min(fleet_size, len(busy_until)))

    summary = {
        "fleet_size": fleet_size,
        "energy_budget_kwh": daily_energy_budget_kwh,
        "energy_used_kwh": energy_used,
        "energy_utilisation": (
            energy_used / daily_energy_budget_kwh if daily_energy_budget_kwh else 0.0
        ),
        "peak_buses_in_use": peak_buses,
    }
    return assigned, summary


def published_fixed_schedule(
    forecast: pd.DataFrame,
) -> tuple[dict[tuple[str, int], int], int]:
    """
    Full Fixed published timetable and the fleet both modes share.

    Fleet = max over hours of concurrent buses needed that hour if every
    corridor runs baseline_frequency(hour). (Peak-hour fleet formula —
    same vehicle pool for Fixed and AI.)
    """
    assigned: dict[tuple[str, int], int] = {}
    peak_buses = 0

    rows_by_hour: dict[int, list[pd.Series]] = {h: [] for h in HOURS}
    for _, row in forecast.iterrows():
        rows_by_hour[int(row["hour"])].append(row)

    for hour in HOURS:
        hour_buses = 0
        for row in rows_by_hour[hour]:
            freq = baseline_frequency(hour)
            cycle_h = (float(row["pred_travel_min"]) * 1.10) / 60.0
            assigned[(row["route_id"], hour)] = freq
            hour_buses += _buses_for_freq(freq, cycle_h)
        peak_buses = max(peak_buses, hour_buses)

    return assigned, max(1, peak_buses)


def fixed_timetable_under_fleet(
    forecast: pd.DataFrame, fleet_size: int = 110
) -> dict[tuple[str, int], int]:
    """Published Fixed frequencies, cut if fleet is below the Fixed requirement."""

    def ideals_fn(row: pd.Series, hour: int) -> int:
        return baseline_frequency(hour)

    assigned, _ = allocate_frequencies(
        forecast,
        ideals_fn,
        fleet_size=fleet_size,
        enforce_energy=False,
        peak_floor=False,
    )
    return assigned


def optimise_frequencies(
    forecast: pd.DataFrame,
    fleet_size: int | None = None,
    daily_energy_budget_kwh: float | None = None,
) -> tuple[list[HourPlan], dict]:
    """
    AI+EV plan with the same bus pool as Fixed.

    Fleet size comes from the Fixed published timetable (peak concurrent buses).
    Each hour starts at Fixed headways, then AI redeploys trips from low-load
    corridors to high-load ones — and can use spare buses that Fixed leaves
    idle off-peak — without ever exceeding that Fixed fleet.

    Efficiency = better allocation of the *same* vehicles.
    """
    fixed_freqs, fixed_fleet = published_fixed_schedule(forecast)
    if fleet_size is None:
        fleet_size = fixed_fleet

    route_meta = {r[0]: r for r in ROUTES}
    usable_kwh = EV.battery_kwh * (1.0 - EV.min_soc)
    if daily_energy_budget_kwh is None:
        daily_energy_budget_kwh = fleet_size * (usable_kwh + EV.charge_kw * 1.5)

    rows_by_hour: dict[int, list[pd.Series]] = {h: [] for h in HOURS}
    for _, row in forecast.iterrows():
        rows_by_hour[int(row["hour"])].append(row)

    assigned: dict[tuple[str, int], int] = {}
    peak_buses = 0
    energy_used = 0.0
    redeploy_moves = 0
    spare_boosts = 0
    max_freq = 24
    min_freq = 1

    for hour in HOURS:
        entries: list[dict] = []
        for row in rows_by_hour[hour]:
            rid = row["route_id"]
            cycle_h = (float(row["pred_travel_min"]) * 1.10) / 60.0
            length_km = route_meta[rid][1]
            entries.append(
                {
                    "route_id": rid,
                    "demand": int(row["pred_passengers"]),
                    "cycle_h": cycle_h,
                    "e_trip": length_km * EV.energy_kwh_per_km * 1.05,
                    "freq": int(fixed_freqs.get((rid, hour), baseline_frequency(hour))),
                }
            )

        def buses_needed(items: list[dict] = entries) -> int:
            return sum(_buses_for_freq(e["freq"], e["cycle_h"]) for e in items)

        def load_of(e: dict) -> float:
            seats = max(e["freq"], 1) * EV.bus_capacity
            return e["demand"] / seats

        def unserved_of(e: dict) -> int:
            return max(0, e["demand"] - e["freq"] * EV.bus_capacity)

        available = fleet_size  # same pool every hour; Fixed often under-uses off-peak

        # 1) Use spare fleet: add trips to the busiest corridor while buses remain
        for _ in range(80):
            if buses_needed() >= available:
                break
            receiver = max(entries, key=lambda e: (unserved_of(e), load_of(e), e["demand"]))
            if unserved_of(receiver) <= 0 and load_of(receiver) <= 0.85:
                break
            if receiver["freq"] >= max_freq:
                break
            receiver["freq"] += 1
            if buses_needed() > available:
                receiver["freq"] -= 1
                break
            spare_boosts += 1
            redeploy_moves += 1

        # 2) Rebalance: move one trip/hr from lowest load → highest load / most unserved
        for _ in range(120):
            if len(entries) < 2:
                break
            receiver = max(entries, key=lambda e: (unserved_of(e), load_of(e), e["demand"]))
            donors = [
                e
                for e in entries
                if e["route_id"] != receiver["route_id"] and e["freq"] > min_freq
            ]
            if not donors:
                break
            # Prefer donors with spare capacity; else least-busy overloaded
            spare_donors = [e for e in donors if unserved_of(e) == 0]
            donor = min(
                spare_donors or donors,
                key=lambda e: (load_of(e), e["demand"], -e["freq"]),
            )
            # Stop when loads/unserved are already as balanced as discrete freqs allow
            if unserved_of(receiver) <= unserved_of(donor) and load_of(receiver) <= load_of(
                donor
            ) + 0.08:
                break
            if receiver["freq"] >= max_freq:
                break

            receiver["freq"] += 1
            donor["freq"] -= 1
            if buses_needed() > available:
                receiver["freq"] -= 1
                donor["freq"] += 1
                # Try sacrificing two donor trips if receiver cycle is longer
                if donor["freq"] > min_freq:
                    donor["freq"] -= 1
                    receiver["freq"] += 1
                    if buses_needed() > available:
                        donor["freq"] += 1
                        receiver["freq"] -= 1
                        break
                else:
                    break
            redeploy_moves += 1

        hour_peak = buses_needed()
        peak_buses = max(peak_buses, hour_peak)

        for e in entries:
            assigned[(e["route_id"], hour)] = e["freq"]
            energy_used += e["freq"] * e["e_trip"]

    plans: list[HourPlan] = []
    for _, row in forecast.sort_values(["route_id", "hour"]).iterrows():
        route_id = row["route_id"]
        hour = int(row["hour"])
        demand = int(row["pred_passengers"])
        travel = float(row["pred_travel_min"])
        length_km = route_meta[route_id][1]
        freq = assigned.get((route_id, hour), baseline_frequency(hour))
        energy = freq * length_km * EV.energy_kwh_per_km * 1.05
        seats = max(freq, 0) * EV.bus_capacity
        load = demand / seats if seats else 0.0
        plans.append(
            HourPlan(
                route_id=route_id,
                hour=hour,
                frequency_per_hour=max(freq, 0),
                predicted_demand=demand,
                travel_min=travel,
                trips_needed_by_demand=trips_from_demand(demand, EV.bus_capacity),
                energy_kwh=energy,
                load_factor=min(load, 1.5),
            )
        )

    summary = {
        "fleet_size": fleet_size,
        "fleet_from_fixed": fixed_fleet,
        "fleet_source": "Fixed published timetable (peak concurrent buses)",
        "energy_budget_kwh": daily_energy_budget_kwh,
        "energy_used_kwh": energy_used,
        "energy_utilisation": (
            energy_used / daily_energy_budget_kwh if daily_energy_budget_kwh else 0.0
        ),
        "peak_buses_in_use": peak_buses,
        "redeploy_moves": redeploy_moves,
        "spare_boosts": spare_boosts,
        "avg_load_factor": float(np.mean([p.load_factor for p in plans])),
        "total_vehicle_trips": sum(p.frequency_per_hour for p in plans),
    }
    return plans, summary


# ---------------------------------------------------------------------------
# 4. Business impact: fuel/electricity, fleet, labour, ridership, revenue
# ---------------------------------------------------------------------------

@dataclass
class BusinessAssumptions:
    # Defaults overwritten below from data.thai_assumptions (official Thai sources)
    electricity_thb_per_kwh: float = 2.766
    diesel_thb_per_litre: float = 34.94
    driver_thb_per_hour: float = 120.0
    diesel_co2_kg_per_l: float = 2.68
    grid_co2_kg_per_kwh: float = 0.4758
    # Unserved demand converts to lost ridership; better wait time wins riders
    elasticity_wait_time: float = -0.25  # % ridership per % wait change
    fare_capture_rate: float = 0.92  # paid boardings


try:
    from data.thai_assumptions import (
        ASSUMPTION_SOURCES,
        DIESEL_CO2_KG_PER_LITRE,
        DIESEL_THB_PER_LITRE,
        ELECTRICITY_THB_PER_KWH,
        GRID_CO2_KG_PER_KWH,
    )

    BIZ = BusinessAssumptions(
        electricity_thb_per_kwh=ELECTRICITY_THB_PER_KWH,
        diesel_thb_per_litre=DIESEL_THB_PER_LITRE,
        diesel_co2_kg_per_l=DIESEL_CO2_KG_PER_LITRE,
        grid_co2_kg_per_kwh=GRID_CO2_KG_PER_KWH,
    )
except Exception:
    ASSUMPTION_SOURCES = {}
    BIZ = BusinessAssumptions()


def business_comparison(
    forecast: pd.DataFrame,
    plans: list[HourPlan],
    fleet_size: int | None = None,
    fixed_freqs: dict[tuple[str, int], int] | None = None,
) -> dict:
    """Compare diesel Fixed vs EV+AI under the same Fixed-derived fleet."""
    pub_fixed, fixed_fleet = published_fixed_schedule(forecast)
    if fleet_size is None:
        fleet_size = fixed_fleet
    if fixed_freqs is None:
        # Fleet is sized for Fixed → run the full published timetable
        fixed_freqs = pub_fixed if fleet_size >= fixed_fleet else fixed_timetable_under_fleet(
            forecast, fleet_size=fleet_size
        )

    # Baseline: diesel + Fixed published timetable (same bus pool AI must share)
    base = {
        "vehicle_km": 0.0,
        "diesel_l": 0.0,
        "energy_kwh": 0.0,
        "driver_hours": 0.0,
        "served": 0,
        "unserved": 0,
        "wait_sum": 0.0,
        "demand_w": 0.0,
        "revenue": 0.0,
        "trips": 0,
    }
    route_meta = {r[0]: r for r in ROUTES}
    for _, row in forecast.iterrows():
        rid, hour = row["route_id"], int(row["hour"])
        demand = int(row["pred_passengers"])
        travel = float(row["pred_travel_min"])
        length_km, _, _, fare, diesel_rate = route_meta[rid][1:]
        freq = max(0, int(fixed_freqs.get((rid, hour), baseline_frequency(hour))))
        cap = freq * EV.bus_capacity
        boarded = min(demand, cap)
        base["served"] += boarded
        base["unserved"] += max(0, demand - cap)
        wait = (60.0 / max(freq, 1)) / 2.0 if freq > 0 else 45.0
        base["wait_sum"] += wait * demand  # demand-weighted wait
        base["demand_w"] += demand
        base["vehicle_km"] += freq * length_km
        base["diesel_l"] += freq * length_km * diesel_rate
        base["driver_hours"] += freq * (travel * 1.15) / 60.0
        base["revenue"] += boarded * fare * BIZ.fare_capture_rate
        base["trips"] += freq

    opt = {
        "vehicle_km": 0.0,
        "diesel_l": 0.0,
        "energy_kwh": 0.0,
        "driver_hours": 0.0,
        "served": 0,
        "unserved": 0,
        "wait_sum": 0.0,
        "demand_w": 0.0,
        "revenue": 0.0,
        "trips": 0,
    }
    for p in plans:
        length_km, _, _, fare, _ = route_meta[p.route_id][1:]
        cap = p.frequency_per_hour * EV.bus_capacity
        boarded = min(p.predicted_demand, cap)
        opt["served"] += boarded
        opt["unserved"] += max(0, p.predicted_demand - cap)
        freq = max(p.frequency_per_hour, 0)
        wait = (60.0 / max(freq, 1)) / 2.0 if freq > 0 else 45.0
        opt["wait_sum"] += wait * p.predicted_demand
        opt["demand_w"] += p.predicted_demand
        opt["vehicle_km"] += freq * length_km
        opt["energy_kwh"] += p.energy_kwh
        opt["driver_hours"] += freq * (p.travel_min * 1.15) / 60.0
        opt["revenue"] += boarded * fare * BIZ.fare_capture_rate
        opt["trips"] += freq

    base_wait = base["wait_sum"] / max(base["demand_w"], 1)
    opt_wait = opt["wait_sum"] / max(opt["demand_w"], 1)
    wait_pct_change = (opt_wait - base_wait) / max(base_wait, 1e-6)
    # Ridership response to wait-time change (elasticity can raise or lower)
    induced = opt["served"] * BIZ.elasticity_wait_time * wait_pct_change
    opt_ridership = max(opt["served"] + induced, opt["served"] * 0.95)
    opt_revenue_adj = opt["revenue"] * (opt_ridership / max(opt["served"], 1))

    base_cost = (
        base["diesel_l"] * BIZ.diesel_thb_per_litre
        + base["driver_hours"] * BIZ.driver_thb_per_hour
    )
    opt_cost = (
        opt["energy_kwh"] * BIZ.electricity_thb_per_kwh
        + opt["driver_hours"] * BIZ.driver_thb_per_hour
    )
    base_co2 = base["diesel_l"] * BIZ.diesel_co2_kg_per_l
    opt_co2 = opt["energy_kwh"] * BIZ.grid_co2_kg_per_kwh

    # Fleet utilisation: trips per available bus-hour slot
    fleet = max(fleet_size, 1)
    base_util = base["trips"] / (fleet * len(HOURS))
    opt_util = opt["trips"] / (fleet * len(HOURS))

    return {
        "baseline": {
            "mode": "Diesel + Fixed published timetable",
            "vehicle_km": round(base["vehicle_km"], 1),
            "fuel_or_energy_cost_thb": round(
                base["diesel_l"] * BIZ.diesel_thb_per_litre, 0
            ),
            "labour_cost_thb": round(base["driver_hours"] * BIZ.driver_thb_per_hour, 0),
            "operating_cost_thb": round(base_cost, 0),
            "served_passengers": base["served"],
            "unserved_passengers": base["unserved"],
            "avg_wait_min": round(base_wait, 2),
            "revenue_thb": round(base["revenue"], 0),
            "co2_kg": round(base_co2, 0),
            "fleet_utilisation": round(base_util, 3),
            "driver_hours": round(base["driver_hours"], 1),
            "diesel_litres": round(base["diesel_l"], 1),
        },
        "optimised": {
            "mode": "EV + AI dynamic frequency",
            "vehicle_km": round(opt["vehicle_km"], 1),
            "fuel_or_energy_cost_thb": round(
                opt["energy_kwh"] * BIZ.electricity_thb_per_kwh, 0
            ),
            "labour_cost_thb": round(opt["driver_hours"] * BIZ.driver_thb_per_hour, 0),
            "operating_cost_thb": round(opt_cost, 0),
            "served_passengers": int(round(opt_ridership)),
            "unserved_passengers": opt["unserved"],
            "avg_wait_min": round(opt_wait, 2),
            "revenue_thb": round(opt_revenue_adj, 0),
            "co2_kg": round(opt_co2, 0),
            "fleet_utilisation": round(opt_util, 3),
            "driver_hours": round(opt["driver_hours"], 1),
            "electricity_kwh": round(opt["energy_kwh"], 1),
            "induced_riders_from_wait": round(max(0, induced), 0),
        },
        "savings_per_day": {
            "operating_cost_thb": round(base_cost - opt_cost, 0),
            "energy_vs_diesel_thb": round(
                base["diesel_l"] * BIZ.diesel_thb_per_litre
                - opt["energy_kwh"] * BIZ.electricity_thb_per_kwh,
                0,
            ),
            "labour_thb": round(
                (base["driver_hours"] - opt["driver_hours"]) * BIZ.driver_thb_per_hour,
                0,
            ),
            "vehicle_km": round(base["vehicle_km"] - opt["vehicle_km"], 1),
            "co2_kg": round(base_co2 - opt_co2, 0),
            "extra_revenue_thb": round(opt_revenue_adj - base["revenue"], 0),
            "extra_riders": int(round(opt_ridership - base["served"])),
            "wait_minutes_reduced": round(base_wait - opt_wait, 2),
        },
        "annualised_250_days": {},
    }


# ---------------------------------------------------------------------------
# 5. Reporting
# ---------------------------------------------------------------------------

def print_section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    print_section("BANGKOK SMART BUS OPTIMISER")
    print("Problem: congestion-driven delay & mismatched bus supply ↔ demand")
    print("Stack:   ML demand/travel models → EV-constrained frequency optimiser")
    print("         → quantified ops & revenue impact")

    load_real_network()
    print(f"Data mode: {DATA_SOURCES.get('mode')} | {DATA_SOURCES.get('sources') or DATA_SOURCES.get('note')}")

    # --- Data ---
    print_section("1. Historical data (BMTA GTFS / MOT / traffic)")
    hist = generate_historical_data(60)
    print(f"Observations: {len(hist):,}  |  Routes: {len(ROUTES)}  |  Days: 60")
    print(hist.groupby("route_id")[["passengers", "travel_time_min"]].mean().round(1))

    # --- Models ---
    print_section("2. AI prediction models")
    models = train_models(hist)
    print(
        f"Demand  (RandomForest):     MAE = {models.demand_mae:.1f} passengers  "
        f"|  R² = {models.demand_r2:.3f}"
    )
    print(
        f"Travel  (GradientBoosting): MAE = {models.travel_mae:.1f} min         "
        f"|  R² = {models.travel_r2:.3f}"
    )

    forecast = predict_horizon(models, day_of_week=1, rain=0.0)  # Tuesday dry
    sample_route = ROUTES[0][0]
    print(f"\nSample forecast (Route {sample_route}, peak hours):")
    sample = forecast[
        (forecast.route_id == sample_route) & (forecast.hour.isin([8, 12, 18]))
    ]
    print(sample.to_string(index=False))

    # --- Optimisation ---
    print_section("3. EV-aware frequency optimisation")
    print(
        f"EV bus: {EV.battery_kwh:.0f} kWh battery, "
        f"{EV.energy_kwh_per_km} kWh/km, min SOC {EV.min_soc:.0%}, "
        f"capacity {EV.bus_capacity}"
    )
    plans, opt_sum = optimise_frequencies(forecast)
    print(
        f"Energy used: {opt_sum['energy_used_kwh']:.0f} / "
        f"{opt_sum['energy_budget_kwh']:.0f} kWh "
        f"({opt_sum['energy_utilisation']:.1%} of daily budget)"
    )
    print(
        f"Fleet from Fixed timetable: {opt_sum['fleet_from_fixed']} buses · "
        f"AI peak in use: {opt_sum['peak_buses_in_use']} / {opt_sum['fleet_size']}"
    )
    print(f"Average load factor: {opt_sum['avg_load_factor']:.2f}")
    print(f"Total vehicle-trips scheduled: {opt_sum['total_vehicle_trips']}")

    print(f"\nDynamic vs fixed frequency — Route {sample_route}:")
    print(f"{'Hour':>4}  {'Demand':>7}  {'Fixed':>5}  {'AI+EV':>5}  {'Load':>5}")
    for p in plans:
        if p.route_id != sample_route:
            continue
        fixed = baseline_frequency(p.hour)
        print(
            f"{p.hour:4d}  {p.predicted_demand:7d}  {fixed:5d}  "
            f"{p.frequency_per_hour:5d}  {p.load_factor:5.2f}"
        )

    # Optional simple route adjustment hint
    print_section("3b. Route adjustment recommendations")
    by_route = (
        forecast.groupby("route_id")
        .agg(
            total_demand=("pred_passengers", "sum"),
            avg_traffic=("traffic_index", "mean"),
            avg_travel=("pred_travel_min", "mean"),
        )
        .reset_index()
    )
    by_route["demand_per_min"] = by_route["total_demand"] / by_route["avg_travel"]
    congested = by_route.sort_values("avg_traffic", ascending=False).iloc[0]
    sparse = by_route.sort_values("total_demand").iloc[0]
    busy = by_route.sort_values("total_demand", ascending=False).iloc[0]
    print(
        f"• Short-turn / express candidate: {congested['route_id']} "
        f"(avg traffic index {congested['avg_traffic']:.2f}) — "
        "skip low-board stops in peaks."
    )
    print(
        f"• Frequency boost: {busy['route_id']} "
        f"({int(busy['total_demand']):,} predicted boardings/day)."
    )
    print(
        f"• Frequency trim / vehicle reallocation: {sparse['route_id']} "
        f"({int(sparse['total_demand']):,} boardings) → redeploy to {busy['route_id']}."
    )

    # --- Business ---
    print_section("4. Business impact (one weekday)")
    biz = business_comparison(forecast, plans)
    b, o, s = biz["baseline"], biz["optimised"], biz["savings_per_day"]

    print(f"\n{'Metric':<28} {'Baseline':>14} {'AI+EV opt':>14} {'Savings':>12}")
    print("-" * 70)
    rows = [
        ("Mode", b["mode"], o["mode"], ""),
        ("Vehicle-km", f"{b['vehicle_km']:,.0f}", f"{o['vehicle_km']:,.0f}", f"{s['vehicle_km']:+,.0f}"),
        (
            "Energy/fuel cost (THB)",
            f"{b['fuel_or_energy_cost_thb']:,.0f}",
            f"{o['fuel_or_energy_cost_thb']:,.0f}",
            f"{s['energy_vs_diesel_thb']:+,.0f}",
        ),
        (
            "Labour cost (THB)",
            f"{b['labour_cost_thb']:,.0f}",
            f"{o['labour_cost_thb']:,.0f}",
            f"{s['labour_thb']:+,.0f}",
        ),
        (
            "Operating cost (THB)",
            f"{b['operating_cost_thb']:,.0f}",
            f"{o['operating_cost_thb']:,.0f}",
            f"{s['operating_cost_thb']:+,.0f}",
        ),
        (
            "Fleet utilisation",
            f"{b['fleet_utilisation']:.3f}",
            f"{o['fleet_utilisation']:.3f}",
            f"{o['fleet_utilisation'] - b['fleet_utilisation']:+.3f}",
        ),
        (
            "Avg wait (min)",
            f"{b['avg_wait_min']:.2f}",
            f"{o['avg_wait_min']:.2f}",
            f"{s['wait_minutes_reduced']:+.2f}",
        ),
        (
            "Served riders",
            f"{b['served_passengers']:,}",
            f"{o['served_passengers']:,}",
            f"{s['extra_riders']:+,}",
        ),
        (
            "Revenue (THB)",
            f"{b['revenue_thb']:,.0f}",
            f"{o['revenue_thb']:,.0f}",
            f"{s['extra_revenue_thb']:+,.0f}",
        ),
        (
            "CO₂ (kg)",
            f"{b['co2_kg']:,.0f}",
            f"{o['co2_kg']:,.0f}",
            f"{s['co2_kg']:+,.0f}",
        ),
    ]
    for label, bv, ov, dv in rows:
        print(f"{label:<28} {bv:>14} {ov:>14} {dv:>12}")

    annual_ops = s["operating_cost_thb"] * 250
    annual_rev = s["extra_revenue_thb"] * 250
    print_section("5. Annualised estimate (250 operating days, 5 corridors)")
    print(f"Operating cost savings:     THB {annual_ops:>12,.0f}")
    print(f"Incremental revenue:        THB {annual_rev:>12,.0f}")
    print(f"Combined annual value:      THB {annual_ops + annual_rev:>12,.0f}")
    print(f"CO₂ avoided:                {s['co2_kg'] * 250:>12,.0f} kg")
    print(
        f"Fleet utilisation:          "
        f"{b['fleet_utilisation']:.1%} → {o['fleet_utilisation']:.1%} "
        "(trips per bus-hour capacity)"
    )

    print_section("Notes / next steps for real deployment")
    print("• Replace synthetic series with BMTA ridership (MOT Data Catalog) +")
    print("  corridor speeds from GPS/AVL or Google/HERE traffic feeds.")
    print("• Retrain models nightly; re-optimise frequencies every 15–30 min.")
    print("• Add depot charger queuing & battery ageing into the energy budget.")
    print("• Validate elasticity & fare assumptions with A/B trials on 1–2 routes.")
    print()


if __name__ == "__main__":
    main()
