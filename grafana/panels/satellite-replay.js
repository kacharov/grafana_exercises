/*
 * Satellite replay — code for the "Business Charts" (Apache ECharts) panel.
 *
 * This file is the readable source. Grafana runs the copy embedded in
 * grafana/dashboards/satellites.json (panel option "getOption").
 * After editing this file run:   python3 grafana/embed_panel_code.py
 *
 * Data: query A -> /animation?group=..&ids=..&from=${__from}&to=${__to}
 *   one row per satellite per time step: norad_id, name, t (epoch ms), lat, lon, alt_km
 *
 * Behaviour:
 *   - Plays the satellites' motion from the start to the end of the dashboard
 *     time range in $anim_seconds seconds, then stops on the end time.
 *   - Positions between time steps are interpolated along a great circle.
 *   - The night side of the Earth moves with the animated clock.
 *   - When the dashboard auto-refreshes with the same selection and the same
 *     range length (e.g. "Last 1 hour"), it does NOT replay: it jumps straight
 *     to the new end positions, so the map stays live. Use the ▶ button
 *     (top right) to replay.
 *
 * Grafana's Business Charts panel calls this code as a function body with one
 * argument, `context` (context.panel.data, context.panel.chart, ...).
 */

const chart = context.panel.chart;
const dark = context.grafana.theme.isDark;
const rv = context.grafana.replaceVariables;

// ---- settings from dashboard variables ------------------------------------
const ANIM_MS = (parseFloat(rv('$anim_seconds')) || 10) * 1000;
const LABEL_LIMIT = parseInt(rv('$label_limit'), 10) || 0;
const TRAIL_LIMIT = 25;      // draw the path travelled so far when <= this many satellites
const FRAME_MS = 40;         // ~25 frames per second

const COLORS = {
  land: dark ? '#2a3442' : '#dfe5ec',
  border: dark ? '#46546a' : '#aab5c2',
  sat: '#F2495C',
  trail: '#8AB8FF',
  night: dark ? 'rgba(0, 0, 0, 0.38)' : 'rgba(20, 30, 60, 0.22)',
  text: dark ? '#d8dee9' : '#24292e',
};

const rad = Math.PI / 180;
const deg = 180 / Math.PI;

// ---- read the data frame returned by the Infinity query ---------------------
const data = context.panel.data;
const frame = data.series && data.series[0];
const column = (name) => {
  const f = frame && frame.fields.find((x) => x.name === name);
  return f ? Array.from(f.values) : [];
};
const colId = column('norad_id');
const colName = column('name');
const colT = column('t');
const colLat = column('lat');
const colLon = column('lon');
const colAlt = column('alt_km');

// Group rows by satellite. All satellites share the same time steps.
const byId = new Map();
for (let i = 0; i < colId.length; i++) {
  let s = byId.get(colId[i]);
  if (!s) {
    s = { id: colId[i], name: '', lat: [], lon: [], alt: [] };
    byId.set(colId[i], s);
  }
  if (colName[i]) s.name = colName[i];
  s.lat.push(colLat[i]);
  s.lon.push(colLon[i]);
  s.alt.push(colAlt[i]);
}
const sats = Array.from(byId.values());
const times = sats.length ? colT.slice(0, sats[0].lat.length) : [];
const T0 = times[0];
const T1 = times[times.length - 1];

// ---- geometry helpers ----------------------------------------------------------
const toVec = (lat, lon) => [Math.cos(lat * rad) * Math.cos(lon * rad), Math.cos(lat * rad) * Math.sin(lon * rad), Math.sin(lat * rad)];

// Great-circle interpolation between two sub-satellite points (fraction f in 0..1).
function slerp(lat1, lon1, lat2, lon2, f) {
  const a = toVec(lat1, lon1);
  const b = toVec(lat2, lon2);
  const dot = Math.min(1, Math.max(-1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]));
  const om = Math.acos(dot);
  let v;
  if (om < 1e-6) {
    v = a;
  } else {
    const s1 = Math.sin((1 - f) * om) / Math.sin(om);
    const s2 = Math.sin(f * om) / Math.sin(om);
    v = [s1 * a[0] + s2 * b[0], s1 * a[1] + s2 * b[1], s1 * a[2] + s2 * b[2]];
  }
  return [Math.atan2(v[1], v[0]) * deg, Math.atan2(v[2], Math.hypot(v[0], v[1])) * deg];
}

