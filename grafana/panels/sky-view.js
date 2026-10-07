/*
 * Sky view — code for the "Business Charts" (Apache ECharts) panel.
 *
 * Shows the sky above an observer: zenith in the centre, horizon on the outer
 * circle, North up and East on the LEFT (as on a star chart held overhead).
 * Satellites move across it over the dashboard time range, like the map.
 *
 * This file is the readable source. Grafana runs the copy embedded in
 * grafana/dashboards/satellites.json (panel option "getOption").
 * After editing this file run:   python3 grafana/embed_panel_code.py
 *
 * Data (Infinity queries):
 *   A  /sky?...       satellites while above the horizon: norad_id, name, k (step), t, az, el, range_km, sunlit
 *   B  /observer?...  one row: name, lat, lon
 *   C  /sky_chart     constellation lines (kind=line) and labels (kind=label), RA/Dec in degrees
 *
 * Drawn layers (back to front):
 *   sky disc (colour follows the Sun's altitude), equatorial grid (declination
 *   parallels every 30 deg and hour-circle meridians every 2 h, celestial
 *   equator brighter), constellation stick figures and names, alt/az grid
 *   (30/60 deg rings, N-E-S-W), the Sun, satellite trails and satellites
 *   (bright = sunlit, grey = in Earth's shadow and therefore invisible to the eye).
 */

const chart = context.panel.chart;
const dark = context.grafana.theme.isDark;
const rv = context.grafana.replaceVariables;

const ANIM_MS = (parseFloat(rv('$anim_seconds')) || 10) * 1000;
const LABEL_LIMIT = parseInt(rv('$label_limit'), 10) || 0;
const MIN_EL = parseFloat(rv('$min_el')) || 0;   // hide satellites lower than this
const TRAIL_LIMIT = 25;
const FRAME_MS = 40;
const T0 = Number(rv('${__from}'));
const T1 = Number(rv('${__to}'));

const COLORS = {
  text: dark ? '#d8dee9' : '#24292e',
  ring: 'rgba(160, 175, 200, 0.55)',
  grid: 'rgba(160, 175, 200, 0.22)',
  eqGrid: 'rgba(120, 170, 255, 0.16)',
  equator: 'rgba(120, 170, 255, 0.40)',
  constLine: 'rgba(200, 210, 255, 0.30)',
  constText: 'rgba(200, 210, 255, 0.45)',
  satLit: '#FFE27A',
  satShadow: '#7d8796',
  trail: 'rgba(255, 226, 122, 0.45)',
  sun: '#FFB000',
};

const rad = Math.PI / 180;
const deg = 180 / Math.PI;

// ---- data frames by query ------------------------------------------------------
const data = context.panel.data;
const frameOf = (refId) => (data.series || []).find((s) => s.refId === refId);
const columns = (refId) => {
  const f = frameOf(refId);
  const out = {};
  if (f) f.fields.forEach((x) => { out[x.name] = Array.from(x.values); });
  return out;
};
const A = columns('A');
const B = columns('B');
const C = columns('C');

const obs = B.lat ? { name: B.name[0], lat: B.lat[0], lon: B.lon[0] } : null;

// Satellites: per satellite a map step-index -> sample
const sats = [];
{
  const byId = new Map();
  const n = (A.norad_id || []).length;
  for (let i = 0; i < n; i++) {
    let s = byId.get(A.norad_id[i]);
    if (!s) {
      s = { id: A.norad_id[i], name: String(A.norad_id[i]), samples: new Map() };
      byId.set(s.id, s);
      sats.push(s);
    }
    if (A.name[i]) s.name = A.name[i];
    s.samples.set(A.k[i], { t: A.t[i], az: A.az[i], el: A.el[i], range: A.range_km[i], sunlit: A.sunlit[i] });
  }
}
let STEP_MS = 20000;
for (const s of sats) {
  const ks = Array.from(s.samples.keys()).sort((a, b) => a - b);
  for (let j = 1; j < ks.length; j++) {
    if (ks[j] === ks[j - 1] + 1) { STEP_MS = s.samples.get(ks[j]).t - s.samples.get(ks[j - 1]).t; break; }
  }
  s.keys = ks;
}

// Constellations: polylines in RA/Dec
const constLines = [];
const constLabels = [];
{
  const n = (C.kind || []).length;
  let cur = null;
  let curLine = null;
  for (let i = 0; i < n; i++) {
    if (C.kind[i] === 'label') {
      constLabels.push({ name: C.name[i], ra: C.ra[i], dec: C.dec[i] });
    } else {
      if (C.line[i] !== curLine) { cur = []; constLines.push(cur); curLine = C.line[i]; }
      cur.push([C.ra[i], C.dec[i]]);
    }
  }
}

