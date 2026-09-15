/*
 * Live benchmark console - browser client.
 *
 * Deliberately dependency-free: the workshop room may have no internet, and a
 * chart library loaded from a CDN is exactly the thing that fails in front of
 * a customer. Every chart here is hand-built SVG.
 */
'use strict';

const PALETTE = [
  '--cp-accent', '--cp-success', '--cp-warning', '--cp-danger',
  '--cp-link', '--cp-text-soft', '--cp-border-strong', '--cp-text-muted'
];
const APP_BASE = location.pathname.replace(/\/(?:index\.html)?$/, '');
const apiUrl = (path) => APP_BASE + path;

const state = {
  catalog: null,
  dataset: 'scenarios',
  selectedArms: new Map(),   // deployment -> effort ('' = registry default)
  selectedItems: { scenarios: new Set(), router: new Set() },
  runId: null,
  source: null,
  runs: [],
  summaries: [],
  counters: { done: 0, total: 0, ok: 0, err: 0, trunc: 0 },
  startedAt: null,
  timer: null,
  colors: new Map()
};

const $ = (id) => document.getElementById(id);
const themeColor = (token) =>
  getComputedStyle(document.documentElement).getPropertyValue(token).trim();

// ---------------------------------------------------------------- utilities

function colorFor(name) {
  if (!state.colors.has(name)) {
    state.colors.set(name, themeColor(PALETTE[state.colors.size % PALETTE.length]));
  }
  return state.colors.get(name);
}

function fmt(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits
  });
}

function fmtMoney(value) {
  if (value === null || value === undefined) return '—';
  if (value >= 1) return '$' + value.toFixed(2);
  if (value >= 0.01) return '$' + value.toFixed(3);
  return '$' + value.toPrecision(2);
}

function shortLabel(name, max = 18) {
  const trimmed = name.replace(/^router-sol-luna-/, 'router:').replace(/-dz\b/, '');
  if (trimmed.length <= max) return trimmed;
  // The reasoning effort is often the only thing separating two arms, so it
  // must survive truncation even when the model name does not.
  const at = trimmed.lastIndexOf('@');
  if (at > 0) {
    const effort = trimmed.slice(at);
    const head = trimmed.slice(0, at).replace(/^gpt-/, '');
    const room = Math.max(4, max - effort.length);
    return (head.length > room ? head.slice(0, room - 1) + '…' : head) + effort;
  }
  return trimmed.slice(0, max - 1) + '…';
}

function notice(message, kind) {
  const el = $('notice');
  if (!message) { el.classList.add('hidden'); return; }
  el.className = 'notice' + (kind ? ' ' + kind : '');
  el.innerHTML = message;
}

function log(line, cls) {
  const el = $('log');
  const div = document.createElement('div');
  if (cls) div.className = cls;
  div.textContent = line;
  el.appendChild(div);
  while (el.childElementCount > 400) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

// ------------------------------------------------------------- svg plumbing

function el(tag, attrs, text) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

function newSvg(container, width, height) {
  container.innerHTML = '';
  const svg = el('svg', {
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: 'xMidYMid meet',
    role: 'img'
  });
  container.appendChild(svg);
  return svg;
}

function emptyChart(container, message) {
  container.innerHTML = `<p class="muted" style="font-size:12px;padding:18px 4px">${message}</p>`;
}

function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0, 1];
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  // The last tick must sit at or above the data maximum: it is also the axis
  // top, and a bar taller than the axis paints outside the chart.
  const ticks = [];
  for (let v = 0; ; v += step) {
    ticks.push(Number(v.toFixed(10)));
    if (v >= max - step * 1e-9) break;
  }
  return ticks;
}

function axes(svg, geo, ticks, formatter) {
  const { left, top, width, height } = geo;
  const max = ticks[ticks.length - 1] || 1;
  ticks.forEach((t) => {
    const y = top + height - (t / max) * height;
    svg.appendChild(el('line', {
      x1: left, x2: left + width, y1: y, y2: y,
      stroke: themeColor('--cp-border'), 'stroke-width': 1
    }));
    svg.appendChild(el('text', {
      x: left - 8, y: y + 4, 'text-anchor': 'end',
      fill: themeColor('--cp-text-muted'), 'font-size': 10
    }, formatter ? formatter(t) : fmt(t)));
  });
  return max;
}

function xLabels(svg, geo, labels, bandWidth) {
  const { left, top, height } = geo;
  labels.forEach((label, i) => {
    const x = left + bandWidth * (i + 0.5);
    const text = el('text', {
      x, y: top + height + 14, 'text-anchor': 'end',
      fill: themeColor('--cp-text-muted'), 'font-size': 10,
      transform: `rotate(-28 ${x} ${top + height + 14})`
    }, shortLabel(label));
    text.appendChild(el('title', {}, label));
    svg.appendChild(text);
  });
}

function legend(container, entries) {
  const div = document.createElement('div');
  div.className = 'legend';
  div.innerHTML = entries
    .map((e) => `<span><i style="background:${e.color}"></i>${e.label}</span>`)
    .join('');
  container.appendChild(div);
}

// ---------------------------------------------------------------- the charts