// Sub-solar point (accuracy ~0.5 deg, plenty for shading the night side).
function subSolar(tMs) {
  const d = tMs / 86400000 - 10957.5; // days since J2000.0
  const g = (357.529 + 0.98560028 * d) * rad;
  const q = 280.459 + 0.98564736 * d;
  const L = (q + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * rad;
  const e = (23.439 - 0.00000036 * d) * rad;
  const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L)) * deg;
  const dec = Math.asin(Math.sin(e) * Math.sin(L)) * deg;
  const gmstDeg = (((280.46061837 + 360.98564736629 * d) % 360) + 360) % 360;
  let lon = ra - gmstDeg;
  lon = ((((lon + 180) % 360) + 360) % 360) - 180;
  return { lat: dec, lon };
}

// Polygon covering the night side: terminator line closed over the dark pole.
function nightPolygon(tMs) {
  const sun = subSolar(tMs);
  const dec = Math.abs(sun.lat) < 0.1 ? (sun.lat < 0 ? -0.1 : 0.1) : sun.lat;
  const pts = [];
  for (let lon = -180; lon <= 180; lon += 2) {
    const lat = Math.atan(-Math.cos((lon - sun.lon) * rad) / Math.tan(dec * rad)) * deg;
    pts.push([lon, lat]);
  }
  const darkPole = dec > 0 ? -90 : 90; // northern summer -> south pole is dark
  pts.push([180, darkPole], [-180, darkPole]);
  return pts;
}

// Position of every satellite at time tMs.
function positionsAt(tMs) {
  if (times.length < 2) {
    return sats.map((s, i) => [s.lon[0], s.lat[0], s.alt[0], i]);
  }
  const x = ((tMs - T0) / (T1 - T0)) * (times.length - 1);
  const k = Math.min(times.length - 2, Math.max(0, Math.floor(x)));
  const f = Math.min(1, Math.max(0, x - k));
  return sats.map((s, i) => {
    const [lon, lat] = slerp(s.lat[k], s.lon[k], s.lat[k + 1], s.lon[k + 1], f);
    return [lon, lat, Math.round(s.alt[k] + (s.alt[k + 1] - s.alt[k]) * f), i];
  });
}

// Path travelled so far (time steps before tMs plus the current point).
function trailsAt(tMs, current) {
  if (sats.length > TRAIL_LIMIT) return [];
  const out = [];
  sats.forEach((s, i) => {
    for (let k = 0; k < times.length && times[k] <= tMs; k++) out.push([s.lon[k], s.lat[k], s.alt[k], i]);
    out.push(current[i]);
  });
  return out;
}

const fmtTime = (tMs) => new Date(tMs).toISOString().replace('T', ' ').slice(0, 19) + ' UTC';

// ---- draw one moment ------------------------------------------------------------
function render(tMs, playing) {
  const current = positionsAt(tMs);
  chart.setOption({
    series: [
      { id: 'night', data: [[tMs]] },
      { id: 'trails', data: trailsAt(tMs, current) },
      { id: 'sats', data: current },
    ],
    graphic: [{
      id: 'clock',
      style: { text: (playing ? '▶  ' : '■  ') + fmtTime(tMs) + '   ·   ' + sats.length + ' satellites' },
    }],
  });
}

// ---- animation loop -------------------------------------------------------------
let timer = null;
const stop = () => { if (timer) { clearTimeout(timer); timer = null; } };

function play() {
  stop();
  const started = Date.now();
  const step = () => {
    const f = Math.min(1, (Date.now() - started) / ANIM_MS);
    render(T0 + f * (T1 - T0), f < 1);
    if (f < 1) {
      timer = setTimeout(step, FRAME_MS);
    } else {
      timer = null;
      chart.__satReplayDone = replayKey;
    }
  };
  step();
}