// ---- astronomy helpers -----------------------------------------------------------
const days = (tMs) => tMs / 86400000 - 10957.5; // days since J2000.0
const lstDeg = (tMs) => ((((280.46061837 + 360.98564736629 * days(tMs) + obs.lon) % 360) + 360) % 360);

// RA/Dec (deg) -> altitude/azimuth (deg) for the observer at time tMs
function raDecToAltAz(ra, dec, lst) {
  const H = (lst - ra) * rad;
  const d = dec * rad;
  const p = obs.lat * rad;
  const alt = Math.asin(Math.sin(p) * Math.sin(d) + Math.cos(p) * Math.cos(d) * Math.cos(H));
  const az = Math.atan2(-Math.cos(d) * Math.sin(H), Math.sin(d) * Math.cos(p) - Math.cos(d) * Math.sin(p) * Math.cos(H));
  return [alt * deg, ((az * deg) % 360 + 360) % 360];
}

// Sun RA/Dec (deg), low precision
function sunRaDec(tMs) {
  const d = days(tMs);
  const g = (357.529 + 0.98560028 * d) * rad;
  const L = (280.459 + 0.98564736 * d + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * rad;
  const e = (23.439 - 0.00000036 * d) * rad;
  return [Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L)) * deg, Math.asin(Math.sin(e) * Math.sin(L)) * deg];
}

// Alt/az -> chart x/y. Zenith at (0,0), horizon at radius 1, North up, East left.
const project = (alt, az) => {
  const r = (90 - alt) / 90;
  return [-Math.sin(az * rad) * r, Math.cos(az * rad) * r];
};

// Turn a list of RA/Dec points into polylines above the horizon (split where they set).
function skyPolylines(points, lst) {
  const out = [];
  let cur = [];
  for (const [ra, dec] of points) {
    const [alt, az] = raDecToAltAz(ra, dec, lst);
    if (alt >= 0) {
      cur.push(project(alt, az));
    } else if (cur.length) {
      if (cur.length > 1) out.push(cur);
      cur = [];
    }
  }
  if (cur.length > 1) out.push(cur);
  return out;
}

// Equatorial grid in RA/Dec, computed once
const decParallels = [];
for (const dec of [-60, -30, 0, 30, 60]) {
  const pts = [];
  for (let ra = 0; ra <= 360; ra += 4) pts.push([ra, dec]);
  decParallels.push({ dec, pts });
}
const raMeridians = [];
for (let ra = 0; ra < 360; ra += 30) {
  const pts = [];
  for (let dec = -88; dec <= 88; dec += 4) pts.push([ra, dec]);
  raMeridians.push(pts);
}

// Interpolated satellite direction at time tMs (great-circle between steps)
function satAt(s, tMs) {
  const x = (tMs - T0) / STEP_MS;
  const k = Math.floor(x);
  const a = s.samples.get(k);
  const b = s.samples.get(k + 1);
  if (!a && !b) return null;
  if (!a || !b) {
    const only = a || b;
    return Math.abs(only.t - tMs) < STEP_MS / 2 ? only : null;
  }
  const f = Math.min(1, Math.max(0, x - k));
  const va = [Math.cos(a.el * rad) * Math.cos(a.az * rad), Math.cos(a.el * rad) * Math.sin(a.az * rad), Math.sin(a.el * rad)];
  const vb = [Math.cos(b.el * rad) * Math.cos(b.az * rad), Math.cos(b.el * rad) * Math.sin(b.az * rad), Math.sin(b.el * rad)];
  const om = Math.acos(Math.min(1, Math.max(-1, va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2])));
  let v = va;
  if (om > 1e-6) {
    const s1 = Math.sin((1 - f) * om) / Math.sin(om);
    const s2 = Math.sin(f * om) / Math.sin(om);
    v = [s1 * va[0] + s2 * vb[0], s1 * va[1] + s2 * vb[1], s1 * va[2] + s2 * vb[2]];
  }
  return {
    el: Math.atan2(v[2], Math.hypot(v[0], v[1])) * deg,
    az: ((Math.atan2(v[1], v[0]) * deg) % 360 + 360) % 360,
    range: Math.round(a.range + (b.range - a.range) * f),
    sunlit: f < 0.5 ? a.sunlit : b.sunlit,
  };
}

const fmtTime = (tMs) => new Date(tMs).toISOString().replace('T', ' ').slice(0, 19) + ' UTC';