function groupedBars(container, labels, series, formatter) {
  if (!labels.length || series.every((s) => s.values.every((v) => v === null))) {
    emptyChart(container, 'No measurements yet.');
    return;
  }
  const W = 520, H = 250;
  const geo = { left: 58, top: 12, width: W - 74, height: H - 74 };
  const svg = newSvg(container, W, H);
  const max = Math.max(...series.flatMap((s) => s.values.filter((v) => v !== null)), 0);
  const ticks = niceTicks(max);
  const axisMax = axes(svg, geo, ticks, formatter);

  const band = geo.width / labels.length;
  const inner = Math.min(band * 0.7, 64);
  const barW = inner / series.length;

  labels.forEach((label, i) => {
    series.forEach((s, j) => {
      const value = s.values[i];
      if (value === null || value === undefined) return;
      const h = (value / axisMax) * geo.height;
      const x = geo.left + band * i + (band - inner) / 2 + barW * j;
      const rect = el('rect', {
        x, y: geo.top + geo.height - h, width: Math.max(barW - 2, 1), height: Math.max(h, 1),
        fill: s.color || colorFor(label), opacity: s.opacity ?? 1, rx: 2
      });
      rect.appendChild(el('title', {}, `${label} · ${s.name}: ${formatter ? formatter(value) : value}`));
      svg.appendChild(rect);
      if (series.length <= 2 && barW > 22) {
        svg.appendChild(el('text', {
          x: x + barW / 2 - 1, y: geo.top + geo.height - h - 4,
          'text-anchor': 'middle', fill: themeColor('--cp-text-muted'), 'font-size': 9
        }, formatter ? formatter(value) : fmt(value)));
      }
    });
  });

  svg.appendChild(el('line', {
    x1: geo.left, x2: geo.left + geo.width, y1: geo.top + geo.height, y2: geo.top + geo.height,
    stroke: themeColor('--cp-border')
  }));
  xLabels(svg, geo, labels, band);
  if (series.length > 1) legend(container, series.map((s) => ({ label: s.name, color: s.color })));
}

function stackedBars(container, labels, series, formatter) {
  if (!labels.length) { emptyChart(container, 'No measurements yet.'); return; }
  const W = 520, H = 250;
  const geo = { left: 58, top: 12, width: W - 74, height: H - 74 };
  const svg = newSvg(container, W, H);
  const totals = labels.map((_, i) => series.reduce((sum, s) => sum + (s.values[i] || 0), 0));
  const ticks = niceTicks(Math.max(...totals, 0));
  const axisMax = axes(svg, geo, ticks, formatter);

  const band = geo.width / labels.length;
  const barW = Math.min(band * 0.6, 52);

  labels.forEach((label, i) => {
    let acc = 0;
    series.forEach((s) => {
      const value = s.values[i] || 0;
      if (value <= 0) return;
      const h = (value / axisMax) * geo.height;
      const y = geo.top + geo.height - (acc / axisMax) * geo.height - h;
      const rect = el('rect', {
        x: geo.left + band * i + (band - barW) / 2, y, width: barW, height: Math.max(h, 1),
        fill: s.color, rx: 2
      });
      rect.appendChild(el('title', {}, `${label} · ${s.name}: ${fmt(value)}`));
      svg.appendChild(rect);
      acc += value;
    });
  });

  svg.appendChild(el('line', {
    x1: geo.left, x2: geo.left + geo.width, y1: geo.top + geo.height, y2: geo.top + geo.height,
    stroke: themeColor('--cp-border')
  }));
  xLabels(svg, geo, labels, band);
  legend(container, series.map((s) => ({ label: s.name, color: s.color })));
}

function scatter(container, points, xLabel, yLabel) {
  const usable = points.filter((p) => p.x !== null && p.y !== null && p.x > 0);
  if (!usable.length) {
    emptyChart(container, 'Needs at least one arm with confirmed pricing.');
    return;
  }
  const W = 520, H = 250;
  const geo = { left: 58, top: 12, width: W - 78, height: H - 70 };
  const svg = newSvg(container, W, H);

  // Cost spans orders of magnitude between a nano model and a frontier one,
  // so a linear x-axis would collapse every cheap arm onto the y-axis.
  const xs = usable.map((p) => Math.log10(p.x));
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const xPad = (xMax - xMin) < 0.2 ? 0.5 : (xMax - xMin) * 0.12;
  const lo = xMin - xPad, hi = xMax + xPad;
  const yTicks = niceTicks(Math.max(...usable.map((p) => p.y)));
  const yMax = axes(svg, geo, yTicks, (v) => fmt(v) + ' ms');

  const px = (v) => geo.left + ((Math.log10(v) - lo) / (hi - lo)) * geo.width;
  const py = (v) => geo.top + geo.height - (v / yMax) * geo.height;

  for (let e = Math.floor(lo); e <= Math.ceil(hi); e++) {
    const v = Math.pow(10, e);
    if (px(v) < geo.left - 1 || px(v) > geo.left + geo.width + 1) continue;
    svg.appendChild(el('line', {
      x1: px(v), x2: px(v), y1: geo.top, y2: geo.top + geo.height,
      stroke: themeColor('--cp-border'), 'stroke-dasharray': '2 3'
    }));
    svg.appendChild(el('text', {
      x: px(v), y: geo.top + geo.height + 14, 'text-anchor': 'middle',
      fill: themeColor('--cp-text-muted'), 'font-size': 10
    }, fmtMoney(v)));
  }

  const maxTokens = Math.max(...usable.map((p) => p.size || 0), 1);
  const placed = [];
  usable.forEach((p) => {
    const r = 5 + 9 * Math.sqrt((p.size || 0) / maxTokens);
    const cx = px(p.x), cy = py(p.y);
    const circle = el('circle', {
      cx, cy, r, fill: colorFor(p.label), opacity: .75,
      stroke: themeColor('--cp-surface'), 'stroke-width': 1.5
    });
    circle.appendChild(el('title', {},
      `${p.label}\n${xLabel}: ${fmtMoney(p.x)}\n${yLabel}: ${fmt(p.y)} ms\nmean output: ${fmt(p.size)} tokens`));
    svg.appendChild(circle);

    // Crowded runs put several arms in the same corner. Try a few offsets and
    // drop the label rather than stack unreadable text; the tooltip still has it.
    const text = shortLabel(p.label, 14);
    const halfWidth = text.length * 2.4;
    for (const dy of [-r - 5, r + 11, -r - 16, r + 22]) {
      const y = cy + dy;
      const box = { x1: cx - halfWidth, x2: cx + halfWidth, y1: y - 8, y2: y + 2 };
      const clash = placed.some((b) =>
        !(box.x2 < b.x1 || box.x1 > b.x2 || box.y2 < b.y1 || box.y1 > b.y2));
      if (!clash) {
        placed.push(box);
        svg.appendChild(el('text', {
          x: cx, y, 'text-anchor': 'middle', fill: themeColor('--cp-text'), 'font-size': 9
        }, text));
        break;
      }
    }
  });

  svg.appendChild(el('text', {
    x: geo.left + geo.width / 2, y: H - 6, 'text-anchor': 'middle',
    fill: themeColor('--cp-text-muted'), 'font-size': 10
  }, xLabel + ' (log scale)'));
  svg.appendChild(el('text', {
    x: 12, y: geo.top + geo.height / 2, 'text-anchor': 'middle',
    fill: themeColor('--cp-text-muted'), 'font-size': 10,
    transform: `rotate(-90 12 ${geo.top + geo.height / 2})`
  }, yLabel));
}

