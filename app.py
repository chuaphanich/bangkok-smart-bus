"""
Bangkok Smart Bus — web dashboard

Local:
    .venv_bangkok/bin/python app.py
    → http://127.0.0.1:5050

Production (Render / Docker):
    gunicorn -b 0.0.0.0:$PORT -w 1 --timeout 120 app:app
"""

from __future__ import annotations

import os
from dataclasses import asdict
from math import ceil
from typing import Any

from flask import Flask, jsonify, render_template, request

import bangkok_bus as bb

app = Flask(__name__)

_state: dict[str, Any] = {
    "models": None,
    "ready": False,
    "error": None,
}


def _bootstrap() -> None:
    try:
        print("Loading real BMTA/OSM/MOT/traffic network…")
        ok = bb.load_real_network()
        if not ok:
            print("Warning: real network unavailable — synthetic fallback.")

        cached = bb.load_models()
        if cached is not None:
            print("Loaded trained models from cache.")
            _state["models"] = cached
        else:
            print("Training demand & travel models…")
            hist = bb.generate_historical_data(60)
            models = bb.train_models(hist)
            bb.save_models(models)
            print(f"Models saved → {bb.MODELS_CACHE}")
            _state["models"] = models

        _state["ready"] = True
        print(f"Ready. Data mode={bb.DATA_SOURCES.get('mode')}")
    except Exception as exc:  # noqa: BLE001 — surface to UI
        _state["error"] = str(exc)
        _state["ready"] = False


def _route_meta() -> list[dict[str, Any]]:
    if bb.ROUTE_RECORDS:
        out = []
        for r in bb.ROUTE_RECORDS:
            out.append(
                {
                    "route_id": r["route_id"],
                    "name": r.get("name", r["route_id"]),
                    "color": r.get("color", "#888"),
                    "path": r.get("path", []),
                    "length_km": r["length_km"],
                    "n_stops": r["n_stops"],
                    "base_demand": r["base_demand"],
                    "fare_thb": r["fare_thb"],
                    "diesel_l_per_km": r["diesel_l_per_km"],
                    "shape_source": r.get("shape_source"),
                    "daily_boardings_prior": r.get("daily_boardings_prior"),
                }
            )
        return out

    # Synthetic geometry fallback (same as legacy)
    fallback_geo = {
        "R8": {"name": "Victory Monument → Bang Sue", "color": "#E85D04", "path": [[13.7649, 100.5383], [13.8355, 100.5410]]},
        "R29": {"name": "Siam → Lat Phrao", "color": "#2A9D8F", "path": [[13.7460, 100.5340], [13.8125, 100.5860]]},
        "R39": {"name": "Silom → Ratchadapisek", "color": "#4C6EF5", "path": [[13.7240, 100.5280], [13.8000, 100.5865]]},
        "R509": {"name": "Tha Phra → MBK", "color": "#D62828", "path": [[13.7305, 100.4700], [13.7465, 100.5325]]},
        "R554": {"name": "Din Daeng → Min Buri", "color": "#9B5DE5", "path": [[13.7690, 100.5530], [13.8135, 100.6900]]},
    }
    out = []
    for route_id, length_km, stops, base_dem, fare, diesel in bb.ROUTES:
        geo = fallback_geo.get(route_id, {})
        out.append(
            {
                "route_id": route_id,
                "name": geo.get("name", route_id),
                "color": geo.get("color", "#888"),
                "path": geo.get("path", []),
                "length_km": length_km,
                "n_stops": stops,
                "base_demand": base_dem,
                "fare_thb": fare,
                "diesel_l_per_km": diesel,
            }
        )
    return out


