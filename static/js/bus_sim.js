/**
 * Control-centre bus fleet animation along GTFS/OSM polylines.
 *
 * Concurrent buses ≈ frequency (buses/hr) × one-way trip hours
 * (e.g. 6/hr and 40 min trip → ~4 buses on the corridor).
 * Playback speed is independent (Speed scrubber).
 */

const BusSim = (() => {
  const SIM = {
    playing: true,
    speed: 36,
    mode: "ai",
    buses: [],
    lastTs: 0,
    raf: 0,
    simMinutes: 0,
    nextId: 1,
    departAccum: {},
    targetByRoute: {},
    layer: null,
    bunchedPairs: 0,
    departures: 0,
    redeploys: 0,
    hour: 8,
    /** When set, only this route gets buses / depots (null = none). */
    focusRoute: null,
  };

  function routesToSim(data) {
    if (!data?.routes) return [];
    if (!SIM.focusRoute) return [];
    return data.routes.filter((r) => r.route_id === SIM.focusRoute);
  }

  function haversineM(a, b) {
    const R = 6371000;
    const toR = Math.PI / 180;
    const dLat = (b[0] - a[0]) * toR;
    const dLon = (b[1] - a[1]) * toR;
    const lat1 = a[0] * toR;
    const lat2 = b[0] * toR;
    const h =
      Math.sin(dLat / 2) ** 2 +
      Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  function buildTrack(path) {
    const pts = path.map(([lat, lon]) => [lat, lon]);
    const cum = [0];
    let total = 0;
    for (let i = 1; i < pts.length; i++) {
      total += haversineM(pts[i - 1], pts[i]);
      cum.push(total);
    }
    return { pts, cum, total: total || 1 };
  }

  function pointAt(track, dist) {
    const d = Math.max(0, Math.min(track.total, dist));
    const cum = track.cum;
    let i = 1;
    while (i < cum.length && cum[i] < d) i++;
    const i0 = Math.max(0, i - 1);
    const i1 = Math.min(cum.length - 1, i);
    const seg = cum[i1] - cum[i0] || 1;
    const t = (d - cum[i0]) / seg;
    const a = track.pts[i0];
    const b = track.pts[i1];
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  }

  function planFor(data, routeId, hour) {
    return data.plans.find((p) => p.route_id === routeId && p.hour === hour);
  }

  function freqFor(plan) {
    if (!plan) return 2;
    return SIM.mode === "ai"
      ? Math.max(1, plan.frequency_per_hour)
      : Math.max(1, plan.fixed_frequency);
  }

  /**
   * Buses simultaneously on the corridor (one-way):
   * vehicles ≈ departures_per_hour × trip_duration_hours
   */
  function fleetTarget(plan) {
    const freq = freqFor(plan);
    const tripHours = Math.max(8, plan.travel_min) / 60;
    return Math.max(1, Math.round(freq * tripHours));
  }

  function headwayMin(plan) {
    return 60 / freqFor(plan);
  }

  function countOnRoute(routeId) {
    return SIM.buses.filter((b) => b.routeId === routeId).length;
  }

  function busIcon(color, bunched) {
    return L.divIcon({
      className: `bus-marker${bunched ? " bunched" : ""}`,
      html: `<span class="bus-dot" style="--bus:${color}"></span>`,
      iconSize: [14, 14],
      iconAnchor: [7, 7],
    });
  }

  function depotIcon(color) {
    return L.divIcon({
      className: "depot-marker",
      html: `<span class="depot-ring" style="--bus:${color}"></span>`,
      iconSize: [22, 22],
      iconAnchor: [11, 11],
    });
  }

  function clearLayer(map) {
    if (SIM.layer) {
      map.removeLayer(SIM.layer);
      SIM.layer = null;
    }
  }

  function spawnBus(route, track, plan, progress01) {
    const travel = Math.max(12, plan.travel_min);
    const tripMin = travel * (0.97 + Math.random() * 0.06);
    const dist = Math.min(track.total * 0.998, progress01 * track.total);
    const bus = {
      id: SIM.nextId++,
      routeId: route.route_id,
      color: route.color,
      track,
      dist,
      speedMPerMin: track.total / tripMin,
      dwellLeft: 0,
      marker: null,
      bunched: false,
    };
    bus.marker = L.marker(pointAt(track, bus.dist), {
      icon: busIcon(route.color, false),
      interactive: true,
      keyboard: false,
      zIndexOffset: 600,
    });
    bus.marker.bindTooltip(`${route.route_id} · #${bus.id}`, {
      direction: "top",
      opacity: 0.9,
    });
    SIM.layer.addLayer(bus.marker);
    SIM.buses.push(bus);
    return bus;
  }

  function pulseDepot(map, latlng, color) {
    const pulse = L.circleMarker(latlng, {
      radius: 8,
      color,
      weight: 2,
      fillColor: color,
      fillOpacity: 0.35,
      opacity: 0.9,
    }).addTo(map);
    const t0 = performance.now();
    function grow(now) {
      const age = now - t0;
      pulse.setRadius(8 + age / 40);
      pulse.setStyle({
        opacity: Math.max(0, 1 - age / 700),
        fillOpacity: Math.max(0, 0.4 - age / 1800),
      });
      if (age < 700) requestAnimationFrame(grow);
      else map.removeLayer(pulse);
    }
    requestAnimationFrame(grow);
  }

  function reset(map, data, hour) {
    clearLayer(map);
    SIM.layer = L.layerGroup().addTo(map);
    SIM.buses = [];
    SIM.departAccum = {};
    SIM.targetByRoute = {};
    SIM.hour = hour;
    SIM.simMinutes = hour * 60;
    SIM.departures = 0;
    SIM.redeploys = 0;
    SIM.bunchedPairs = 0;
    SIM.lastTs = 0;

    if (!data?.routes) {
      updateHud();
      return;
    }

    routesToSim(data).forEach((route) => {
      const path = route.path;
      if (!path || path.length < 2) return;
      const plan = planFor(data, route.route_id, hour);
      if (!plan) return;

      const track = buildTrack(path);
      const target = fleetTarget(plan);
      const headway = headwayMin(plan);
      SIM.targetByRoute[route.route_id] = target;

      const dA = L.marker(path[0], {
        icon: depotIcon(route.color),
        zIndexOffset: 400,
      });
      dA.bindTooltip(
        `Depot · ${route.route_id} · ~${target} on road (${freqFor(plan)} buses/hr × ${plan.travel_min.toFixed(0)} min trip)`,
        { permanent: false }
      );
      SIM.layer.addLayer(dA);
      SIM.layer.addLayer(
        L.circleMarker(path[path.length - 1], {
          radius: 4,
          color: "#f5f7fa",
          weight: 1,
          fillColor: route.color,
          fillOpacity: 0.7,
        })
      );

      for (let i = 0; i < target; i++) {
        spawnBus(route, track, plan, (i + 0.5) / target);
      }

      SIM.departAccum[route.route_id] = headway * 0.85;
      route._track = track;
    });

    updateHud();
  }

  function markBunching() {
    SIM.bunchedPairs = 0;
    const byRoute = {};
    SIM.buses.forEach((b) => {
      (byRoute[b.routeId] || (byRoute[b.routeId] = [])).push(b);
    });
    Object.values(byRoute).forEach((list) => {
      list.forEach((b) => {
        b.bunched = false;
      });
      const idealGap = list[0] ? list[0].track.total / Math.max(list.length, 1) : 0;
      const thresh = idealGap * 0.35;
      for (let i = 0; i < list.length; i++) {
        for (let j = i + 1; j < list.length; j++) {
          if (Math.abs(list[i].dist - list[j].dist) < thresh) {
            list[i].bunched = true;
            list[j].bunched = true;
            SIM.bunchedPairs += 1;
          }
        }
      }
      list.forEach((b) => {
        if (b.marker) b.marker.setIcon(busIcon(b.color, b.bunched));
      });
    });
  }

  function step(map, data, hour, dtSec) {
    if (!data || !SIM.playing) return;
    if (hour !== SIM.hour) {
      reset(map, data, hour);
      return;
    }

    const activeRoutes = routesToSim(data);
    if (!activeRoutes.length) {
      updateHud();
      return;
    }

    let minHeadway = 30;
    activeRoutes.forEach((route) => {
      const plan = planFor(data, route.route_id, hour);
      if (plan) minHeadway = Math.min(minHeadway, headwayMin(plan));
    });
    const dSimMin = Math.min(dtSec * SIM.speed, minHeadway * 0.9);

    activeRoutes.forEach((route) => {
      const plan = planFor(data, route.route_id, hour);
      if (!plan || !route._track) return;
      const target = fleetTarget(plan);
      SIM.targetByRoute[route.route_id] = target;
      const headway = headwayMin(plan);
      const key = route.route_id;

      SIM.departAccum[key] = (SIM.departAccum[key] ?? headway) - dSimMin;
      while (SIM.departAccum[key] <= 0) {
        SIM.departAccum[key] += headway;
        if (countOnRoute(route.route_id) >= target) continue;
        const bus = spawnBus(route, route._track, plan, 0);
        SIM.departures += 1;
        pulseDepot(map, route.path[0], route.color);
        if (
          SIM.mode === "ai" &&
          plan.frequency_per_hour > plan.fixed_frequency &&
          Math.random() < 0.2
        ) {
          SIM.redeploys += 1;
          bus.marker?.getElement()?.classList.add("redeploy");
        }
      }
    });

    const survivors = [];
    SIM.buses.forEach((bus) => {
      if (bus.dwellLeft > 0) {
        bus.dwellLeft -= dSimMin;
        survivors.push(bus);
        return;
      }
      if (Math.random() < 0.008 * dSimMin) {
        bus.dwellLeft = 0.2 + Math.random() * 0.6;
      }
      bus.dist += bus.speedMPerMin * dSimMin;
      if (bus.dist >= bus.track.total) {
        SIM.layer.removeLayer(bus.marker);
        return;
      }
      bus.marker.setLatLng(pointAt(bus.track, bus.dist));
      survivors.push(bus);
    });
    SIM.buses = survivors;
    SIM.simMinutes += dSimMin;
    markBunching();
    updateHud();
  }

  function updateHud() {
    const el = document.getElementById("simHud");
    if (!el) return;
    if (!SIM.focusRoute) {
      el.innerHTML = `
        <div><span>Live fleet</span><strong>Select a route</strong></div>
        <div><span>Hour</span><strong>${String(SIM.hour).padStart(2, "0")}:00</strong></div>
        <div><span>Dispatch</span><strong>${SIM.mode === "ai" ? "AI+EV" : "Fixed"}</strong></div>
      `;
      return;
    }
    const active = SIM.buses.length;
    const scheduled = Object.values(SIM.targetByRoute).reduce((a, b) => a + b, 0);
    const perRoute = Object.entries(SIM.targetByRoute)
      .map(([id, n]) => `${id}:${countOnRoute(id)}/${n}`)
      .join(" · ");
    el.innerHTML = `
      <div><span>On road / expected</span><strong>${active} / ${scheduled}</strong></div>
      <div><span>Route</span><strong>${SIM.focusRoute}</strong></div>
      <div><span>Hour</span><strong>${String(SIM.hour).padStart(2, "0")}:00</strong></div>
      <div class="${SIM.bunchedPairs ? "warn" : ""}"><span>Bunching</span><strong>${SIM.bunchedPairs}</strong></div>
      <div><span>Dispatch</span><strong>${SIM.mode === "ai" ? "AI+EV" : "Fixed"}</strong></div>
      <div class="sim-hud-routes"><span>Per route (on/expected)</span><strong title="${perRoute}">${perRoute || "—"}</strong></div>
    `;
  }

  function setFocusRoute(routeId) {
    SIM.focusRoute = routeId || null;
  }

  function tick(map, getContext) {
    const now = performance.now();
    if (!SIM.lastTs) SIM.lastTs = now;
    const dt = Math.min(0.08, (now - SIM.lastTs) / 1000);
    SIM.lastTs = now;
    const ctx = getContext();
    if (ctx) step(map, ctx.data, ctx.hour, dt);
    SIM.raf = requestAnimationFrame(() => tick(map, getContext));
  }

  function start(map, getContext) {
    stop();
    const ctx = getContext();
    if (ctx) reset(map, ctx.data, ctx.hour);
    SIM.playing = true;
    SIM.lastTs = 0;
    SIM.raf = requestAnimationFrame(() => tick(map, getContext));
  }

  function stop() {
    if (SIM.raf) cancelAnimationFrame(SIM.raf);
    SIM.raf = 0;
  }

  function setPlaying(on) {
    SIM.playing = on;
    SIM.lastTs = 0;
    const btn = document.getElementById("simPlay");
    if (btn) btn.textContent = on ? "Pause" : "Play";
  }

  function setSpeed(v) {
    SIM.speed = Number(v);
  }

  function setMode(mode) {
    SIM.mode = mode === "fixed" ? "fixed" : "ai";
    updateHud();
  }

  function rebuild(map, data, hour) {
    reset(map, data, hour);
  }

  function getMode() {
    return SIM.mode;
  }

  return {
    start,
    stop,
    setPlaying,
    setSpeed,
    setMode,
    setFocusRoute,
    getMode,
    rebuild,
    getState: () => SIM,
  };
})();