// Same selection + same range length as the last finished replay -> just jump to the end.
const replayKey = [rv('$constellation'), rv('${satellite:csv}'), Math.round((T1 - T0) / 60000)].join('|');

// ---- message instead of a map when there is nothing to animate --------------------
if (!sats.length) {
  const err = (data.errors && data.errors[0]) || data.error;
  const msg = err && err.message
    ? err.message
    : 'No satellites to show. Pick a constellation and satellite, and a time range of 24 hours or less.';
  return {
    version: 2,
    notMerge: true,
    option: {
      graphic: [{
        type: 'text', left: 'center', top: 'middle',
        style: { text: msg, fill: COLORS.text, fontSize: 14, width: 500, overflow: 'break', align: 'center' },
      }],
    },
  };
}

// ---- static chart definition (the loop above only updates data) ---------------------
const nameOf = (i) => sats[i].name || String(sats[i].id);

const option = {
  geo: {
    map: 'world',             // world GeoJSON bundled with Business Charts (panel option "Map: JSON")
    roam: true,               // mouse-wheel zoom and drag
    aspectScale: 1,           // plain longitude/latitude grid (equirectangular)
    boundingCoords: [[-180, 90], [180, -90]],
    top: 8, bottom: 8, left: 8, right: 8,
    silent: true,             // countries don't react to hover
    itemStyle: { areaColor: COLORS.land, borderColor: COLORS.border, borderWidth: 0.5 },
  },
  tooltip: {
    trigger: 'item',
    formatter: (p) => {
      const v = p.value;
      return `<b>${nameOf(v[3])}</b> [${sats[v[3]].id}]<br/>` +
        `lat ${v[1].toFixed(2)}°, lon ${v[0].toFixed(2)}°<br/>alt ${v[2]} km`;
    },
  },
  toolbox: {
    right: 12, top: 8,
    iconStyle: { borderColor: COLORS.text },
    feature: {
      myReplay: {
        show: true,
        title: 'Replay',
        icon: 'path://M4,2 L20,12 L4,22 Z',
        onclick: () => play(),
      },
    },
  },
  graphic: [{
    id: 'clock', type: 'text', left: 16, top: 12, z: 100,
    style: { text: '', fill: COLORS.text, fontSize: 13, fontFamily: 'monospace' },
  }],
  series: [
    {
      id: 'night',
      type: 'custom',
      coordinateSystem: 'geo',
      silent: true,
      z: 1,
      data: [[T1]],
      renderItem: (params, api) => ({
        type: 'polygon',
        shape: { points: nightPolygon(api.value(0)).map((p) => api.coord(p)) },
        style: { fill: COLORS.night },
      }),
    },
    {
      id: 'trails',
      type: 'scatter',
      coordinateSystem: 'geo',
      symbolSize: 2,
      itemStyle: { color: COLORS.trail, opacity: 0.7 },
      animation: false,
      z: 2,
      data: [],
    },
    {
      id: 'sats',
      type: 'scatter',
      coordinateSystem: 'geo',
      symbolSize: sats.length > 500 ? 3 : 7,
      itemStyle: { color: COLORS.sat, borderColor: '#fff', borderWidth: sats.length > 500 ? 0 : 1 },
      animation: false,
      large: sats.length > 2000,
      label: {
        show: sats.length <= LABEL_LIMIT,
        position: 'right',
        color: COLORS.text,
        fontSize: 11,
        formatter: (p) => nameOf(p.value[3]),
      },
      z: 3,
      data: [],
    },
  ],
};

// Start after Grafana has applied `option` to the chart.
setTimeout(() => {
  if (chart.__satReplayDone === replayKey) {
    render(T1, false);  // auto-refresh: stay live, no replay
  } else {
    play();
  }
}, 0);

return {
  version: 2,
  notMerge: true,
  option,
  unsubscribe: stop,     // Grafana calls this before re-running the code (new data, panel closed)
};