def _run_optimisation(
    day_of_week: int = 1,
    rain: float = 0.0,
) -> dict[str, Any]:
    models = _state["models"]
    if models is None:
        raise RuntimeError("Models not ready")

    forecast = bb.predict_horizon(models, day_of_week=day_of_week, rain=rain)
    # Fleet size comes from Fixed published headways × predicted cycle times.
    # AI gets the same number of buses and must win by reallocating them.
    fixed_freqs, fleet_size = bb.published_fixed_schedule(forecast)
    plans, opt_sum = bb.optimise_frequencies(forecast, fleet_size=fleet_size)
    biz = bb.business_comparison(
        forecast, plans, fleet_size=fleet_size, fixed_freqs=fixed_freqs
    )

    by_route = (
        forecast.groupby("route_id")
        .agg(
            total_demand=("pred_passengers", "sum"),
            avg_traffic=("traffic_index", "mean"),
            avg_travel=("pred_travel_min", "mean"),
        )
        .reset_index()
    )
    congested = by_route.sort_values("avg_traffic", ascending=False).iloc[0]
    sparse = by_route.sort_values("total_demand").iloc[0]
    busy = by_route.sort_values("total_demand", ascending=False).iloc[0]
    recommendations = [
        {
            "type": "express",
            "route_id": str(congested["route_id"]),
            "text": (
                f"Short-turn / express candidate: {congested['route_id']} "
                f"(avg traffic {congested['avg_traffic']:.2f}) — "
                "skip low-board stops in peaks."
            ),
        },
        {
            "type": "boost",
            "route_id": str(busy["route_id"]),
            "text": (
                f"Frequency boost: {busy['route_id']} "
                f"({int(busy['total_demand']):,} predicted boardings/day)."
            ),
        },
        {
            "type": "trim",
            "route_id": str(sparse["route_id"]),
            "text": (
                f"Frequency trim: {sparse['route_id']} "
                f"({int(sparse['total_demand']):,} boardings) → "
                f"redeploy to {busy['route_id']}."
            ),
        },
    ]

    def _buses_at(freq: int, travel_min: float) -> int:
        if freq <= 0:
            return 0
        cycle_h = (float(travel_min) * 1.10) / 60.0
        return max(1, int(ceil(freq * cycle_h)))

    hourly = []
    for p in plans:
        fx = int(fixed_freqs.get((p.route_id, p.hour), bb.baseline_frequency(p.hour)))
        hourly.append(
            {
                **asdict(p),
                "fixed_frequency": fx,
                "published_frequency": bb.baseline_frequency(p.hour),
                "is_peak": bb.is_peak_hour(p.hour),
                "ai_buses": _buses_at(p.frequency_per_hour, p.travel_min),
                "fixed_buses": _buses_at(fx, p.travel_min),
            }
        )

    # Peak-hour fleet is the daily depot size; in-use varies by hour
    buses_by_hour: dict[int, dict[str, int]] = {}
    for h in bb.HOURS:
        rows = [r for r in hourly if r["hour"] == h]
        buses_by_hour[h] = {
            "fixed": sum(r["fixed_buses"] for r in rows),
            "ai": sum(r["ai_buses"] for r in rows),
        }
    opt_sum = dict(opt_sum)
    opt_sum["buses_by_hour"] = buses_by_hour

    from data.stop_wait import attach_stop_waits

    route_meta = _route_meta()
    hourly, stop_meta = attach_stop_waits(
        route_meta, hourly, day_of_week=day_of_week, rain=rain
    )
    data_sources = dict(bb.DATA_SOURCES)
    data_sources["stop_wait"] = (
        f"Per-stop wait from corridor traffic ({stop_meta.get('stop_traffic_source')})"
    )

    forecast_rows = forecast.to_dict(orient="records")
    s = biz["savings_per_day"]
    annual = {
        "operating_cost_thb": s["operating_cost_thb"] * 250,
        "extra_revenue_thb": s["extra_revenue_thb"] * 250,
        "combined_thb": (s["operating_cost_thb"] + s["extra_revenue_thb"]) * 250,
        "co2_kg": s["co2_kg"] * 250,
    }

    return {
        "routes": route_meta,
        "hours": bb.HOURS,
        "forecast": forecast_rows,
        "plans": hourly,
        "optimisation": opt_sum,
        "business": biz,
        "annual": annual,
        "recommendations": recommendations,
        "models": {
            "demand_mae": models.demand_mae,
            "demand_r2": models.demand_r2,
            "travel_mae": models.travel_mae,
            "travel_r2": models.travel_r2,
        },
        "params": {
            "day_of_week": day_of_week,
            "rain": rain,
            "fleet_size": fleet_size,
            "fleet_source": "Fixed published timetable",
        },
        "ev": asdict(bb.EV),
        "data_sources": data_sources,
        "stop_wait_meta": stop_meta,
        "assumptions": getattr(bb, "ASSUMPTION_SOURCES", {}),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify(
        {
            "ready": _state["ready"],
            "error": _state["error"],
            "data_sources": bb.DATA_SOURCES,
        }
    )


@app.route("/api/optimise")
def optimise():
    if not _state["ready"]:
        return jsonify({"error": _state["error"] or "Models still loading"}), 503

    day = int(request.args.get("day_of_week", 1))
    rain = float(request.args.get("rain", 0.0))
    day = max(0, min(6, day))
    rain = 1.0 if rain >= 0.5 else 0.0

    try:
        payload = _run_optimisation(day_of_week=day, rain=rain)
        return jsonify(payload)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


# Bootstrap when imported by Gunicorn (production) or run via __main__
_bootstrap()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    host = os.environ.get("HOST", "127.0.0.1")
    if _state["ready"]:
        print(f"Models ready. Open http://{host}:{port}")
    else:
        print("Bootstrap failed:", _state["error"])
    app.run(host=host, port=port, debug=False)
