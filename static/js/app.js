/* Bangkok Smart Bus — map UI */

const state = {
  data: null,
  hour: 8,
  selectedRoute: null,
  layers: {},
  map: null,
};

const $ = (id) => document.getElementById(id);

function fmt(n, digits = 0) {
  if (n == null || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString("en-US", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function hourLabel(h) {
  return `${String(h).padStart(2, "0")}:00`;
}

function planFor(routeId, hour) {
  if (!state.data) return null;
  return state.data.plans.find((p) => p.route_id === routeId && p.hour === hour);
}

function initMap() {
  const map = L.map("map", {
    zoomControl: false,
    attributionControl: true,
  }).setView([13.76, 100.54], 12);

  L.control.zoom({ position: "topright" }).addTo(map);

  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
    subdomains: "abcd",
    maxZoom: 19,
  }).addTo(map);

  state.map = map;
}

function setStatus(text, kind = "") {
  const el = $("status");
  el.textContent = text;
  el.className = `status ${kind}`.trim();
}

function setLoading(on) {
  $("loading").hidden = !on;
  $("runBtn").disabled = on;
}

function renderLegend(routes) {
  $("legend").innerHTML = routes
    .map(
      (r) =>
        `<span class="legend-pill"><i style="background:${r.color}"></i>${r.route_id}</span>`
    )
    .join("");
}

function renderRouteList() {
  const list = $("routeList");
  if (!state.data) {
    list.innerHTML = "";
    return;
  }

  list.innerHTML = state.data.routes
    .map((r) => {
      const plan = planFor(r.route_id, state.hour);
      const freq = plan ? plan.frequency_per_hour : "—";
      const active = state.selectedRoute === r.route_id ? "active" : "";
      return `
        <button type="button" class="route-chip ${active}" data-route="${r.route_id}">
          <span class="swatch" style="background:${r.color}"></span>
          <span class="meta">
            <strong>${r.route_id}</strong>
            <small>${r.name}</small>
          </span>
          <span class="freq">${freq}<span>buses/hr</span></span>
        </button>`;
    })
    .join("");

  list.querySelectorAll(".route-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.route;
      state.selectedRoute = state.selectedRoute === id ? null : id;
      renderRouteList();
      drawRoutes();
      const layer = state.layers[id];
      if (layer && state.selectedRoute === id) {
        state.map.fitBounds(layer.getBounds(), { padding: [40, 40], maxZoom: 13 });
        layer.eachLayer((lyr) => {
          if (lyr.getPopup) lyr.openPopup();
        });
      }
    });
  });
}

function clearRouteLayers() {
  Object.values(state.layers).forEach((layer) => state.map.removeLayer(layer));
  state.layers = {};
}

function drawRoutes() {
  if (!state.data || !state.map) return;
  clearRouteLayers();

  const hour = state.hour;
  state.data.routes.forEach((route) => {
    const plan = planFor(route.route_id, hour);
    if (!plan || !route.path?.length) return;

    const dimmed = state.selectedRoute && state.selectedRoute !== route.route_id;
    const weight = Math.max(4, Math.min(14, 3 + plan.frequency_per_hour * 1.1));
    const opacity = dimmed ? 0.22 : 0.9;
    const latlngs = route.path.map(([lat, lon]) => [lat, lon]);

    const group = L.featureGroup();
    const line = L.polyline(latlngs, {
      color: route.color,
      weight,
      opacity,
      lineCap: "round",
      lineJoin: "round",
    });

    const wait = (60 / Math.max(plan.frequency_per_hour, 1) / 2).toFixed(1);
    line.bindPopup(`<div class="popup-card">
      <h3 style="color:${route.color}">${route.route_id} · ${hourLabel(hour)}</h3>
      <p><strong>${route.name}</strong></p>
      <p>Demand: ${fmt(plan.predicted_demand)} riders</p>
      <p>AI+EV: <strong>${plan.frequency_per_hour}</strong>/hr vs fixed ${plan.fixed_frequency}</p>
      <p>Travel ${plan.travel_min} min · Load ${(plan.load_factor * 100).toFixed(0)}%</p>
      <p>Avg wait ≈ ${wait} min</p>
    </div>`);

    line.on("click", () => {
      state.selectedRoute = route.route_id;
      renderRouteList();
      drawRoutes();
    });

    group.addLayer(line);

    if (!dimmed) {
      [latlngs[0], latlngs[latlngs.length - 1]].forEach((pt) => {
        group.addLayer(
          L.circleMarker(pt, {
            radius: 5,
            color: "#f5f7fa",
            weight: 1.5,
            fillColor: route.color,
            fillOpacity: 1,
            opacity: 1,
          })
        );
      });
    }

    group.addTo(state.map);
    state.layers[route.route_id] = group;
  });
}