// Sky colour from the Sun's altitude: day blue -> twilight -> night
function skyColor(sunAlt) {
  const night = [10, 14, 26];
  const day = [38, 74, 128];
  const f = Math.min(1, Math.max(0, (sunAlt + 12) / 18)); // -12 deg (night) .. +6 deg (day)
  const c = night.map((n, i) => Math.round(n + (day[i] - n) * f));
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}

// ---- draw one moment ------------------------------------------------------------
function render(tMs, playing) {
  const lst = lstDeg(tMs);
  const [sra, sdec] = sunRaDec(tMs);
  const [sunAlt, sunAz] = raDecToAltAz(sra, sdec, lst);

  const eqLines = [];
  for (const p of decParallels) {
    for (const pl of skyPolylines(p.pts, lst)) eqLines.push({ coords: pl, lineStyle: { color: p.dec === 0 ? COLORS.equator : COLORS.eqGrid } });
  }
  for (const m of raMeridians) {
    for (const pl of skyPolylines(m, lst)) eqLines.push({ coords: pl });
  }

  const conLines = [];
  for (const line of constLines) for (const pl of skyPolylines(line, lst)) conLines.push({ coords: pl });
  const conNames = [];
  for (const l of constLabels) {
    const [alt, az] = raDecToAltAz(l.ra, l.dec, lst);
    if (alt > 3) conNames.push({ value: project(alt, az), name: l.name });
  }

  const points = [];
  const trails = [];
  sats.forEach((s, i) => {
    const p = satAt(s, tMs);
    if (p && p.el >= MIN_EL) {
      points.push({ value: [...project(p.el, p.az), i, p.az, p.el, p.range, p.sunlit],
        itemStyle: { color: p.sunlit ? COLORS.satLit : COLORS.satShadow } });
    }
  });
  if (points.length <= TRAIL_LIMIT) {
    for (const pt of points) {
      const s = sats[pt.value[2]];
      for (const k of s.keys) {
        const smp = s.samples.get(k);
        if (smp.t <= tMs && smp.el >= MIN_EL) trails.push(project(smp.el, smp.az));
      }
    }
  }

  chart.setOption({
    series: [
      { id: 'disc', data: [[0, 0]], itemStyle: { color: skyColor(sunAlt) } },
      { id: 'eqgrid', data: eqLines },
      { id: 'constLines', data: conLines },
      { id: 'constNames', data: conNames },
      { id: 'sun', data: sunAlt > -1 ? [project(Math.max(sunAlt, 0), sunAz)] : [] },
      { id: 'trails', data: trails },
      { id: 'sats', data: points, label: { show: points.length <= LABEL_LIMIT } },
    ],
    graphic: [{
      id: 'clock',
      style: {
        text: (playing ? '▶  ' : '■  ') + fmtTime(tMs) + '\n' + obs.name + '   ·   ' + points.length +
          (MIN_EL > 0 ? ' above ' + MIN_EL + '°' : ' above horizon') + (sunAlt > -6 ? '   ·   Sun ' + sunAlt.toFixed(0) + '°' : ''),
      },
    }],
  });
}

// ---- animation loop (same behaviour as the map) -----------------------------------
let timer = null;
const stop = () => { if (timer) { clearTimeout(timer); timer = null; } };
const replayKey = [rv('$constellation'), rv('${satellite:csv}'), rv('$city'), rv('$coords'), Math.round((T1 - T0) / 60000)].join('|');

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
      chart.__skyReplayDone = replayKey;
    }
  };
  step();
}

// ---- message when the observer is unknown (bad coordinates) or the API refused ------
const err = (data.errors && data.errors[0]) || data.error;
if (!obs || !isFinite(T0) || !isFinite(T1) || T1 <= T0 || (err && !sats.length)) {
  const msg = err && err.message ? err.message : 'Choose a city or enter coordinates like "52.52, 13.405", and a time range of 24 hours or less.';
  return {
    version: 2,
    notMerge: true,
    option: {
      graphic: [{ type: 'text', left: 'center', top: 'middle',
        style: { text: msg, fill: COLORS.text, fontSize: 14, width: 460, overflow: 'break', align: 'center' } }],
    },
  };
}

// ---- static chart definition --------------------------------------------------------
// Square plot area: the sky disc must stay round whatever the panel's shape.
const W = chart.getWidth();
const H = chart.getHeight();
const size = Math.max(50, Math.min(W - 40, H - 40));
const grid = { left: (W - size) / 2, top: (H - size) / 2 + 8, width: size, height: size };