// -------------------------------------------------------------- render pass

function renderCharts() {
  const rows = state.summaries.filter((s) => s.ok > 0);
  const labels = rows.map((s) => s.arm);

  groupedBars($('chartTtft'), labels, [
    { name: 'p50', values: rows.map((s) => s.ttft_p50_ms), color: themeColor('--cp-accent') },
    { name: 'p90', values: rows.map((s) => s.ttft_p90_ms), color: themeColor('--cp-link') }
  ], (v) => fmt(v) + ' ms');

  groupedBars($('chartTps'), labels, [
    { name: 'p50 tok/s', values: rows.map((s) => s.decode_tps_p50), color: themeColor('--cp-success') }
  ], (v) => fmt(v, 1));
  const burstOnly = rows.filter((s) => s.paced_n === 0 && s.bursts > 0);
  const partial = rows.filter((s) => s.paced_n > 0 && s.bursts > 0);
  if (burstOnly.length || partial.length) {
    const note = document.createElement('div');
    note.className = 'legend';
    const parts = [];
    if (burstOnly.length) {
      parts.push(`<span>Not shown — the answer arrived in one burst, so there is no per-token pace to measure: `
        + `<b>${burstOnly.map((s) => shortLabel(s.arm, 24)).join(', ')}</b></span>`);
    }
    if (partial.length) {
      parts.push(`<span>Excludes burst deliveries: `
        + partial.map((s) => `${shortLabel(s.arm, 24)} ${s.bursts}/${s.ok}`).join(', ') + '</span>');
    }
    note.innerHTML = parts.join('');
    $('chartTps').appendChild(note);
  }

  const priced = rows.filter((s) => s.cost_per_1k_requests !== null);
  if (priced.length) {
    groupedBars($('chartCost'), priced.map((s) => s.arm), [
      { name: 'USD / 1k requests', values: priced.map((s) => s.cost_per_1k_requests), color: themeColor('--cp-warning') }
    ], (v) => fmtMoney(v));
  } else {
    emptyChart($('chartCost'), 'No arm in this run has confirmed list pricing.');
  }

  scatter($('chartScatter'), rows.map((s) => ({
    label: s.arm,
    x: s.cost_per_1k_requests,
    y: s.ttft_p50_ms,
    size: s.output_tokens_mean
  })), 'USD per 1,000 requests', 'TTFT p50');

  stackedBars($('chartTokens'), labels, [
    { name: 'prompt', values: rows.map((s) => s.prompt_tokens_mean || 0), color: themeColor('--cp-link') },
    { name: 'reasoning (billed, hidden)', values: rows.map((s) => s.reasoning_tokens_mean || 0), color: themeColor('--cp-accent') },
    { name: 'emitted text', values: rows.map((s) => s.emitted_tokens_mean || 0), color: themeColor('--cp-success') }
  ], (v) => fmt(v));

  renderRouter(rows);
  renderTable(rows);
}

function renderRouter(rows) {
  const routed = rows.filter((s) => s.served_split && Object.keys(s.served_split).length
    && (s.arm.includes('router') || Object.keys(s.served_split).length > 1));
  const card = $('routerCard');
  if (!routed.length) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');

  const models = [...new Set(routed.flatMap((s) => Object.keys(s.served_split)))].sort();
  const series = models.map((m, i) => ({
    name: m,
    color: themeColor(PALETTE[i % PALETTE.length]),
    values: routed.map((s) => {
      const total = Object.values(s.served_split).reduce((a, b) => a + b, 0) || 1;
      return ((s.served_split[m] || 0) / total) * 100;
    })
  }));
  stackedBars($('chartRouter'), routed.map((s) => s.arm), series, (v) => fmt(v) + '%');
}

