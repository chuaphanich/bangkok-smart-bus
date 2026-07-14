"""
Bangkok Smart Bus — web dashboard
Run:  .venv_bangkok/bin/python app.py
Then open http://127.0.0.1:5050
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from flask import Flask, jsonify, render_template, request

import bangkok_bus as bb

app = Flask(__name__)

# Approximate BMTA-style corridor polylines (lat, lon) for map display
ROUTE_GEOMETRY: dict[str, dict[str, Any]] = {
    "R8": {
        "name": "Victory Monument → Bang Sue",
        "color": "#E85D04",
        "path": [
            [13.7649, 100.5383],
            [13.7705, 100.5401],
            [13.7790, 100.5450],
            [13.7865, 100.5488],
            [13.7940, 100.5520],
            [13.8025, 100.5545],
            [13.8120, 100.5568],
            [13.8205, 100.5530],
            [13.8280, 100.5475],
            [13.8355, 100.5410],
        ],
    },
    "R29": {
        "name": "Siam → Lat Phrao",
        "color": "#2A9D8F",
        "path": [
            [13.7460, 100.5340],
            [13.7495, 100.5395],
            [13.7545, 100.5445],
            [13.7620, 100.5510],
            [13.7700, 100.5565],
            [13.7785, 100.5620],
            [13.7880, 100.5685],
            [13.7965, 100.5740],
            [13.8050, 100.5805],
            [13.8125, 100.5860],
        ],
    },
    "R39": {
        "name": "Silom → Ratchadapisek",
        "color": "#4C6EF5",
        "path": [
            [13.7240, 100.5280],
            [13.7285, 100.5325],
            [13.7350, 100.5380],
            [13.7420, 100.5435],
            [13.7490, 100.5490],
            [13.7565, 100.5555],
            [13.7650, 100.5620],
            [13.7740, 100.5685],
            [13.7830, 100.5750],
            [13.7915, 100.5810],
            [13.8000, 100.5865],
        ],
    },
    "R509": {
        "name": "Tha Phra → MBK",
        "color": "#D62828",
        "path": [
            [13.7305, 100.4700],
            [13.7320, 100.4805],
            [13.7340, 100.4910],
            [13.7365, 100.5010],
            [13.7395, 100.5105],
            [13.7425, 100.5195],
            [13.7450, 100.5265],
            [13.7465, 100.5325],
        ],
    },
    "R554": {
        "name": "Din Daeng → Min Buri",
        "color": "#9B5DE5",
        "path": [
            [13.7690, 100.5530],
            [13.7725, 100.5650],
            [13.7760, 100.5780],
            [13.7800, 100.5920],
            [13.7850, 100.6080],
            [13.7905, 100.6250],
            [13.7960, 100.6420],
            [13.8020, 100.6580],
            [13.8085, 100.6750],
            [13.8135, 100.6900],
        ],
    },
}

_state: dict[str, Any] = {
    "models": None,
    "ready": False,
    "error": None,
}


def _bootstrap() -> None:
    try:
        hist = bb.generate_historical_data(60)
        models = bb.train_models(hist)
        _state["models"] = models
        _state["ready"] = True
    except Exception as exc:  # noqa: BLE001 — surface to UI
        _state["error"] = str(exc)
        _state["ready"] = False


def _route_meta() -> list[dict[str, Any]]:
    out = []
    for route_id, length_km, stops, base_dem, fare, diesel in bb.ROUTES:
        geo = ROUTE_GEOMETRY.get(route_id, {})
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
    fleet_size: int = 110,
) -> dict[str, Any]:
    models = _state["models"]
    if models is None:
        raise RuntimeError("Models not ready")

    forecast = bb.predict_horizon(models, day_of_week=day_of_week, rain=rain)
    plans, opt_sum = bb.optimise_frequencies(forecast, fleet_size=fleet_size)
    biz = bb.business_comparison(forecast, plans)

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

    hourly = []
    for p in plans:
        hourly.append(
            {
                **asdict(p),
                "fixed_frequency": bb.baseline_frequency(p.hour),
                "is_peak": bb.is_peak_hour(p.hour),
            }
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
        "routes": _route_meta(),
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
        },
        "ev": asdict(bb.EV),
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
        }
    )


@app.route("/api/optimise")
def optimise():
    if not _state["ready"]:
        return jsonify({"error": _state["error"] or "Models still loading"}), 503

    day = int(request.args.get("day_of_week", 1))
    rain = float(request.args.get("rain", 0.0))
    fleet = int(request.args.get("fleet_size", 110))
    day = max(0, min(6, day))
    rain = 1.0 if rain >= 0.5 else 0.0
    fleet = max(40, min(200, fleet))

    try:
        payload = _run_optimisation(day_of_week=day, rain=rain, fleet_size=fleet)
        return jsonify(payload)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    print("Training demand & travel models (first run)…")
    _bootstrap()
    if _state["ready"]:
        print("Models ready. Open http://127.0.0.1:5050")
    else:
        print("Bootstrap failed:", _state["error"])
    app.run(host="127.0.0.1", port=5050, debug=False)