function renderMetrics() {
  const box = $("metrics");
  if (!state.data) {
    box.innerHTML = `<p class="muted">Run the optimiser to see predictions on the map.</p>`;
    return;
  }

  const o = state.data.optimisation;
  const b = state.data.business;
  const m = state.data.models;
  const hourPlans = state.data.plans.filter((p) => p.hour === state.hour);
  const hourDemand = hourPlans.reduce((s, p) => s + p.predicted_demand, 0);
  const hourTrips = hourPlans.reduce((s, p) => s + p.frequency_per_hour, 0);

  box.innerHTML = `
    <div class="stat accent"><span>Demand @ ${hourLabel(state.hour)}</span><strong>${fmt(hourDemand)}</strong></div>
    <div class="stat good"><span>Trips this hour</span><strong>${fmt(hourTrips)}</strong></div>
    <div class="stat"><span>Energy used</span><strong>${fmt(o.energy_used_kwh, 0)} kWh</strong></div>
    <div class="stat"><span>Peak buses</span><strong>${o.peak_buses_in_use} / ${o.fleet_size}</strong></div>
    <div class="stat"><span>Demand model R²</span><strong>${m.demand_r2.toFixed(3)}</strong></div>
    <div class="stat"><span>Travel model R²</span><strong>${m.travel_r2.toFixed(3)}</strong></div>
    <div class="stat good"><span>Daily cost save</span><strong>฿${fmt(b.savings_per_day.operating_cost_thb)}</strong></div>
    <div class="stat accent"><span>Wait cut</span><strong>${b.savings_per_day.wait_minutes_reduced.toFixed(2)} min</strong></div>
  `;
}

function renderImpact() {
  const el = $("impact");
  const recs = $("recs");
  if (!state.data) {
    el.hidden = true;
    recs.hidden = true;
    return;
  }

  const { baseline: b, optimised: o, savings_per_day: s } = state.data.business;
  const a = state.data.annual;

  el.hidden = false;
  el.innerHTML = `
    <h2>One weekday impact</h2>
    <div class="compare">
      <div class="compare-row head"><span>Metric</span><span>Baseline</span><span>AI+EV</span></div>
      <div class="compare-row"><span>Op. cost</span><span>฿${fmt(b.operating_cost_thb)}</span><span>฿${fmt(o.operating_cost_thb)}</span></div>
      <div class="compare-row"><span>Riders</span><span>${fmt(b.served_passengers)}</span><span>${fmt(o.served_passengers)}</span></div>
      <div class="compare-row"><span>Avg wait</span><span>${b.avg_wait_min} min</span><span>${o.avg_wait_min} min</span></div>
      <div class="compare-row"><span>CO₂</span><span>${fmt(b.co2_kg)} kg</span><span>${fmt(o.co2_kg)} kg</span></div>
      <div class="compare-row"><span>Revenue</span><span>฿${fmt(b.revenue_thb)}</span><span>฿${fmt(o.revenue_thb)}</span></div>
      <div class="compare-row"><span>Save / day</span><span></span><span class="delta">฿${fmt(s.operating_cost_thb)}</span></div>
      <div class="compare-row"><span>~250 days</span><span></span><span class="delta">฿${fmt(a.combined_thb)}</span></div>
    </div>
  `;

  recs.hidden = false;
  recs.innerHTML = `
    <h2>Route tips</h2>
    <ul>${state.data.recommendations.map((r) => `<li>${r.text}</li>`).join("")}</ul>
  `;
}

async function waitForModels() {
  for (let i = 0; i < 60; i++) {
    const res = await fetch("/api/health");
    const data = await res.json();
    if (data.ready) {
      setStatus("Models ready", "ready");
      return true;
    }
    if (data.error) {
      setStatus(data.error, "error");
      return false;
    }
    setStatus("Training models…");
    await new Promise((r) => setTimeout(r, 800));
  }
  setStatus("Timed out waiting for models", "error");
  return false;
}

async function runOptimise(e) {
  if (e) e.preventDefault();
  setLoading(true);
  setStatus("Optimising…");

  const day = $("day").value;
  const rain = $("rain").checked ? 1 : 0;
  const fleet = $("fleet").value;
  const url = `/api/optimise?day_of_week=${day}&rain=${rain}&fleet_size=${fleet}`;

  try {
    const res = await fetch(url);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    state.data = await res.json();
    state.hour = Number($("hour").value);
    renderLegend(state.data.routes);
    renderRouteList();
    drawRoutes();
    renderMetrics();
    renderImpact();

    const bounds = L.latLngBounds([]);
    state.data.routes.forEach((r) => {
      r.path.forEach(([lat, lon]) => bounds.extend([lat, lon]));
    });
    if (bounds.isValid()) {
      state.map.fitBounds(bounds, { padding: [30, 30] });
    }
    setStatus(
      `Optimised · ${state.data.optimisation.total_vehicle_trips} trips`,
      "ready"
    );
  } catch (err) {
    setStatus(err.message || "Optimise failed", "error");
  } finally {
    setLoading(false);
  }
}

function wireControls() {
  $("fleet").addEventListener("input", () => {
    $("fleetVal").textContent = $("fleet").value;
  });

  $("hour").addEventListener("input", () => {
    state.hour = Number($("hour").value);
    $("hourLabel").textContent = hourLabel(state.hour);
    if (state.data) {
      renderRouteList();
      drawRoutes();
      renderMetrics();
    }
  });

  $("controls").addEventListener("submit", runOptimise);
}

async function boot() {
  initMap();
  wireControls();
  $("hourLabel").textContent = hourLabel(state.hour);

  const ok = await waitForModels();
  if (ok) {
    await runOptimise();
  }
}

boot();