const COLUMNS = [
  ['arm', 'Arm', (s) => s.arm],
  ['ok', 'OK', (s) => fmt(s.ok)],
  ['errors', 'Err', (s) => fmt(s.errors)],
  ['truncated', 'Trunc', (s) => fmt(s.truncated)],
  ['bursts', 'Burst', (s) => (s.bursts ? `${s.bursts}/${s.ok}` : '0')],
  ['ttft_p50_ms', 'TTFT p50', (s) => fmt(s.ttft_p50_ms) + ' ms'],
  ['ttft_p90_ms', 'TTFT p90', (s) => fmt(s.ttft_p90_ms) + ' ms'],
  ['tpot_p50_ms', 'TPOT p50', (s) => fmt(s.tpot_p50_ms, 1) + ' ms'],
  ['decode_tps_p50', 'tok/s p50', (s) => fmt(s.decode_tps_p50, 1)],
  ['e2e_p50_ms', 'E2E p50', (s) => fmt(s.e2e_p50_ms) + ' ms'],
  ['e2e_p90_ms', 'E2E p90', (s) => fmt(s.e2e_p90_ms) + ' ms'],
  ['output_tokens_mean', 'Out tok', (s) => fmt(s.output_tokens_mean)],
  ['reasoning_tokens_mean', 'Reason tok', (s) => fmt(s.reasoning_tokens_mean)],
  ['cost_per_1k_requests', 'USD/1k req', (s) => fmtMoney(s.cost_per_1k_requests)],
  ['cost_per_1k_emitted_tokens', 'USD/1k out tok', (s) => fmtMoney(s.cost_per_1k_emitted_tokens)],
  ['value_ratio', 'x cheaper', (s) => (s.value_ratio ? s.value_ratio + '×' : '—')],
  ['run_output_tps', 'Run tok/s', (s) => fmt(s.run_output_tps, 1)]
];

function renderTable(rows) {
  const table = $('summaryTable');
  table.tHead.innerHTML = '<tr>' + COLUMNS.map((c) => `<th>${c[1]}</th>`).join('') + '</tr>';
  const body = table.tBodies[0];
  body.innerHTML = '';
  rows.forEach((s) => {
    const tr = document.createElement('tr');
    tr.innerHTML = COLUMNS.map((c) => `<td>${c[2](s)}</td>`).join('');
    body.appendChild(tr);
  });
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="' + COLUMNS.length + '" class="muted">Run a benchmark or load a recorded run.</td></tr>';
  }
}

// ------------------------------------------------------------------ setup UI

function renderArms() {
  const host = $('armList');
  host.innerHTML = '';
  const groups = [
    ['Study candidates', state.catalog.arms.filter((a) => a.is_study_model)],
    ['Other direct deployments', state.catalog.arms.filter((a) => !a.is_router && !a.is_study_model)],
    ['Model Router', state.catalog.arms.filter((a) => a.is_router)]
  ];
  for (const [title, arms] of groups) {
    if (!arms.length) continue;
    const heading = document.createElement('div');
    heading.className = 'group-title';
    heading.textContent = title;
    if (title === 'Study candidates') {
      const pill = document.createElement('span');
      pill.className = 'pill study';
      pill.textContent = 'in the report';
      pill.title = 'The three models the written scenario study compares.';
      heading.appendChild(pill);
    }
    host.appendChild(heading);
    arms.forEach((arm) => host.appendChild(armRow(arm)));
  }
  const regions = state.catalog.regions || [];
  const note = document.createElement('div');
  note.className = 'region-note';
  note.innerHTML = regions.length
    ? `All arms are served by one Azure OpenAI resource, verified in <b>${regions.join(', ')}</b>. `
      + 'One resource means one region, so a latency difference between arms is not geography.'
    : 'Region was not recorded for these deployments. Check that every arm lives on one resource '
      + 'before comparing latency.';
  host.appendChild(note);
}

function armRow(arm) {
  const label = document.createElement('label');
  label.className = 'opt' + (arm.is_router ? ' router' : '');

  const box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = state.selectedArms.has(arm.deployment);

  const body = document.createElement('div');
  body.className = 'body';

  const name = document.createElement('div');
  name.className = 'name';
  name.textContent = arm.deployment;

  const meta = document.createElement('div');
  meta.className = 'meta';
  const bits = [];
  if (arm.is_router) {
    bits.push(`router · ${arm.routing_mode || 'mode n/a'}`);
    if (arm.router_subset) bits.push(arm.router_subset.join(' + '));
  } else {
    bits.push(arm.family || 'unknown');
    bits.push(arm.priced
      ? `$${arm.price_input}/$${arm.price_output} per 1M in/out`
      : 'no confirmed price');
  }
  if (arm.region) bits.push(arm.region);
  if (arm.sku) bits.push(arm.sku);
  meta.textContent = bits.join(' · ');

  body.appendChild(name);
  body.appendChild(meta);

  // A reasoning model is not one arm but one arm per effort, and comparing
  // those permutations is the point of the study. Each effort is its own chip.
  if (!arm.is_router && arm.supported_efforts.length) {
    const chips = document.createElement('div');
    chips.className = 'chips';
    const options = [['', `default (${arm.default_effort ?? 'not sent'})`]]
      .concat(arm.supported_efforts.map((e) => [e, e]));
    options.forEach(([value, text]) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip';
      chip.textContent = text;
      chip.dataset.effort = value;
      chip.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        toggleEffort(arm.deployment, value);
        renderArms();
        updateEstimate();
      });
      chips.appendChild(chip);
    });
    body.appendChild(chips);
    syncChips(chips, state.selectedArms.get(arm.deployment));
  }

  box.addEventListener('change', () => {
    if (box.checked) {
      state.selectedArms.set(arm.deployment, new Set(['']));
    } else {
      state.selectedArms.delete(arm.deployment);
    }
    renderArms();
    updateEstimate();
  });

  label.classList.toggle('on', box.checked);
  label.appendChild(box);
  label.appendChild(body);
  return label;
}

