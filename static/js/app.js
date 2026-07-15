/* Bangkok Smart Bus — map UI */

const state = {
  data: null,
  hour: 8,
  selectedRoute: null,
  layers: {},
  heatLayer: null,
  map: null,
  dispatchMode: "ai",
  waitHeat: true,
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

function freqForPlan(plan) {
  if (!plan) return 1;
  return state.dispatchMode === "ai"
    ? Math.max(1, plan.frequency_per_hour)
    : Math.max(1, plan.fixed_frequency);
}

/** Average passenger wait (min) ≈ half the headway. 0 trips → very long wait. */
function waitMinutes(freq) {
  if (!freq || freq <= 0) return 45;
  return 60 / freq / 2;
}

/**
 * Green (short wait) → red (long wait).
 * ~1.5 min ≈ best peak service; ~12+ min ≈ sparse.
 */
function waitColor(waitMin) {
  const t = Math.max(0, Math.min(1, (waitMin - 1.5) / (12 - 1.5)));
  let r, g, b;
  if (t < 0.5) {
    const u = t / 0.5;
    r = Math.round(31 + (240 - 31) * u);
    g = Math.round(191 + (162 - 191) * u);
    b = Math.round(154 + (2 - 154) * u);
  } else {
    const u = (t - 0.5) / 0.5;
    r = Math.round(240 + (239 - 240) * u);
    g = Math.round(162 + (71 - 162) * u);
    b = Math.round(2 + (111 - 2) * u);
  }
  return `rgb(${r},${g},${b})`;
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

function renderLegend() {
  const box = $("legend");
  if (!box || !state.data?.routes) return;
  const routePills = state.data.routes
    .map(
      (r) =>
        `<span class="legend-pill"><i style="background:${r.color}"></i>${r.route_id}</span>`
    )
    .join("");
  const waitBlock =
    state.waitHeat && state.selectedRoute
      ? `<div class="wait-legend">
        <span class="wait-legend-title">Wait on ${state.selectedRoute} · ${
          state.dispatchMode === "ai" ? "AI+EV" : "Fixed"
        }</span>
        <div class="wait-scale">
          <span>Low</span>
          <i class="wait-bar"></i>
          <span>Long</span>
        </div>
        <span class="wait-ticks">~2 min    ~15 min</span>
      </div>`
      : `<p class="legend-hint">Click a route for buses + wait heatmap</p>`;
  box.innerHTML = `${routePills}${waitBlock}`;
}

function syncFleetFocus() {
  if (typeof BusSim === "undefined" || !state.map || !state.data) return;
  BusSim.setFocusRoute(state.selectedRoute);
  BusSim.rebuild(state.map, state.data, state.hour);
}

function selectRoute(routeId, { toggle = false } = {}) {
  state.selectedRoute =
    toggle && state.selectedRoute === routeId ? null : routeId;
  renderRouteList();
  drawRoutes();
  syncFleetFocus();
  if (state.selectedRoute && state.layers[state.selectedRoute]) {
    const layer = state.layers[state.selectedRoute];
    state.map.fitBounds(layer.getBounds(), { padding: [40, 40], maxZoom: 13 });
    layer.eachLayer((lyr) => {
      if (lyr.getPopup) lyr.openPopup();
    });
  }
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
      const freq = plan ? freqForPlan(plan) : "—";
      const wait = plan ? waitMinutes(freqForPlan(plan)) : null;
      const active = state.selectedRoute === r.route_id ? "active" : "";
      return `
        <button type="button" class="route-chip ${active}" data-route="${r.route_id}">
          <span class="swatch" style="background:${r.color}"></span>
          <span class="meta">
            <strong>${r.route_id}</strong>
            <small>${r.name}</small>
          </span>
          <span class="freq" ${
            wait != null ? `style="color:${waitColor(wait)}"` : ""
          }>${
            wait != null
              ? `${wait.toFixed(1)}<span>min wait</span>`
              : `${freq}<span>buses/hr</span>`
          }</span>
        </button>`;
    })
    .join("");

  list.querySelectorAll(".route-chip").forEach((btn) => {
    btn.addEventListener("click", () =>
      selectRoute(btn.dataset.route, { toggle: true })
    );
  });
}

function clearRouteLayers() {
  Object.values(state.layers).forEach((layer) => state.map.removeLayer(layer));
  state.layers = {};
  if (state.heatLayer) {
    state.map.removeLayer(state.heatLayer);
    state.heatLayer = null;
  }
}