// Alt/az grid, fixed
const ring = (alt) => {
  const pts = [];
  for (let az = 0; az <= 360; az += 3) pts.push(project(alt, az));
  return { coords: pts };
};
const altAzLines = [ring(30), ring(60)];
for (let az = 0; az < 360; az += 30) altAzLines.push({ coords: [project(90, az), project(0, az)] });
const cardinals = [['N', 0], ['NE', 45], ['E', 90], ['SE', 135], ['S', 180], ['SW', 225], ['W', 270], ['NW', 315]]
  .map(([name, az]) => ({ name, value: [-Math.sin(az * rad) * 1.07, Math.cos(az * rad) * 1.07] }));

const nameOf = (i) => sats[i].name;
const lineSeries = (id, color, z) => ({
  id, type: 'lines', coordinateSystem: 'cartesian2d', polyline: true, silent: true, z,
  lineStyle: { color, width: 1 }, animation: false, data: [],
});

const option = {
  animation: false,  // every frame replaces the data; built-in transitions would fight the replay
  grid,
  xAxis: { type: 'value', min: -1.12, max: 1.12, show: false },
  yAxis: { type: 'value', min: -1.12, max: 1.12, show: false },
  tooltip: {
    trigger: 'item',
    formatter: (p) => {
      if (p.seriesId !== 'sats') return '';
      const v = p.value;
      return `<b>${nameOf(v[2])}</b> [${sats[v[2]].id}]<br/>az ${v[3].toFixed(1)}°, el ${v[4].toFixed(1)}°<br/>` +
        `range ${v[5]} km<br/>${v[6] ? 'sunlit' : "in Earth's shadow"}`;
    },
  },
  toolbox: {
    right: 12, top: 8,
    iconStyle: { borderColor: COLORS.text },
    feature: { myReplay: { show: true, title: 'Replay', icon: 'path://M4,2 L20,12 L4,22 Z', onclick: () => play() } },
  },
  graphic: [{
    id: 'clock', type: 'text', left: 16, top: 10, z: 100,
    style: { text: '', fill: COLORS.text, fontSize: 12, fontFamily: 'monospace', lineHeight: 17 },
  }],
  series: [
    { // sky disc (radius 1 = horizon), drawn as one big circle symbol sized to the grid
      id: 'disc', type: 'scatter', silent: true, z: 0, animation: false,
      symbolSize: size / 2.24 * 2, data: [[0, 0]], itemStyle: { color: skyColor(-20) },
    },
    { ...lineSeries('eqgrid', COLORS.eqGrid, 1) },
    { ...lineSeries('constLines', COLORS.constLine, 2), lineStyle: { color: COLORS.constLine, width: 1.2 } },
    {
      id: 'constNames', type: 'scatter', silent: true, z: 2, symbolSize: 0, animation: false, data: [],
      label: { show: true, formatter: (p) => p.name, color: COLORS.constText, fontSize: 10 },
    },
    { ...lineSeries('altaz', COLORS.grid, 3), data: altAzLines },
    { // horizon circle
      id: 'horizon', type: 'lines', coordinateSystem: 'cartesian2d', polyline: true, silent: true, z: 3,
      lineStyle: { color: COLORS.ring, width: 1.5 }, animation: false, data: [ring(0)],
    },
    {
      id: 'cardinals', type: 'scatter', silent: true, z: 3, symbolSize: 0, animation: false, data: cardinals,
      label: { show: true, formatter: (p) => p.name, color: COLORS.text, fontSize: 12, fontWeight: 'bold' },
    },
    { id: 'sun', type: 'scatter', silent: true, z: 4, symbolSize: 16, itemStyle: { color: COLORS.sun }, animation: false, data: [] },
    { id: 'trails', type: 'scatter', silent: true, z: 5, symbolSize: 2, itemStyle: { color: COLORS.trail }, animation: false, data: [] },
    {
      id: 'sats', type: 'scatter', z: 6, animation: false,
      symbolSize: sats.length > 300 ? 4 : 7,
      itemStyle: { borderColor: 'rgba(0,0,0,0.6)', borderWidth: 0.5 },
      label: { show: false, position: 'right', color: '#e8ecf1', fontSize: 11, formatter: (p) => nameOf(p.value[2]) },
      data: [],
    },
  ],
};

setTimeout(() => {
  if (chart.__skyReplayDone === replayKey) {
    render(T1, false);
  } else {
    play();
  }
}, 0);

return { version: 2, notMerge: true, option, unsubscribe: stop };