function syncChips(chips, efforts) {
  chips.querySelectorAll('.chip').forEach((chip) => {
    chip.classList.toggle('on', Boolean(efforts && efforts.has(chip.dataset.effort)));
  });
}

function toggleEffort(deployment, effort) {
  const efforts = state.selectedArms.get(deployment) || new Set();
  if (efforts.has(effort)) {
    efforts.delete(effort);
  } else {
    efforts.add(effort);
  }
  if (efforts.size) {
    state.selectedArms.set(deployment, efforts);
  } else {
    state.selectedArms.delete(deployment);
  }
}

function selectedArmList() {
  const arms = [];
  state.selectedArms.forEach((efforts, deployment) => {
    efforts.forEach((effort) => arms.push({ deployment, effort }));
  });
  return arms;
}

function currentItems() {
  return state.dataset === 'router'
    ? state.catalog.router_tiers
    : state.catalog.scenarios;
}

function renderItems() {
  const host = $('itemList');
  host.innerHTML = '';
  const items = currentItems();
  if (!items.length) {
    host.innerHTML = '<p class="muted" style="font-size:12px">This dataset is not present next to the console.</p>';
    return;
  }
  const selected = state.selectedItems[state.dataset];
  const groups = new Map();
  items.forEach((item) => {
    const key = state.dataset === 'router'
      ? `tier: ${item.tier || 'n/a'}`
      : item.scenario_label;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });

  groups.forEach((rows, key) => {
    const heading = document.createElement('div');
    heading.className = 'group-title';
    heading.textContent = key;
    if (rows[0].scope === 'partial') {
      const pill = document.createElement('span');
      pill.className = 'pill scope';
      pill.textContent = 'text proxy';
      pill.title = rows[0].scope_note || 'Measured through a text proxy only.';
      heading.appendChild(pill);
    }
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'ghost';
    toggle.textContent = 'all';
    toggle.addEventListener('click', () => {
      const everySelected = rows.every((r) => selected.has(r.id));
      rows.forEach((r) => everySelected ? selected.delete(r.id) : selected.add(r.id));
      renderItems();
      updateEstimate();
    });
    heading.appendChild(toggle);
    host.appendChild(heading);

    rows.forEach((item) => {
      const label = document.createElement('label');
      label.className = 'opt' + (selected.has(item.id) ? ' on' : '');
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = selected.has(item.id);
      box.addEventListener('change', () => {
        box.checked ? selected.add(item.id) : selected.delete(item.id);
        label.classList.toggle('on', box.checked);
        updateEstimate();
      });
      const body = document.createElement('div');
      body.className = 'body';
      const budget = item.answer_budget ? ` · budget ${item.answer_budget} tok` : '';
      body.innerHTML = `<div class="name">${item.id}${budget
        ? `<span class="pill">${item.answer_budget} tok</span>` : ''}</div>`
        + `<div class="meta">${escapeHtml(item.preview)}…</div>`;
      body.title = item.preview;
      label.appendChild(box);
      label.appendChild(body);
      host.appendChild(label);
    });
  });
}

function escapeHtml(text) {
  return String(text).replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
}

function updateEstimate() {
  const arms = selectedArmList().length;
  const items = state.selectedItems[state.dataset].size;
  const iterations = Math.max(1, Number($('iterations').value) || 1);
  const warmup = $('warmup').checked ? 1 : 0;
  const total = arms * items * (iterations + warmup);
  const measured = arms * items * iterations;
  const limit = state.catalog?.limits?.max_requests ?? 400;
  const over = total > limit;
  $('estimate').innerHTML = over
    ? `<b class="estimate-error">${total} requests</b> exceeds the ${limit}-request cap for one run. Reduce prompts, arms or iterations.`
    : `<b>${total}</b> requests (${measured} measured${warmup ? ` + ${total - measured} warm-up` : ''}) across <b>${arms}</b> arm(s) &times; <b>${items}</b> prompt(s).`;
  $('runBtn').disabled = over || !arms || !items || state.runId !== null
    || state.catalog?.mode !== 'live';
}

// --------------------------------------------------------------- run control

function resetCounters(total) {
  state.counters = {
    done: 0, total, ok: 0, err: 0, trunc: 0,
    promptTokens: 0, outputTokens: 0, reasoningTokens: 0, cost: 0
  };
  state.summaries = [];
  state.startedAt = Date.now();
  $('log').innerHTML = '';
  paintCounters();
  paintTotals(null);
  clearInterval(state.timer);
  state.timer = setInterval(paintCounters, 1000);
}

function paintCounters() {
  const c = state.counters;
  $('cDone').textContent = c.done;
  $('cTotal').textContent = c.total;
  $('cOk').textContent = c.ok;
  $('cErr').textContent = c.err;
  $('cTrunc').textContent = c.trunc;
  $('cElapsed').textContent = state.startedAt
    ? Math.round((Date.now() - state.startedAt) / 1000) + 's' : '0s';
  $('progressBar').style.width = c.total ? (c.done / c.total * 100) + '%' : '0%';
  $('cSource').textContent = state.source || '';
}