/** Evenly space stop markers along a route polyline (by distance). */
function stopsAlongPath(latlngs, nStops) {
  if (!latlngs?.length) return [];
  if (latlngs.length === 1) return [latlngs[0]];

  const cum = [0];
  for (let i = 1; i < latlngs.length; i++) {
    const a = L.latLng(latlngs[i - 1]);
    const b = L.latLng(latlngs[i]);
    cum.push(cum[i - 1] + a.distanceTo(b));
  }
  const total = cum[cum.length - 1] || 1;
  // Cap markers for readability; always include terminals
  const count = Math.max(2, Math.min(nStops || 12, 22));
  const out = [];
  for (let s = 0; s < count; s++) {
    const target = (s / (count - 1)) * total;
    let i = 1;
    while (i < cum.length && cum[i] < target) i++;
    const i0 = Math.max(0, i - 1);
    const i1 = Math.min(cum.length - 1, i);
    const seg = cum[i1] - cum[i0] || 1;
    const t = (target - cum[i0]) / seg;
    const a = latlngs[i0];
    const b = latlngs[i1];
    out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
  }
  return out;
}

function drawRoutes() {
  if (!state.data || !state.map) return;
  clearRouteLayers();

  const hour = state.hour;
  const heatGroup = L.layerGroup();

  state.data.routes.forEach((route) => {
    const plan = planFor(route.route_id, hour);
    if (!plan || !route.path?.length) return;

    const dimmed = state.selectedRoute && state.selectedRoute !== route.route_id;
    const freq = freqForPlan(plan);
    const waitAi = waitMinutes(plan.frequency_per_hour);
    const waitFixed = waitMinutes(plan.fixed_frequency);
    const wait = waitMinutes(freq);
    const weight = Math.max(4, Math.min(12, 3 + freq * 0.9));
    const opacity = dimmed ? 0.2 : 0.88;
    const latlngs = route.path.map(([lat, lon]) => [lat, lon]);

    const group = L.featureGroup();

    // Corridor always keeps brand colour so routes stay distinguishable
    const line = L.polyline(latlngs, {
      color: route.color,
      weight,
      opacity,
      lineCap: "round",
      lineJoin: "round",
    });

    const better =
      waitAi < waitFixed - 0.05
        ? `AI saves ~${(waitFixed - waitAi).toFixed(1)} min wait`
        : waitAi > waitFixed + 0.05
          ? `Fixed shorter by ~${(waitAi - waitFixed).toFixed(1)} min`
          : "Similar wait";

    line.bindPopup(`<div class="popup-card">
      <h3 style="color:${route.color}">${route.route_id} · ${hourLabel(hour)}</h3>
      <p><strong>${route.name}</strong></p>
      <p class="wait-row"><span class="wait-chip" style="background:${waitColor(waitAi)}"></span>
        AI wait <strong>${waitAi.toFixed(1)} min</strong> (${plan.frequency_per_hour}/hr)</p>
      <p class="wait-row"><span class="wait-chip" style="background:${waitColor(waitFixed)}"></span>
        Fixed wait <strong>${waitFixed.toFixed(1)} min</strong> (${plan.fixed_frequency}/hr)</p>
      <p>${better}</p>
      <p>Demand: ${fmt(plan.predicted_demand)} · Load ${(plan.load_factor * 100).toFixed(0)}%</p>
    </div>`);

    line.on("click", () => selectRoute(route.route_id));

    group.addLayer(line);

    const focused = state.selectedRoute === route.route_id;

    // Wait heatmap only for the selected route
    if (state.waitHeat && focused) {
      const stopRows =
        plan.stops?.length > 0
          ? plan.stops
          : stopsAlongPath(latlngs, route.n_stops).map((pt) => ({
              lat: pt[0],
              lon: pt[1],
              wait_ai: waitAi,
              wait_fixed: waitFixed,
              traffic_index: plan.traffic_index || 1,
            }));

      stopRows.forEach((stop, idx) => {
        const stopWait =
          state.dispatchMode === "ai"
            ? Number(stop.wait_ai ?? waitAi)
            : Number(stop.wait_fixed ?? waitFixed);
        const color = waitColor(stopWait);
        const intensity = Math.max(0.2, Math.min(1, stopWait / 12));
        const isEnd = idx === 0 || idx === stopRows.length - 1;
        heatGroup.addLayer(
          L.circleMarker([stop.lat, stop.lon], {
            radius: (isEnd ? 14 : 11) + intensity * 6,
            color: color,
            weight: 0,
            fillColor: color,
            fillOpacity: 0.12 + intensity * 0.1,
            opacity: 0,
            interactive: false,
          })
        );
        const marker = L.circleMarker([stop.lat, stop.lon], {
          radius: (isEnd ? 5 : 4) + intensity * 2.5,
          color: color,
          weight: 1,
          fillColor: color,
          fillOpacity: 0.28 + intensity * 0.22,
          opacity: 0.35,
        });
        const tf = stop.traffic_index != null ? Number(stop.traffic_index).toFixed(2) : "—";
        marker.bindTooltip(
          `${route.route_id} · ~${stopWait.toFixed(1)} min wait · traffic ${tf}`,
          { direction: "top", opacity: 0.95 }
        );
        heatGroup.addLayer(marker);
      });
    } else if (!dimmed) {
      // Quiet overview: terminals only until a route is focused
      [latlngs[0], latlngs[latlngs.length - 1]].forEach((pt) => {
        group.addLayer(
          L.circleMarker(pt, {
            radius: focused ? 6 : 4,
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

  if (state.waitHeat && state.selectedRoute && heatGroup.getLayers().length) {
    heatGroup.addTo(state.map);
    state.heatLayer = heatGroup;
  }

  renderLegend();
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
  const demandWait = (key) => {
    const dem = hourPlans.reduce((s, p) => s + p.predicted_demand, 0) || 1;
    return (
      hourPlans.reduce(
        (s, p) => s + waitMinutes(Math.max(p[key] || 0, 0)) * p.predicted_demand,
        0
      ) / dem
    );
  };
  const avgWaitAi = demandWait("frequency_per_hour");
  const avgWaitFixed = demandWait("fixed_frequency");
  const fleetFromFixed = o.fleet_from_fixed ?? o.fleet_size;
  const hourBuses = o.buses_by_hour?.[state.hour] || o.buses_by_hour?.[String(state.hour)];
  const busesNowFixed = hourBuses?.fixed ?? hourPlans.reduce((s, p) => s + (p.fixed_buses || 0), 0);
  const busesNowAi = hourBuses?.ai ?? hourPlans.reduce((s, p) => s + (p.ai_buses || 0), 0);
  const fleetNote = $("fleetVal");
  if (fleetNote) fleetNote.textContent = String(fleetFromFixed);
  const fleetDetail = $("fleetDetail");
  if (fleetDetail) {
    fleetDetail.textContent = `depot pool · ${busesNowFixed} Fixed / ${busesNowAi} AI on road @ ${hourLabel(state.hour)}`;
  }

  box.innerHTML = `
    <div class="stat accent"><span>Demand @ ${hourLabel(state.hour)}</span><strong>${fmt(hourDemand)}</strong></div>
    <div class="stat good"><span>AI avg wait</span><strong style="color:${waitColor(avgWaitAi)}">${avgWaitAi.toFixed(1)} min</strong></div>
    <div class="stat"><span>Fixed avg wait</span><strong style="color:${waitColor(avgWaitFixed)}">${avgWaitFixed.toFixed(1)} min</strong></div>
    <div class="stat" title="Depot size = Fixed’s busiest hour. In-use below changes with the hour slider."><span>Depot fleet</span><strong>${fleetFromFixed}</strong></div>
    <div class="stat" title="Buses on the road at the selected hour (same depot pool)"><span>On road @ ${hourLabel(state.hour)}</span><strong>${busesNowAi} / ${busesNowFixed}</strong></div>
    <div class="stat"><span>Demand model R²</span><strong>${m.demand_r2.toFixed(3)}</strong></div>
    <div class="stat good"><span>Daily cost save</span><strong>฿${fmt(b.savings_per_day.operating_cost_thb)}</strong></div>
    <div class="stat accent"><span>Wait cut (day)</span><strong>${b.savings_per_day.wait_minutes_reduced.toFixed(2)} min</strong></div>
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

  const assumptions = state.data.assumptions || {};
  const assumpSpec = [
    ["electricity_thb_per_kwh", "฿/kWh"],
    ["diesel_thb_per_litre", "฿/L"],
    ["grid_co2_kg_per_kwh", "kgCO₂/kWh"],
    ["bus_capacity", "pax"],
  ];
  const assumpLines = assumpSpec
    .map(([key, unit]) => {
      const x = assumptions[key];
      if (!x) return "";
      return `<li><strong>${x.value} ${unit}</strong> — ${x.label}</li>`;
    })
    .filter(Boolean)
    .join("");

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
    ${
      assumpLines
        ? `<h2 style="margin-top:1rem">Official parameters</h2><ul class="assumptions-list">${assumpLines}</ul>`
        : ""
    }
  `;

  recs.hidden = false;
  recs.innerHTML = `
    <h2>Route tips</h2>
    <ul>${state.data.recommendations.map((r) => `<li>${r.text}</li>`).join("")}</ul>
  `;
}

function setProvenance(ds) {
  const el = $("provenance");
  const badge = $("dataBadge");
  if (!ds) return;

  const isReal = ds.mode === "real";
  if (badge) {
    badge.className = `data-badge ${isReal ? "real" : "synthetic"}`;
    badge.textContent = isReal ? "Real data" : "Synthetic";
    badge.title = isReal
      ? "Using Namtang GTFS, OSM, MOT ridership, and traffic profiles"
      : ds.note || ds.error || "Fell back to synthetic corridors";
  }

  if (!el) return;
  if (isReal && ds.sources) {
    const s = ds.sources;
    const traffic = ds.traffic_provider || "heuristic";
    const month = ds.mot_month ? ` · MOT ${ds.mot_month}` : "";
    el.textContent = `Namtang GTFS · OSM · ${traffic} traffic${month}`;
    el.title = ds.disclaimer || s.ridership || "";
  } else {
    el.textContent = ds.note || "Synthetic fallback (run: python -m data.build_dataset)";
  }
}

async function waitForModels() {
  for (let i = 0; i < 120; i++) {
    const res = await fetch("/api/health");
    const data = await res.json();
    if (data.data_sources) setProvenance(data.data_sources);
    if (data.ready) {
      setStatus("Models ready", "ready");
      return true;
    }
    if (data.error) {
      setStatus(data.error, "error");
      return false;
    }
    setStatus("Loading GTFS / training models…");
    await new Promise((r) => setTimeout(r, 1000));
  }
  setStatus("Timed out waiting for models", "error");
  return false;
}

function refreshMapViews() {
  renderRouteList();
  drawRoutes();
  renderMetrics();
  syncFleetFocus();
}

function wireControls() {
  $("hour").addEventListener("input", () => {
    state.hour = Number($("hour").value);
    $("hourLabel").textContent = hourLabel(state.hour);
    if (state.data) refreshMapViews();
  });

  $("controls").addEventListener("submit", runOptimise);

  $("simPlay")?.addEventListener("click", () => {
    const sim = BusSim.getState();
    BusSim.setPlaying(!sim.playing);
  });

  $("simSpeed")?.addEventListener("input", (e) => {
    BusSim.setSpeed(e.target.value);
  });

  $("waitHeatToggle")?.addEventListener("change", (e) => {
    state.waitHeat = e.target.checked;
    if (state.data) refreshMapViews();
  });

  document.querySelectorAll(".sim-mode").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".sim-mode").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const mode = btn.dataset.mode;
      state.dispatchMode = mode;
      BusSim.setMode(mode);
      $("simModeLabel").textContent = mode === "ai" ? "AI+EV" : "Fixed";
      if (state.data) refreshMapViews();
    });
  });
}

function startFleetSim() {
  if (!state.map || typeof BusSim === "undefined") return;
  BusSim.start(state.map, () =>
    state.data ? { data: state.data, hour: state.hour } : null
  );
}

async function runOptimise(e) {
  if (e) e.preventDefault();
  setLoading(true);
  setStatus("Optimising…");

  const day = $("day").value;
  const rain = $("rain").checked ? 1 : 0;
  const url = `/api/optimise?day_of_week=${day}&rain=${rain}`;

  try {
    const res = await fetch(url);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    state.data = await res.json();
    if (state.data.data_sources) setProvenance(state.data.data_sources);
    state.hour = Number($("hour").value);
    refreshMapViews();
    renderImpact();
    startFleetSim();

    const bounds = L.latLngBounds([]);
    state.data.routes.forEach((r) => {
      (r.path || []).forEach(([lat, lon]) => bounds.extend([lat, lon]));
    });
    if (bounds.isValid()) {
      state.map.fitBounds(bounds, { padding: [30, 30] });
    }
    setStatus(
      `Live fleet · wait heatmap · ${state.data.optimisation.total_vehicle_trips} trips/day`,
      "ready"
    );
  } catch (err) {
    setStatus(err.message || "Optimise failed", "error");
  } finally {
    setLoading(false);
  }
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