async function startRun() {
  const body = {
    dataset: state.dataset,
    arms: selectedArmList(),
    items: [...state.selectedItems[state.dataset]],
    iterations: Number($('iterations').value) || 3,
    concurrency: Number($('concurrency').value) || 1,
    headroom: Number($('headroom').value),
    warmup: $('warmup').checked
  };
  notice('');
  try {
    const response = await fetch(apiUrl('/api/run'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Run rejected');
    state.runId = data.run_id;
    state.source = 'live run';
    rememberActiveRun(data.run_id);
    $('cancelBtn').classList.remove('hidden');
    $('exportBtn').disabled = true;
    updateEstimate();
    openStream(data.run_id);
  } catch (err) {
    notice(`<b>Could not start the run.</b> ${err.message}`, 'error');
  }
}

const ACTIVE_RUN_KEY = 'benchmark.activeRun';

function rememberActiveRun(runId) {
  try {
    if (runId) window.sessionStorage.setItem(ACTIVE_RUN_KEY, runId);
    else window.sessionStorage.removeItem(ACTIVE_RUN_KEY);
  } catch (err) {
    // Private modes can refuse storage; reattaching is a convenience.
  }
}

function rememberedRun() {
  try {
    return window.sessionStorage.getItem(ACTIVE_RUN_KEY);
  } catch (err) {
    return null;
  }
}

async function runHasLanded(runId) {
  try {
    const response = await fetch(apiUrl(`/api/history/${encodeURIComponent(runId)}`));
    return response.ok;
  } catch (err) {
    return false;
  }
}

function openStream(runId, attempt) {
  const tries = attempt || 0;
  const stream = new EventSource(apiUrl(`/api/events?run_id=${encodeURIComponent(runId)}`));
  stream.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    handleEvent(payload, stream);
  };
  stream.onerror = async () => {
    stream.close();
    // The server closes the connection after 'done', which clears runId.
    if (state.runId !== runId) return;
    // Losing the stream no longer stops the measurement, so do not pretend the
    // run ended: find out whether it did, and otherwise keep Stop available.
    if (await runHasLanded(runId)) {
      finishRun();
      await loadRunIndex();
      notice('<b>The run finished while the stream was down.</b> '
        + 'It is in Past runs.', 'warn');
      return;
    }
    const wait = Math.min(2000 * (tries + 1), 15000);
    notice('<b>Lost the live stream.</b> The run is still measuring on the '
      + 'benchmark VM; reconnecting. Stop ends it.', 'warn');
    window.setTimeout(() => {
      if (state.runId === runId) openStream(runId, tries + 1);
    }, wait);
  };
}

async function reattachActiveRun() {
  const runId = rememberedRun();
  if (!runId || state.catalog.mode !== 'live') return;
  if (await runHasLanded(runId)) { rememberActiveRun(null); return; }
  state.runId = runId;
  state.source = 'live run';
  state.startedAt = Date.now();
  $('cancelBtn').classList.remove('hidden');
  notice('<b>Reattached to a run still in progress.</b> '
    + 'It kept measuring while this page was away.', 'warn');
  openStream(runId);
}

function handleEvent(payload, stream) {
  switch (payload.type) {
    case 'run_start':
      resetCounters(payload.total);
      log(`run start · ${payload.arms.length} arm(s) · ${payload.items.length} prompt(s) · `
        + `${payload.iterations} iteration(s) · concurrency ${payload.concurrency} · `
        + `api ${payload.api} · headroom ${payload.headroom ?? 'flat cap'}`);
      break;
    case 'arm_start':
      log(`── ${payload.arm} (${payload.api})`);
      break;
    case 'record': {
      const c = state.counters;
      c.done = payload.done;
      if (payload.error) { c.err++; } else { c.ok++; }
      if (payload.truncated) c.trunc++;
      if (!payload.warmup && !payload.error) {
        c.promptTokens += payload.prompt_tokens || 0;
        c.outputTokens += payload.completion_tokens || 0;
        c.reasoningTokens += payload.reasoning_tokens || 0;
        if (payload.cost_usd) c.cost += payload.cost_usd;
      }
      const line = `${payload.warmup ? 'warm ' : '     '}${payload.item_id.padEnd(6)} `
        + `${(payload.arm || '').padEnd(26)} `
        + (payload.error
          ? `ERROR ${payload.error}`
          : `ttft ${String(payload.ttft_ms ?? '—').padStart(7)}ms  `
            + `in ${String(payload.prompt_tokens ?? 0).padStart(5)}  `
            + `out ${String(payload.completion_tokens ?? 0).padStart(5)}  `
            + `tok/s ${String(payload.decode_tps ?? '—').padStart(6)}`
            + (payload.model_actually_served ? `  → ${payload.model_actually_served}` : ''));
      log(line, payload.error ? 'err' : (payload.warmup ? 'warm' : null));
      paintCounters();
      paintTotals({
        prompt_tokens: c.promptTokens,
        output_tokens: c.outputTokens,
        reasoning_tokens: c.reasoningTokens,
        total_tokens: c.promptTokens + c.outputTokens,
        cost_usd: c.cost || null,
        cost_complete: true
      });
      break;
    }
    case 'arm_summary':
      state.summaries = state.summaries.filter((s) => s.arm !== payload.arm).concat(payload);
      renderCharts();
      break;
    case 'done':
      state.summaries = payload.summaries || state.summaries;
      renderCharts();
      paintTotals(payload.totals);
      log(payload.cancelled ? 'run stopped by operator' : 'run complete');
      if (payload.saved) log('saved to history');
      if (stream) stream.close();
      finishRun();
      loadRunIndex();
      break;
    case 'error':
      notice(`<b>The run failed.</b> ${payload.message}`, 'error');
      log(payload.message, 'err');
      if (stream) stream.close();
      finishRun();
      break;
    default:
      break;
  }
}

function finishRun() {
  state.lastRunId = state.runId;
  state.runId = null;
  rememberActiveRun(null);
  clearInterval(state.timer);
  paintCounters();
  $('cancelBtn').classList.add('hidden');
  updateEstimate();
}

async function cancelRun() {
  if (!state.runId) return;
  await fetch(apiUrl('/api/cancel'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_id: state.runId })
  });
  log('stop requested; finishing the in-flight requests');
}

// ------------------------------------------------------------------- replay

async function loadRunIndex() {
  const runs = [];
  try {
    const replayResponse = await fetch(apiUrl('/api/replay'));
    const replayData = await replayResponse.json();
    (replayData.runs || []).forEach((run) => runs.push({
      kind: 'recorded',
      key: `recorded:${run.id}`,
      title: `Study · ${run.title}`,
      description: run.description,
      detail: `${run.folder} · ${run.records} records`,
      summaries: run.summaries,
      records: run.records
    }));
  } catch (err) {
    // A missing replay pack is not fatal; the history list still works.
  }
  try {
    const historyResponse = await fetch(apiUrl('/api/history'));
    const historyData = await historyResponse.json();
    (historyData.runs || []).forEach((run) => runs.push({
      kind: 'history',
      key: `history:${run.run_id}`,
      runId: run.run_id,
      title: `${formatStamp(run.started_at)} · ${run.arms.length} arm(s) × ${run.items.length} prompt(s)`,
      description: `${run.iterations} iteration(s), concurrency ${run.concurrency}`
        + (run.cancelled ? ', stopped early' : ''),
      detail: `${run.records} records · ${run.duration_s}s · ${run.regions?.join(', ') || 'region n/a'}`,
      totals: run.totals,
      records: run.records
    }));
  } catch (err) {
    // Ditto for history.
  }

  state.runs = runs;
  const select = $('replaySelect');
  select.innerHTML = '';
  if (!runs.length) {
    $('replayField').classList.add('hidden');
    $('replayBtn').disabled = true;
    return;
  }
  $('replayField').classList.remove('hidden');
  $('replayBtn').disabled = false;
  const groups = [['Your runs', 'history'], ['Recorded study runs', 'recorded']];
  groups.forEach(([label, kind]) => {
    const matching = runs.filter((r) => r.kind === kind);
    if (!matching.length) return;
    const group = document.createElement('optgroup');
    group.label = label;
    matching.forEach((run) => {
      const option = document.createElement('option');
      option.value = run.key;
      option.textContent = run.title;
      group.appendChild(option);
    });
    select.appendChild(group);
  });
  syncRunButtons();
}

function formatStamp(iso) {
  if (!iso) return 'run';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function selectedRun() {
  return state.runs.find((r) => r.key === $('replaySelect').value) || null;
}

function syncRunButtons() {
  const run = selectedRun();
  const isHistory = run && run.kind === 'history';
  $('deleteBtn').disabled = !isHistory;
  $('exportBtn').disabled = !(isHistory || state.lastRunId);
}

async function openRun() {
  const choice = selectedRun();
  if (!choice) {
    notice('<b>Nothing saved yet.</b> Run a benchmark, or build the recorded pack with '
      + '<span class="mono">python scripts/build_replay_pack.py</span>.', 'warn');
    return;
  }
  let summaries = choice.summaries;
  let totals = choice.totals;
  if (choice.kind === 'history') {
    const response = await fetch(apiUrl(`/api/history/${choice.runId}`));
    const data = await response.json();
    if (!response.ok) {
      notice(`<b>Could not open that run.</b> ${data.error || ''}`, 'error');
      return;
    }
    summaries = data.summaries;
    totals = data.totals;
    state.lastRunId = choice.runId;
  }
  state.summaries = summaries || [];
  state.source = choice.kind === 'history' ? 'saved run' : 'recorded study run';
  state.counters = {
    done: choice.records ?? 0, total: choice.records ?? 0,
    ok: state.summaries.reduce((a, s) => a + (s.ok || 0), 0),
    err: state.summaries.reduce((a, s) => a + (s.errors || 0), 0),
    trunc: state.summaries.reduce((a, s) => a + (s.truncated || 0), 0)
  };
  state.startedAt = null;
  clearInterval(state.timer);
  paintCounters();
  paintTotals(totals || totalsFromSummaries(state.summaries));
  $('log').innerHTML = '';
  log(`opened: ${choice.title}`);
  if (choice.detail) log(choice.detail);
  renderCharts();
  syncRunButtons();
  notice(choice.kind === 'history'
    ? `<b>Showing a saved run from ${choice.title.split(' · ')[0]}.</b> ${choice.description || ''}`
    : `<b>Showing a recorded study run, not a live test.</b> ${choice.description || ''}`, 'warn');
}

async function deleteRun() {
  const choice = selectedRun();
  if (!choice || choice.kind !== 'history') return;
  await fetch(apiUrl(`/api/history/${choice.runId}`), { method: 'DELETE' });
  await loadRunIndex();
  log(`deleted saved run ${choice.runId}`);
}

function totalsFromSummaries(summaries) {
  const sum = (key) => summaries.reduce((a, s) => a + (s[key] || 0), 0);
  const priced = summaries.filter((s) => s.cost_usd_total !== null && s.cost_usd_total !== undefined);
  return {
    prompt_tokens: sum('prompt_tokens_total'),
    output_tokens: sum('output_tokens_total'),
    reasoning_tokens: sum('reasoning_tokens_total'),
    total_tokens: sum('total_tokens'),
    cost_usd: priced.length ? priced.reduce((a, s) => a + s.cost_usd_total, 0) : null,
    cost_complete: summaries.length > 0 && priced.length === summaries.length
  };
}

function paintTotals(totals) {
  const t = totals || {};
  $('tPrompt').textContent = t.prompt_tokens ? fmt(t.prompt_tokens) : '—';
  $('tOutput').textContent = t.output_tokens ? fmt(t.output_tokens) : '—';
  $('tReason').textContent = t.reasoning_tokens !== undefined && t.reasoning_tokens !== null
    ? fmt(t.reasoning_tokens) : '—';
  $('tTotal').textContent = t.total_tokens ? fmt(t.total_tokens) : '—';
  const cost = $('tCost');
  if (t.cost_usd === null || t.cost_usd === undefined) {
    cost.textContent = '—';
    cost.title = 'No arm in this run has a confirmed list price.';
  } else {
    cost.textContent = '$' + Number(t.cost_usd).toFixed(4);
    cost.title = t.cost_complete
      ? 'List-price estimate from the tokens this run actually consumed.'
      : 'Partial: at least one arm in this run has no confirmed list price.';
    if (!t.cost_complete) cost.textContent += ' (partial)';
  }
}

// ------------------------------------------------------------------ presets

function applyPreset(kind) {
  state.selectedArms.clear();
  state.selectedItems.scenarios.clear();
  state.dataset = 'scenarios';
  document.querySelectorAll('#datasetTabs .tab').forEach((t) =>
    t.classList.toggle('on', t.dataset.dataset === 'scenarios'));

  if (kind === 'clear') {
    renderArms();
    renderItems();
    updateEstimate();
    return;
  }

  const studyArms = state.catalog.arms.filter((a) => a.is_study_model);
  if (kind === 'study') {
    // The written study's shape: each candidate model across every reasoning
    // effort it supports, over every assistant prompt.
    studyArms.forEach((arm) => {
      state.selectedArms.set(arm.deployment, new Set(
        arm.supported_efforts.length ? arm.supported_efforts : ['']));
    });
    state.catalog.scenarios.forEach((s) => state.selectedItems.scenarios.add(s.id));
    $('iterations').value = 1;
  } else {
    studyArms.forEach((arm) => state.selectedArms.set(arm.deployment, new Set([''])));
    // One prompt per scenario keeps the demo short but still covers all six.
    const seen = new Set();
    state.catalog.scenarios.forEach((s) => {
      if (!seen.has(s.scenario)) { seen.add(s.scenario); state.selectedItems.scenarios.add(s.id); }
    });
    $('iterations').value = 2;
  }
  renderArms();
  renderItems();
  updateEstimate();
}

async function init() {
  renderTable([]);
  try {
    const response = await fetch(apiUrl('/api/catalog'));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'catalog unavailable');
    state.catalog = data;
  } catch (err) {
    notice(`<b>The console could not read its catalog.</b> ${err.message}`, 'error');
    return;
  }

  const badge = $('modeBadge');
  badge.textContent = state.catalog.mode === 'live' ? 'LIVE' : 'REPLAY ONLY';
  badge.className = 'badge ' + state.catalog.mode;
  if (state.catalog.endpoint) {
    $('endpointBadge').textContent = state.catalog.endpoint;
  } else {
    $('endpointBadge').classList.add('hidden');
  }

  $('headroom').value = state.catalog.defaults.headroom;
  $('iterations').value = state.catalog.defaults.iterations;
  $('concurrency').max = state.catalog.limits.max_concurrency;

  applyPreset('quick');
  await loadRunIndex();
  await reattachActiveRun();

  if (state.catalog.mode !== 'live') {
    notice('<b>Replay mode.</b> <span class="mono">AZURE_OPENAI_ENDPOINT</span> is not set, '
      + 'so nothing can be measured live. Load your environment and restart to run against real deployments, '
      + 'or open a saved or recorded run to see the charts.', 'warn');
  }

  $('runBtn').addEventListener('click', startRun);
  $('cancelBtn').addEventListener('click', cancelRun);
  $('replayBtn').addEventListener('click', openRun);
  $('deleteBtn').addEventListener('click', deleteRun);
  $('replaySelect').addEventListener('change', syncRunButtons);
  $('presetStudy').addEventListener('click', () => applyPreset('study'));
  $('presetQuick').addEventListener('click', () => applyPreset('quick'));
  $('presetClear').addEventListener('click', () => applyPreset('clear'));
  $('exportBtn').addEventListener('click', () => {
    const choice = selectedRun();
    const id = (choice && choice.kind === 'history') ? choice.runId : state.lastRunId;
    if (id) window.location = apiUrl(`/api/export?run_id=${id}`);
  });
  ['iterations', 'concurrency', 'warmup'].forEach((id) =>
    $(id).addEventListener('input', updateEstimate));
  document.querySelectorAll('#datasetTabs .tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#datasetTabs .tab').forEach((t) => t.classList.remove('on'));
      tab.classList.add('on');
      state.dataset = tab.dataset.dataset;
      renderItems();
      updateEstimate();
    });
  });
}

init();
