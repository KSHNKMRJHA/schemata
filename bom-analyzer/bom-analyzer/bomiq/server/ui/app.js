/* ==========================================================================
   BOM-IQ — single-page UI
   Vanilla JS, no build step, no dependencies. Talks to the local REST API.

   Layout of this file
     1. state + api
     2. small helpers (escaping, formatting, DOM, toasts)
     3. charts (donut, bar rows, price-break line) — inline SVG
     4. generic sortable/filterable table
     5. views
     6. drawer (part detail)
     7. settings + export
     8. boot
   ========================================================================== */
'use strict';

/* ---------------------------------------------------------------- 1. state */

const S = {
  token: new URLSearchParams(location.search).get('t') || '',
  boot: null,
  settings: {},
  providers: [],
  fields: [],
  upload: null,        // { upload_id, mapping, preview, available_sheets, ... }
  job: null,
  analysis: null,
  analysisId: null,
  view: 'start',
  selectedLine: null,
  poll: null,
  runStart: 0,
  sort: {},            // per-table sort state
  filters: {},
};

async function api(path, options = {}) {
  const opts = Object.assign({ headers: {} }, options);
  opts.headers = Object.assign({ 'X-BOMIQ-Token': S.token }, opts.headers);
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers['Content-Type'] = 'application/json';
    opts.method = opts.method || 'POST';
    delete opts.json;
  }
  let response;
  try {
    response = await fetch(path, opts);
  } catch (err) {
    throw new Error('Could not reach the local BOM-IQ service. It may have ' +
                    'stopped — restart the app.');
  }
  if (response.status === 204) return null;
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch (_) { payload = null; }
  if (!response.ok) {
    const message = (payload && (payload.error || payload.message)) ||
                    `Request failed (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    error.detail = payload && payload.detail;
    throw error;
  }
  return payload;
}

/* -------------------------------------------------------------- 2. helpers */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function num(value, places = 0) {
  if (value === null || value === undefined || value === '') return '';
  const n = Number(value);
  if (!isFinite(n)) return '';
  return n.toLocaleString(undefined, { minimumFractionDigits: places,
                                       maximumFractionDigits: places });
}

function money(value, places) {
  if (value === null || value === undefined || value === '') return '';
  const n = Number(value);
  if (!isFinite(n)) return '';
  if (places === undefined) {
    const abs = Math.abs(n);
    places = abs === 0 ? 2 : abs < 0.01 ? 5 : abs < 1 ? 4 : 2;
  }
  return n.toLocaleString(undefined, { minimumFractionDigits: places,
                                       maximumFractionDigits: places });
}

function pct(part, whole) {
  if (!whole) return '';
  return Math.round((100 * part) / whole) + '%';
}

const STATUS_CLASS = {
  Low: 'good', Medium: 'warning', High: 'serious', Critical: 'critical',
  Active: 'good', New: 'info', Unknown: 'neutral', NRND: 'warning',
  EOL: 'serious', Obsolete: 'critical', 'No data': 'neutral',
  Compliant: 'good', Exempt: 'warning', 'Non-compliant': 'critical',
  error: 'critical', warning: 'warning', info: 'neutral',
};

const STATUS_VAR = {
  good: 'var(--good)', warning: 'var(--warning)', serious: 'var(--serious)',
  critical: 'var(--critical)', info: 'var(--series-1)',
  neutral: 'var(--neutral)',
};

function pill(label, kind) {
  if (label === null || label === undefined || label === '') return '';
  const cls = kind || STATUS_CLASS[label] || 'soft';
  return `<span class="pill ${cls}">${esc(label)}</span>`;
}

function softPills(values) {
  return (values || []).map(v => `<span class="pill soft">${esc(v)}</span>`)
    .join(' ');
}

/** Heat cue for a 0-100 risk score: colour plus the number itself. */
function heat(score) {
  const kind = score >= 75 ? 'critical' : score >= 50 ? 'serious'
    : score >= 25 ? 'warning' : 'good';
  return `<span class="heat" style="background:color-mix(in srgb,` +
    `${STATUS_VAR[kind]} 18%, transparent);color:${STATUS_VAR[kind]}">` +
    `${num(score, 0)}</span>`;
}

function toast(message, kind = '', ms = 4200) {
  const node = document.createElement('div');
  node.className = 'toast' + (kind ? ' ' + kind : '');
  node.textContent = message;
  $('#toasts').appendChild(node);
  setTimeout(() => { node.style.opacity = '0'; setTimeout(() => node.remove(), 250); }, ms);
}

function show(view) {
  S.view = view;
  ['start', 'mapping', 'running', 'dashboard', 'bom', 'risk', 'sourcing',
   'alternates', 'compliance', 'issues', 'search'].forEach(name => {
    const node = $('#view-' + name);
    if (node) node.hidden = name !== view;
  });
  const tabViews = ['dashboard', 'bom', 'risk', 'sourcing', 'alternates',
                    'compliance', 'issues', 'search'];
  $('#tabs').hidden = !S.analysis;
  $$('#tabs .tab').forEach(tab => {
    tab.setAttribute('aria-selected', String(tab.dataset.view === view));
  });
  if (tabViews.includes(view)) window.scrollTo(0, 0);
}

function download(url, filename) {
  const link = document.createElement('a');
  link.href = url;
  if (filename) link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

/* --------------------------------------------------------------- 3. charts */

/**
 * Donut gauge for a single 0-100 headline number.
 * One arc, one status colour, the figure in the middle — no legend needed
 * because the title names it.
 */
function donut(score, kind, size = 116) {
  const stroke = 11;
  const r = size / 2 - stroke / 2 - 1;
  const c = 2 * Math.PI * r;
  const filled = (c * Math.max(0, Math.min(100, score))) / 100;
  const colour = STATUS_VAR[kind] || 'var(--series-1)';
  return `
<div class="chart gauge">
  <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}"
       role="img" aria-label="Health score ${num(score, 0)} out of 100">
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none"
            stroke="var(--surface-3)" stroke-width="${stroke}"/>
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none"
            stroke="${colour}" stroke-width="${stroke}" stroke-linecap="round"
            stroke-dasharray="${filled.toFixed(2)} ${c.toFixed(2)}"
            transform="rotate(-90 ${size / 2} ${size / 2})"/>
    <text x="50%" y="48%" text-anchor="middle" dy="0.1em"
          font-size="27" font-weight="700" fill="var(--ink)"
          font-family="var(--font)">${num(score, 0)}</text>
    <text x="50%" y="68%" text-anchor="middle" font-size="10.5"
          fill="var(--ink-3)" font-family="var(--font)">of 100</text>
  </svg>
</div>`;
}

/**
 * Labelled horizontal bar rows for a distribution.
 * Deliberately not a stacked bar: the status ramp has adjacent pairs that fall
 * below the colour-separation floor, and one directly-labelled row per
 * category reads better anyway.
 */
function barRows(items, total, opts = {}) {
  const max = opts.scaleToMax
    ? Math.max(1, ...items.map(i => i.value))
    : Math.max(1, total || 0);
  const rows = items.filter(i => opts.keepZero || i.value > 0).map(item => {
    const colour = STATUS_VAR[item.kind] || item.colour || 'var(--series-1)';
    const width = Math.max(1.5, (100 * item.value) / max);
    const share = total ? ` <small>${pct(item.value, total)}</small>` : '';
    return `
  <div class="barrow">
    <span class="label"><i class="swatch" style="background:${colour}"
      aria-hidden="true"></i>${esc(item.label)}</span>
    <span class="track"><i style="width:${width.toFixed(1)}%;
      background:${colour}"></i></span>
    <span class="value">${num(item.value)}${share}</span>
  </div>`;
  });
  if (!rows.length) return '<p class="muted">No data.</p>';
  return `<div class="barrows">${rows.join('')}</div>`;
}

/**
 * Price-break line chart: one series, so no legend; 2px line, 8px markers,
 * crosshair + tooltip on hover, log-ish x because breaks are decades.
 */
function priceCurve(points, currency) {
  if (!points || points.length < 2) return '';
  const W = 460, H = 168, P = { t: 14, r: 16, b: 30, l: 52 };
  const xs = points.map(p => Math.log10(Math.max(1, Number(p.build_qty))));
  const ys = points.map(p => Number(p.unit_price));
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMax = Math.max(...ys) * 1.12, yMin = 0;
  const px = i => P.l + ((xs[i] - xMin) / (xMax - xMin || 1)) * (W - P.l - P.r);
  const py = v => H - P.b - ((v - yMin) / (yMax - yMin || 1)) * (H - P.t - P.b);

  const ticks = [0, 0.5, 1].map(f => yMin + f * (yMax - yMin));
  const gridLines = ticks.map(v => `
    <line class="grid-line" x1="${P.l}" x2="${W - P.r}"
          y1="${py(v).toFixed(1)}" y2="${py(v).toFixed(1)}"/>
    <text x="${P.l - 8}" y="${py(v).toFixed(1)}" dy="0.32em"
          text-anchor="end">${money(v, v < 1 ? 4 : 2)}</text>`).join('');

  const path = points.map((p, i) =>
    `${i ? 'L' : 'M'}${px(i).toFixed(1)},${py(ys[i]).toFixed(1)}`).join(' ');
  const markers = points.map((p, i) => `
    <circle cx="${px(i).toFixed(1)}" cy="${py(ys[i]).toFixed(1)}" r="4.5"
            fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="2"/>
    <circle class="hit" cx="${px(i).toFixed(1)}" cy="${py(ys[i]).toFixed(1)}"
            r="14" fill="transparent"
            data-tip="${esc(num(p.build_qty) + ' units · ' + money(p.unit_price) + ' ' + currency + ' each · ' + esc(p.distributor || ''))}"
            data-x="${px(i).toFixed(1)}" data-y="${py(ys[i]).toFixed(1)}"/>`)
    .join('');
  const xLabels = points.map((p, i) => `
    <text x="${px(i).toFixed(1)}" y="${H - P.b + 16}" text-anchor="middle">
      ${num(p.build_qty)}</text>`).join('');

  return `
<div class="chart pricecurve">
  <svg viewBox="0 0 ${W} ${H}" class="axis" role="img"
       aria-label="Unit price against build quantity">
    ${gridLines}
    <line class="baseline" x1="${P.l}" x2="${W - P.r}"
          y1="${H - P.b}" y2="${H - P.b}"/>
    <path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2"
          stroke-linejoin="round" stroke-linecap="round"/>
    ${markers}${xLabels}
    <text x="${W / 2}" y="${H - 2}" text-anchor="middle"
          fill="var(--ink-3)">build quantity (assemblies)</text>
  </svg>
  <div class="tip"></div>
</div>`;
}

function wirePriceCurve(root) {
  $$('.pricecurve', root).forEach(chart => {
    const tip = $('.tip', chart);
    const svg = $('svg', chart);
    $$('.hit', chart).forEach(hit => {
      hit.addEventListener('mouseenter', () => {
        const box = svg.getBoundingClientRect();
        const vb = svg.viewBox.baseVal;
        const scale = box.width / vb.width;
        tip.textContent = hit.dataset.tip;
        tip.style.left = (Number(hit.dataset.x) * scale) + 'px';
        tip.style.top = (Number(hit.dataset.y) * scale) + 'px';
        tip.classList.add('show');
      });
      hit.addEventListener('mouseleave', () => tip.classList.remove('show'));
    });
  });
}

/* ------------------------------------------------- 4. generic table helper */

/**
 * Render a sortable, filterable table.
 * columns: [{ key, label, num, cell(row), sortValue(row), width }]
 */
function renderTable(id, columns, rows, opts = {}) {
  const sort = S.sort[id] || opts.defaultSort || {};
  let data = rows.slice();

  const filterText = (S.filters[id + ':text'] || '').toLowerCase();
  const filterTag = S.filters[id + ':tag'] || '';
  if (filterText) {
    data = data.filter(row => (opts.searchText ? opts.searchText(row) : '')
      .toLowerCase().includes(filterText));
  }
  if (filterTag && opts.tagMatch) {
    data = data.filter(row => opts.tagMatch(row, filterTag));
  }
  if (sort.key) {
    const column = columns.find(c => c.key === sort.key);
    if (column) {
      const get = column.sortValue || (row => {
        const value = row[column.key];
        return value === undefined ? '' : value;
      });
      data.sort((a, b) => {
        const x = get(a), y = get(b);
        if (typeof x === 'number' && typeof y === 'number') return x - y;
        return String(x).localeCompare(String(y), undefined, { numeric: true });
      });
      if (sort.dir === 'desc') data.reverse();
    }
  }

  const head = columns.map(c => {
    const arrow = sort.key === c.key
      ? `<span class="arrow">${sort.dir === 'desc' ? '▼' : '▲'}</span>` : '';
    const style = c.width ? ` style="width:${c.width}"` : '';
    return `<th class="${c.num ? 'num' : ''}" data-sort="${esc(c.key)}"${style}
      title="Sort by ${esc(c.label)}">${esc(c.label)}${arrow}</th>`;
  }).join('');

  const body = data.length ? data.map(row => {
    const selected = opts.isSelected && opts.isSelected(row);
    const cells = columns.map(c => {
      const cls = [c.num ? 'num' : '', c.cls || ''].filter(Boolean).join(' ');
      return `<td class="${cls}">${c.cell(row)}</td>`;
    }).join('');
    return `<tr data-row="${esc(opts.rowId ? opts.rowId(row) : '')}"
      ${selected ? 'aria-selected="true"' : ''}>${cells}</tr>`;
  }).join('') : `<tr><td colspan="${columns.length}" class="empty">${
    esc(opts.emptyText || 'Nothing to show.')}</td></tr>`;

  return {
    html: `<div class="tablewrap"><table id="${esc(id)}">
      <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`,
    shown: data.length,
    total: rows.length,
  };
}

function wireTable(id, root, onRowClick) {
  const table = $('#' + id, root);
  if (!table) return;
  $$('thead th[data-sort]', table).forEach(th => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      const current = S.sort[id] || {};
      S.sort[id] = {
        key,
        dir: current.key === key && current.dir === 'asc' ? 'desc' : 'asc',
      };
      renderCurrentView();
    });
  });
  if (onRowClick) {
    $$('tbody tr[data-row]', table).forEach(tr => {
      if (!tr.dataset.row) return;
      tr.addEventListener('click', () => onRowClick(tr.dataset.row));
    });
  }
}

function toolbar(id, opts = {}) {
  const tags = opts.tags || [];
  const options = ['<option value="">All</option>'].concat(
    tags.map(t => `<option value="${esc(t.value)}">${esc(t.label)}</option>`)
  ).join('');
  return `
<div class="toolbar">
  <input type="search" data-filter="${esc(id)}:text"
         placeholder="${esc(opts.placeholder || 'Filter…')}"
         value="${esc(S.filters[id + ':text'] || '')}">
  ${tags.length ? `<select data-filter="${esc(id)}:tag">${options}</select>` : ''}
  ${opts.extra || ''}
  <span class="count" data-count="${esc(id)}"></span>
</div>`;
}

function wireToolbar(root) {
  $$('[data-filter]', root).forEach(input => {
    const key = input.dataset.filter;
    if (input.tagName === 'SELECT') input.value = S.filters[key] || '';
    let timer = null;
    const handler = () => {
      S.filters[key] = input.value;
      clearTimeout(timer);
      timer = setTimeout(() => {
        renderCurrentView();
        const fresh = $(`[data-filter="${key}"]`);
        if (fresh && fresh.tagName !== 'SELECT') {
          fresh.focus();
          fresh.setSelectionRange(fresh.value.length, fresh.value.length);
        }
      }, input.tagName === 'SELECT' ? 0 : 180);
    };
    input.addEventListener(input.tagName === 'SELECT' ? 'change' : 'input',
                           handler);
  });
}

function setCount(root, id, shown, total) {
  const node = $(`[data-count="${id}"]`, root);
  if (node) {
    node.textContent = shown === total
      ? `${num(total)} rows` : `${num(shown)} of ${num(total)} rows`;
  }
}

/* ----------------------------------------------------------------- 5. views */

function renderCurrentView() {
  const map = {
    dashboard: renderDashboard, bom: renderBom, risk: renderRisk,
    sourcing: renderSourcing, alternates: renderAlternates,
    compliance: renderCompliance, issues: renderIssues,
    mapping: renderMapping,
  };
  if (map[S.view]) map[S.view]();
}

/* ---- start ---- */

function renderStart() {
  const support = (S.boot && S.boot.file_support) || {};
  $('#formats').textContent =
    'Reads ' + (support.extensions || []).join(' · ') +
    (support.legacy_xls ? '' :
      '   (legacy .xls needs the optional xlrd package)');
  const projects = (S.boot && S.boot.projects) || [];
  $('#recent-card').hidden = projects.length === 0;
  if (projects.length) {
    const table = renderTable('recent', [
      { key: 'name', label: 'Name', cell: r => esc(r.name) },
      { key: 'lines', label: 'Lines', num: true,
        cell: r => num((r.summary || {}).lines),
        sortValue: r => Number((r.summary || {}).lines || 0) },
      { key: 'health', label: 'Health', num: true,
        cell: r => (r.summary || {}).health !== undefined
          ? heat((r.summary || {}).health) : '',
        sortValue: r => Number((r.summary || {}).health || 0) },
      { key: 'cost', label: 'Cost', num: true,
        cell: r => money((r.summary || {}).total_cost) + ' ' +
          esc((r.summary || {}).currency || '') },
      { key: 'updated_at', label: 'Saved', cell: r =>
          esc(new Date(r.updated_at * 1000).toLocaleString()),
        sortValue: r => Number(r.updated_at || 0) },
      { key: 'actions', label: '', cell: r =>
          `<button class="btn sm" data-open="${esc(r.id)}">Open</button>` +
          ` <button class="btn sm danger" data-del="${esc(r.id)}">Delete</button>` },
    ], projects, { rowId: r => r.id, defaultSort: { key: 'updated_at', dir: 'desc' } });
    $('#recent-list').innerHTML = table.html;
    wireTable('recent', $('#recent-list'));
    $$('[data-open]', $('#recent-list')).forEach(btn =>
      btn.addEventListener('click', event => {
        event.stopPropagation();
        openProject(btn.dataset.open);
      }));
    $$('[data-del]', $('#recent-list')).forEach(btn =>
      btn.addEventListener('click', async event => {
        event.stopPropagation();
        await api('/api/projects/' + btn.dataset.del, { method: 'DELETE' });
        S.boot.projects = (await api('/api/projects')).projects;
        renderStart();
        toast('Project deleted.');
      }));
  }
}

/* ---- mapping ---- */

function renderMapping() {
  const upload = S.upload;
  if (!upload) { show('start'); return; }
  const mapping = upload.mapping || {};
  const headers = mapping.headers || [];
  const confidence = mapping.confidence || {};
  const reasons = mapping.reasons || {};
  const stats = upload.stats || {};

  const banner = [];
  if (upload.error) {
    banner.push(`<div class="banner error"><span class="ico">!</span><div>
      <b>${esc(upload.error)}</b><br>${esc(upload.hint || '')}</div></div>`);
  }
  const overall = mapping.overall_confidence || 0;
  if (!upload.error) {
    if (overall >= 85) {
      banner.push(`<div class="banner info"><span class="ico">✓</span><div>
        Mapped ${Object.keys(mapping.mapping || {}).length} columns with
        ${num(overall)}% confidence${mapping.from_template
          ? ` using the saved template “${esc(mapping.template_name)}”` : ''}.
        Check it below, then analyse.</div></div>`);
    } else {
      banner.push(`<div class="banner warn"><span class="ico">!</span><div>
        Mapping confidence is ${num(overall)}%. Please confirm each field
        before relying on the results.</div></div>`);
    }
  }
  $('#mapping-banner').innerHTML = banner.join('');

  $('#mapping-sub').innerHTML =
    `${esc(upload.filename || '')} · ${num(stats.lines)} data rows · ` +
    `${headers.length} columns · sheet ` +
    `<b>${esc((upload.chosen_region || {}).sheet || '')}</b>, header row ` +
    `<b>${num((upload.chosen_region || {}).header_row)}</b>`;

  const options = ['<option value="-1">— not mapped —</option>'].concat(
    headers.map((header, index) =>
      `<option value="${index}">${esc(header || '(column ' + (index + 1) + ')')}</option>`)
  ).join('');

  $('#map-rows').innerHTML = (S.fields || []).map(field => {
    const index = mapping.mapping && mapping.mapping[field.key] !== undefined
      ? mapping.mapping[field.key] : -1;
    const conf = confidence[field.key];
    const kind = conf === undefined ? 'soft'
      : conf >= 85 ? 'good' : conf >= 60 ? 'warning' : 'critical';
    return `
<div class="maprow">
  <div>
    <div class="fname">${esc(field.label)}${field.required
      ? '<span class="req" title="Required">*</span>' : ''}</div>
    <div class="fhint">${esc(reasons[field.key] || field.description || '')}</div>
  </div>
  <select data-field="${esc(field.key)}">${options}</select>
  <div class="conf">${conf === undefined ? '<span class="muted">—</span>'
    : pill(num(conf) + '%', kind)}</div>
</div>`;
  }).join('');
  $$('#map-rows select').forEach(select => {
    const field = select.dataset.field;
    const index = mapping.mapping && mapping.mapping[field] !== undefined
      ? mapping.mapping[field] : -1;
    select.value = String(index);
  });

  const preview = upload.preview;
  if (preview) {
    const head = ['<th>Row</th>'].concat(
      (preview.headers || []).map(h => `<th>${esc(h || '—')}</th>`)).join('');
    const body = (preview.rows || []).map(row =>
      `<tr><td class="muted">${num(row.source_row)}</td>` +
      (row.cells || []).map(cell => `<td>${esc(cell)}</td>`).join('') +
      '</tr>').join('');
    $('#map-preview').innerHTML =
      `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  } else {
    $('#map-preview').innerHTML = '<p class="empty">No preview available.</p>';
  }

  const sheets = upload.available_sheets || [];
  $('#sheet-select').innerHTML = sheets.map(sheet =>
    `<option value="${esc(sheet.name)}">${esc(sheet.name)} — ` +
    `${num(sheet.rows)} rows, ${num(sheet.columns)} cols</option>`).join('');
  if ((upload.chosen_region || {}).sheet) {
    $('#sheet-select').value = upload.chosen_region.sheet;
  }
  $('#header-row').value = (upload.chosen_region || {}).header_row || 1;

  const notes = (upload.notes || []).concat(
    ((upload.bom || {}).issues || [])
      .filter(i => i.code === 'ingest_note').map(i => i.message));
  const issues = ((upload.bom || {}).issues || [])
    .filter(i => i.severity !== 'info');
  $('#ingest-notes').innerHTML =
    (notes.length ? '<ul style="margin:0 0 8px;padding-left:18px">' +
      notes.map(n => `<li>${esc(n)}</li>`).join('') + '</ul>' : '') +
    (issues.length ? issues.map(i =>
      `<div style="margin-bottom:5px">${pill(i.severity)} ${esc(i.message)}</div>`)
      .join('') : '') ||
    '<span class="muted">Nothing unusual.</span>';

  const active = (S.providers || []).filter(p => p.active);
  const offline = active.length === 1 && active[0].id === 'mock';
  $('#provider-summary').innerHTML = offline
    ? `Running on the <b>offline catalogue</b> — results will be synthetic
       placeholders. <a href="#" id="link-settings">Add an API key</a> for live
       data.`
    : `Will query <b>${esc(active.map(p => p.name).join(', '))}</b>.`;
  const link = $('#link-settings');
  if (link) link.addEventListener('click', event => {
    event.preventDefault(); openSettings('providers');
  });

  $('#map-build-qty').value = S.settings.build_quantity || 100;
}

/* ---- dashboard ---- */

function renderDashboard() {
  const a = S.analysis;
  if (!a) return;
  const s = a.summary, h = a.health;
  const kind = STATUS_CLASS[h.level] || 'good';
  const cur = s.currency || 'USD';

  const banners = [];
  if (a.offline) {
    banners.push(`<div class="banner warn"><span class="ico">!</span><div>
      <b>Offline mode.</b> Pricing, stock and lifecycle figures are synthetic
      placeholders from the built-in catalogue, not live distributor data.
      <a href="#" data-open-settings="providers">Add an API key</a> to get real
      numbers.</div></div>`);
  }
  (a.warnings || []).forEach(w => {
    if (a.offline && w.startsWith('Running on the offline')) return;
    banners.push(`<div class="banner info"><span class="ico">i</span>
      <div>${esc(w)}</div></div>`);
  });

  const tiles = [
    ['Lines', num(s.total_lines),
     `${num(s.unique_parts)} unique · ${num(s.placements)} placements`],
    ['Matched', `${num(s.matched_lines)}<small>/${num(s.total_lines)}</small>`,
     `${num(s.unmatched_lines)} unmatched · ${num(s.review_lines)} to review`],
    [`Total cost (${cur})`, money(s.total_cost),
     `${num(s.cost_coverage_pct)}% of lines costed`],
    [`Per assembly (${cur})`, money(s.cost_per_unit),
     `build of ${num(s.build_quantity)}`],
    ['Supply flags',
     num(s.out_of_stock_lines + s.single_source_lines + s.long_lead_lines),
     `${num(s.out_of_stock_lines)} no stock · ${num(s.single_source_lines)} single source · ${num(s.long_lead_lines)} long lead`],
    [`Cheapest-source saving (${cur})`, money(s.potential_savings),
     'vs buying every line from its dearest source'],
  ].map(([k, v, n]) => `
    <div class="tile"><div class="k">${esc(k)}</div><div class="v">${v}</div>
    <div class="n">${n}</div></div>`).join('');

  const componentLabels = {
    lifecycle: 'Lifecycle health', availability: 'Availability',
    sourcing: 'Multi-sourcing', lead_time: 'Lead time',
    compliance: 'Compliance', data_quality: 'BOM data quality',
  };
  const components = Object.entries(h.components || {})
    .sort((x, y) => x[1] - y[1]).map(([key, value]) => {
      const level = value >= 75 ? 'good' : value >= 50 ? 'warning'
        : value >= 25 ? 'serious' : 'critical';
      return `
<div class="scorerow">
  <span>${esc(componentLabels[key] || key)}</span>
  <span class="n">${num(value)}</span>
  <span class="track" style="height:7px;border-radius:4px;
    background:var(--surface-3);overflow:hidden">
    <i style="display:block;height:100%;width:${Math.max(2, value)}%;
      border-radius:4px;background:${STATUS_VAR[level]}"></i></span>
</div>`;
    }).join('');

  const lifecycleOrder = ['Active', 'New', 'Unknown', 'NRND', 'EOL',
                          'Obsolete', 'No data'];
  const lifecycleItems = lifecycleOrder.map(label => ({
    label, value: (s.lifecycle_counts || {})[label] || 0,
    kind: STATUS_CLASS[label] || 'neutral',
  }));
  const riskItems = ['Low', 'Medium', 'High', 'Critical'].map(label => ({
    label, value: (s.risk_counts || {})[label] || 0,
    kind: STATUS_CLASS[label],
  }));

  const top = (a.results || [])
    .filter(r => !r.line.dnp && r.risk.score > 0)
    .sort((x, y) => y.risk.score - x.risk.score).slice(0, 12);
  const topTable = renderTable('toprisk', [
    { key: 'line', label: '#', num: true, cell: r => num(r.line.line_no),
      sortValue: r => r.line.line_no },
    { key: 'mpn', label: 'MPN', cls: 'mono', cell: r => esc(r.line.mpn) },
    { key: 'qty', label: 'Qty', num: true, cell: r => num(r.line.quantity),
      sortValue: r => r.line.quantity },
    { key: 'score', label: 'Risk', num: true, cell: r => heat(r.risk.score),
      sortValue: r => r.risk.score },
    { key: 'driver', label: 'Why', cls: 'wrap', cell: r => {
        const worst = (r.risk.factors || []).slice()
          .sort((x, y) => y.score - x.score)[0];
        return worst ? `<b>${esc(worst.label)}</b> — ${esc(worst.detail)}` : '';
      } },
    { key: 'alt', label: 'Best alternate', cls: 'mono',
      cell: r => esc((r.alternates || [])[0] ? r.alternates[0].mpn : '') },
  ], top, { rowId: r => r.line.line_no,
            defaultSort: { key: 'score', dir: 'desc' },
            emptyText: 'No line carries measurable supply risk.' });

  const providerRows = Object.entries(s.provider_stats || {})
    .filter(([key]) => !key.startsWith('_'))
    .map(([key, d]) => `
<div class="scorerow" style="grid-template-columns:1fr auto auto;gap:12px">
  <span>${esc(d.name || key)}${d.skipped
    ? ` <span class="muted">— ${esc(d.skipped)}</span>` : ''}</span>
  <span class="muted">${num(d.calls)} calls · ${num(d.cache_hits)} cached
    ${d.errors ? ` · <span style="color:var(--critical)">${num(d.errors)} errors</span>` : ''}</span>
  <span class="muted">${num(d.avg_latency_ms)} ms</span>
</div>`).join('');

  $('#view-dashboard').innerHTML = `
${banners.join('')}
<div class="card">
  <div class="hero">
    ${donut(h.score, kind)}
    <div style="flex:1;min-width:240px">
      <div class="headline">${esc(h.headline)}</div>
      <div class="meta">Grade <b>${esc(h.grade)}</b> · ${pill(h.level)} ·
        analysed in ${a.duration_ms < 1000 ? num(a.duration_ms) + ' ms'
          : num(a.duration_ms / 1000, 1) + ' s'} ·
        ${esc((a.providers_used || []).join(', '))}</div>
      <div class="drivers">${softPills(h.drivers)}</div>
    </div>
  </div>
</div>

<div class="tiles">${tiles}</div>

<div class="grid two" style="margin-top:14px">
  <div class="card">
    <h2>Health components</h2>
    <p class="sub">0–100, higher is healthier. Weighted by placement count, so
      a part used many times counts for more.</p>
    ${components || '<p class="muted">No components.</p>'}
  </div>
  <div class="card">
    <h2>Lifecycle status</h2>
    <p class="sub">Lines by the worst status any provider reported.</p>
    ${barRows(lifecycleItems, s.total_lines)}
  </div>
</div>

<div class="grid two">
  <div class="card">
    <h2>Risk distribution</h2>
    <p class="sub">Per-line risk bands.</p>
    ${barRows(riskItems, s.total_lines, { keepZero: true })}
  </div>
  <div class="card">
    <h2>Data sources</h2>
    <p class="sub">What answered, how fast.</p>
    ${providerRows || '<p class="muted">No provider statistics.</p>'}
  </div>
</div>

<div class="card">
  <h2>Look at these first</h2>
  <p class="sub">Highest-risk lines, worst first. Click a row for detail.</p>
  ${topTable.html}
</div>`;

  wireTable('toprisk', $('#view-dashboard'), openDrawer);
  $$('[data-open-settings]', $('#view-dashboard')).forEach(link =>
    link.addEventListener('click', event => {
      event.preventDefault();
      openSettings(link.dataset.openSettings);
    }));
}

/* ---- BOM grid ---- */

function renderBom() {
  const a = S.analysis;
  if (!a) return;
  const cur = a.summary.currency || 'USD';
  const rows = a.results || [];

  const columns = [
    { key: 'line', label: '#', num: true, cell: r => num(r.line.line_no),
      sortValue: r => r.line.line_no },
    { key: 'mpn', label: 'MPN', cls: 'mono', cell: r =>
        (esc(r.line.mpn) || '<span class="muted">—</span>') +
        (r.line.dnp ? ' <span class="pill soft">DNP</span>' : ''),
      sortValue: r => r.line.mpn },
    { key: 'mfr', label: 'Manufacturer', cell: r => esc(r.line.manufacturer),
      sortValue: r => r.line.manufacturer },
    { key: 'desc', label: 'Description', cls: 'wrap',
      cell: r => esc(truncate(r.line.description, 60)),
      sortValue: r => r.line.description },
    { key: 'qty', label: 'Qty', num: true, cell: r => num(r.line.quantity),
      sortValue: r => r.line.quantity },
    { key: 'refs', label: 'Refs', cls: 'mono',
      cell: r => esc(truncate(r.line.ref_text, 22)) },
    { key: 'lifecycle', label: 'Lifecycle',
      cell: r => pill(r.part ? r.part.lifecycle : 'No data'),
      sortValue: r => r.part ? r.part.lifecycle : 'zz' },
    { key: 'conf', label: 'Match', num: true, cell: r =>
        r.match.confidence
          ? `<span style="color:${r.match.confidence >= 90 ? 'var(--good)'
              : r.match.confidence >= 60 ? 'var(--warning)' : 'var(--critical)'};
              font-weight:650">${num(r.match.confidence)}%</span>`
          : '<span class="muted">—</span>',
      sortValue: r => r.match.confidence },
    { key: 'stock', label: 'Stock', num: true,
      cell: r => r.part ? num(totalStock(r.part)) : '<span class="muted">—</span>',
      sortValue: r => r.part ? totalStock(r.part) : -1 },
    { key: 'lead', label: 'Lead', num: true, cls: 'nowrap',
      cell: r => esc(leadLabel(r)), sortValue: r => bestLead(r) },
    { key: 'order', label: 'Order', num: true,
      cell: r => {
        if (!r.cost.best) return '';
        const over = r.cost.best.overbuy_qty;
        return num(r.cost.best.order_qty) + (over > 0
          ? ` <span class="muted" title="Minimum order or pack size forces ${
              num(over)} extra pieces">(+${num(over)})</span>` : '');
      },
      sortValue: r => Number(r.cost.best ? r.cost.best.order_qty : 0) },
    { key: 'unit', label: `Unit (${cur})`, num: true,
      cell: r => r.cost.estimated
        ? `<span title="Indicative: an aggregated median price, not a ` +
          `purchasable offer">${money(r.cost.unit_price)}<span class="muted">` +
          ` est</span></span>`
        : money(r.cost.unit_price),
      sortValue: r => Number(r.cost.unit_price || 0) },
    { key: 'ext', label: `Extended (${cur})`, num: true,
      cell: r => money(r.cost.extended),
      sortValue: r => Number(r.cost.extended || 0) },
    { key: 'src', label: 'Source',
      cell: r => r.cost.best ? esc(r.cost.best.distributor)
        : r.cost.estimated ? '<span class="muted">estimate</span>' : '' },
    { key: 'risk', label: 'Risk', num: true, cell: r => heat(r.risk.score),
      sortValue: r => r.risk.score },
    { key: 'flags', label: 'Flags', cls: 'wrap',
      cell: r => `<span class="rowflags">${softPills(r.risk.flags)}</span>` },
  ];

  const table = renderTable('bomtable', columns, rows, {
    rowId: r => r.line.line_no,
    defaultSort: { key: 'line', dir: 'asc' },
    isSelected: r => String(r.line.line_no) === String(S.selectedLine),
    searchText: r => [r.line.mpn, r.line.manufacturer, r.line.description,
                      r.line.ref_text, r.line.internal_pn,
                      (r.risk.flags || []).join(' ')].join(' '),
    tagMatch: (r, tag) => {
      switch (tag) {
        case 'critical': return r.risk.level === 'Critical';
        case 'high': return r.risk.level === 'High' || r.risk.level === 'Critical';
        case 'review': return !!r.match.needs_review;
        case 'unmatched': return r.match.kind === 'none';
        case 'nostock': return !!r.part && !hasStock(r.part);
        case 'risky': return !!r.part &&
          ['NRND', 'EOL', 'Obsolete'].includes(r.part.lifecycle);
        case 'single': return (r.risk.flags || []).includes('Single source');
        case 'dnp': return !!r.line.dnp;
        case 'issues': return (r.issues || []).concat(r.line.issues || [])
          .some(i => i.severity !== 'info');
        default: return true;
      }
    },
  });

  $('#view-bom').innerHTML = `
<div class="card">
  <h2>Enriched BOM</h2>
  <p class="sub">Click any line for the full part record, offers, risk factors
    and alternates. Sort by clicking a header.</p>
  ${toolbar('bomtable', {
    placeholder: 'Filter by part, manufacturer, description, designator…',
    tags: [
      { value: 'critical', label: 'Critical risk' },
      { value: 'high', label: 'High risk and above' },
      { value: 'review', label: 'Match needs review' },
      { value: 'unmatched', label: 'No catalogue data' },
      { value: 'nostock', label: 'No stock' },
      { value: 'risky', label: 'NRND / EOL / obsolete' },
      { value: 'single', label: 'Single source' },
      { value: 'issues', label: 'Has findings' },
      { value: 'dnp', label: 'Do not populate' },
    ],
  })}
  ${table.html}
</div>`;
  wireToolbar($('#view-bom'));
  setCount($('#view-bom'), 'bomtable', table.shown, table.total);
  wireTable('bomtable', $('#view-bom'), openDrawer);
}

/* ---- risk ---- */

function renderRisk() {
  const a = S.analysis;
  if (!a) return;
  const rows = (a.results || []).filter(r => !r.line.dnp);
  const table = renderTable('risktable', [
    { key: 'line', label: '#', num: true, cell: r => num(r.line.line_no),
      sortValue: r => r.line.line_no },
    { key: 'mpn', label: 'MPN', cls: 'mono', cell: r => esc(r.line.mpn) },
    { key: 'mfr', label: 'Manufacturer', cell: r => esc(r.line.manufacturer) },
    { key: 'qty', label: 'Qty', num: true, cell: r => num(r.line.quantity),
      sortValue: r => r.line.quantity },
    { key: 'score', label: 'Score', num: true, cell: r => heat(r.risk.score),
      sortValue: r => r.risk.score },
    { key: 'level', label: 'Level', cell: r => pill(r.risk.level),
      sortValue: r => r.risk.score },
    ...['lifecycle', 'availability', 'sourcing', 'lead_time', 'compliance',
        'data_quality'].map(code => ({
      key: code,
      label: code.replace('_', ' ').replace(/^\w/, c => c.toUpperCase()),
      num: true,
      cell: r => {
        const factor = (r.risk.factors || []).find(f => f.code === code);
        return factor ? heat(factor.score) : '<span class="muted">—</span>';
      },
      sortValue: r => {
        const factor = (r.risk.factors || []).find(f => f.code === code);
        return factor ? factor.score : -1;
      },
    })),
    { key: 'flags', label: 'Flags', cls: 'wrap',
      cell: r => softPills(r.risk.flags) },
  ], rows, {
    rowId: r => r.line.line_no,
    defaultSort: { key: 'score', dir: 'desc' },
    searchText: r => [r.line.mpn, r.line.manufacturer,
                      (r.risk.flags || []).join(' ')].join(' '),
    tagMatch: (r, tag) => r.risk.level === tag,
  });

  $('#view-risk').innerHTML = `
<div class="card">
  <h2>Risk by factor</h2>
  <p class="sub">Each column is one 0–100 factor, higher is worse. The score is
    the weighted blend, with a floor applied when any single factor is
    catastrophic.</p>
  ${toolbar('risktable', {
    placeholder: 'Filter…',
    tags: ['Critical', 'High', 'Medium', 'Low'].map(v => ({ value: v, label: v })),
  })}
  ${table.html}
</div>`;
  wireToolbar($('#view-risk'));
  setCount($('#view-risk'), 'risktable', table.shown, table.total);
  wireTable('risktable', $('#view-risk'), openDrawer);
}

/* ---- sourcing ---- */

function renderSourcing() {
  const a = S.analysis;
  if (!a) return;
  const cur = a.summary.currency || 'USD';
  const rows = [];
  (a.results || []).forEach(result => {
    (result.cost.options || []).forEach((option, index) => {
      rows.push({ result, option, best: index === 0 });
    });
  });

  const table = renderTable('sourcetable', [
    { key: 'line', label: '#', num: true,
      cell: r => r.best ? num(r.result.line.line_no) : '',
      sortValue: r => r.result.line.line_no },
    { key: 'mpn', label: 'MPN', cls: 'mono',
      cell: r => r.best ? esc(r.result.line.mpn) : '',
      sortValue: r => r.result.line.mpn },
    { key: 'need', label: 'Need', num: true,
      cell: r => r.best ? num(r.result.cost.required_qty) : '',
      sortValue: r => r.result.cost.required_qty },
    { key: 'dist', label: 'Distributor', cell: r =>
        (r.best ? '<b>' : '') + esc(r.option.distributor) +
        (r.best ? '</b>' : ''),
      sortValue: r => r.option.distributor },
    { key: 'sku', label: 'SKU', cls: 'mono', cell: r => esc(r.option.sku) },
    { key: 'stock', label: 'Stock', num: true, cell: r => num(r.option.stock),
      sortValue: r => Number(r.option.stock || 0) },
    { key: 'lead', label: 'Lead', num: true, cls: 'nowrap',
      cell: r => esc(leadText(r.option.lead_time_days)),
      sortValue: r => Number(r.option.lead_time_days ?? 9999) },
    { key: 'moq', label: 'MOQ', num: true, cell: r => num(r.option.moq),
      sortValue: r => Number(r.option.moq || 0) },
    { key: 'spq', label: 'Pack', num: true, cell: r => num(r.option.spq),
      sortValue: r => Number(r.option.spq || 0) },
    { key: 'order', label: 'Order', num: true,
      cell: r => num(r.option.order_qty),
      sortValue: r => Number(r.option.order_qty || 0) },
    { key: 'unit', label: `Unit (${cur})`, num: true,
      cell: r => money(r.option.unit_price),
      sortValue: r => Number(r.option.unit_price || 0) },
    { key: 'ext', label: `Extended (${cur})`, num: true,
      cell: r => money(r.option.extended),
      sortValue: r => Number(r.option.extended || 0) },
    { key: 'notes', label: 'Notes', cls: 'wrap',
      cell: r => esc((r.option.notes || []).join('; ')) },
    { key: 'buy', label: '', cell: r => r.option.url
        ? `<a href="${esc(r.option.url)}" target="_blank" rel="noopener">Buy ↗</a>`
        : '' },
  ], rows, {
    rowId: r => r.result.line.line_no,
    defaultSort: { key: 'line', dir: 'asc' },
    searchText: r => [r.result.line.mpn, r.option.distributor, r.option.sku]
      .join(' '),
    tagMatch: (r, tag) => tag === 'best' ? r.best
      : tag === 'overbuy' ? r.option.overbuy_qty > 0
      : tag === 'instock' ? Number(r.option.stock || 0) > 0 : true,
    emptyText: 'No purchasable offers were returned.',
  });

  const uncosted = (a.results || [])
    .filter(r => !r.line.dnp && !r.cost.extended);

  $('#view-sourcing').innerHTML = `
<div class="card">
  <h2>Sourcing options</h2>
  <p class="sub">Every offer, costed for this build. The bold row per line is
    the cheapest option that can actually supply the quantity, with minimum
    order and pack size taken into account.</p>
  ${toolbar('sourcetable', {
    placeholder: 'Filter by part or distributor…',
    tags: [
      { value: 'best', label: 'Chosen option only' },
      { value: 'instock', label: 'In stock only' },
      { value: 'overbuy', label: 'Forces an overbuy' },
    ],
  })}
  ${table.html}
</div>
${uncosted.length ? `
<div class="card">
  <h2>${num(uncosted.length)} line(s) could not be costed</h2>
  <p class="sub">These are excluded from the BOM total.</p>
  ${renderTable('uncosted', [
    { key: 'line', label: '#', num: true, cell: r => num(r.line.line_no) },
    { key: 'mpn', label: 'MPN', cls: 'mono', cell: r => esc(r.line.mpn) },
    { key: 'why', label: 'Reason', cls: 'wrap', cell: r =>
        esc((r.cost.notes || []).join('; ') ||
            'No offer or price was available.') },
  ], uncosted, { rowId: r => r.line.line_no }).html}
</div>` : ''}`;
  wireToolbar($('#view-sourcing'));
  setCount($('#view-sourcing'), 'sourcetable', table.shown, table.total);
  wireTable('sourcetable', $('#view-sourcing'), openDrawer);
  wireTable('uncosted', $('#view-sourcing'), openDrawer);
}

/* ---- alternates ---- */

function renderAlternates() {
  const a = S.analysis;
  if (!a) return;
  const cur = a.summary.currency || 'USD';
  const rows = [];
  (a.results || []).forEach(result => {
    (result.alternates || []).forEach(alt => rows.push({ result, alt }));
  });

  const table = renderTable('alttable', [
    { key: 'line', label: '#', num: true, cell: r => num(r.result.line.line_no),
      sortValue: r => r.result.line.line_no },
    { key: 'orig', label: 'Original', cls: 'mono',
      cell: r => esc(r.result.line.mpn) },
    { key: 'origstatus', label: 'Was',
      cell: r => pill(r.result.part ? r.result.part.lifecycle : 'No data') },
    { key: 'mpn', label: 'Alternate', cls: 'mono',
      cell: r => `<b>${esc(r.alt.mpn)}</b>`, sortValue: r => r.alt.mpn },
    { key: 'mfr', label: 'Manufacturer', cell: r => esc(r.alt.manufacturer) },
    { key: 'score', label: 'Score', num: true, cell: r =>
        `<span class="heat" style="background:color-mix(in srgb,${
          r.alt.score >= 75 ? 'var(--good)' : r.alt.score >= 55
            ? 'var(--warning)' : 'var(--critical)'} 18%, transparent);
          color:${r.alt.score >= 75 ? 'var(--good)' : r.alt.score >= 55
            ? 'var(--warning)' : 'var(--critical)'}">${num(r.alt.score)}</span>`,
      sortValue: r => r.alt.score },
    { key: 'dropin', label: 'Drop-in', cell: r => r.alt.pin_compatible === true
        ? pill('Yes', 'good') : r.alt.pin_compatible === false
        ? pill('No', 'critical') : pill('Check', 'warning') },
    { key: 'lifecycle', label: 'Lifecycle', cell: r => pill(r.alt.lifecycle) },
    { key: 'stock', label: 'Stock', num: true, cell: r => num(r.alt.stock),
      sortValue: r => Number(r.alt.stock || 0) },
    { key: 'unit', label: `Unit (${cur})`, num: true,
      cell: r => money(r.alt.unit_price),
      sortValue: r => Number(r.alt.unit_price || 0) },
    { key: 'delta', label: 'Δ price', num: true, cell: r =>
        r.alt.price_delta_pct === null || r.alt.price_delta_pct === undefined
          ? '' : `<span style="color:${r.alt.price_delta_pct <= 0
              ? 'var(--good)' : 'var(--ink-2)'}">${
              r.alt.price_delta_pct > 0 ? '+' : ''}${num(r.alt.price_delta_pct, 0)}%</span>`,
      sortValue: r => Number(r.alt.price_delta_pct ?? 0) },
    { key: 'why', label: 'Why it fits', cls: 'wrap',
      cell: r => esc((r.alt.reasons || []).join('; ')) },
    { key: 'concerns', label: 'Concerns', cls: 'wrap', cell: r =>
        (r.alt.concerns || []).length
          ? `<span style="color:var(--serious)">${esc(r.alt.concerns.join('; '))}</span>`
          : '<span class="muted">none</span>' },
  ], rows, {
    rowId: r => r.result.line.line_no,
    defaultSort: { key: 'score', dir: 'desc' },
    searchText: r => [r.result.line.mpn, r.alt.mpn, r.alt.manufacturer,
                      r.alt.description].join(' '),
    tagMatch: (r, tag) => tag === 'dropin' ? r.alt.pin_compatible === true
      : tag === 'good' ? r.alt.score >= 70
      : tag === 'source' ? r.alt.source === 'bom' : true,
    emptyText: 'No alternates were needed: every populated line is active, ' +
               'stocked and multi-sourced.',
  });

  $('#view-alternates').innerHTML = `
<div class="card">
  <h2>Alternates</h2>
  <p class="sub">Scored out of 100 on lifecycle, availability, form/fit,
    compliance and price. Alternates already approved in your BOM start with a
    bonus; a different footprint is a heavy penalty.</p>
  ${toolbar('alttable', {
    placeholder: 'Filter…',
    tags: [
      { value: 'dropin', label: 'Drop-in only' },
      { value: 'good', label: 'Score 70+' },
      { value: 'source', label: 'From your BOM' },
    ],
  })}
  ${table.html}
</div>`;
  wireToolbar($('#view-alternates'));
  setCount($('#view-alternates'), 'alttable', table.shown, table.total);
  wireTable('alttable', $('#view-alternates'), openDrawer);
}

/* ---- compliance ---- */

function renderCompliance() {
  const a = S.analysis;
  if (!a) return;
  const rows = a.results || [];
  const rules = a.summary.rules || {};

  const counts = { Compliant: 0, Exempt: 0, 'Non-compliant': 0, Unknown: 0 };
  rows.forEach(r => {
    if (r.line.dnp) return;
    counts[r.compliance.rohs] = (counts[r.compliance.rohs] || 0) + 1;
  });
  const live = rows.filter(r => !r.line.dnp).length;

  const table = renderTable('comptable', [
    { key: 'line', label: '#', num: true, cell: r => num(r.line.line_no),
      sortValue: r => r.line.line_no },
    { key: 'mpn', label: 'MPN', cls: 'mono', cell: r => esc(r.line.mpn) },
    { key: 'mfr', label: 'Manufacturer', cell: r => esc(r.line.manufacturer) },
    { key: 'rohs', label: 'RoHS', cell: r => pill(r.compliance.rohs),
      sortValue: r => r.compliance.rohs },
    { key: 'rohsnote', label: 'RoHS note', cls: 'wrap',
      cell: r => esc(r.compliance.rohs_note) },
    { key: 'reach', label: 'REACH', cell: r => pill(r.compliance.reach),
      sortValue: r => r.compliance.reach },
    { key: 'svhc', label: 'SVHC', cls: 'wrap',
      cell: r => esc((r.compliance.svhc || []).join('; ')) },
    { key: 'msl', label: 'MSL', num: true, cell: r => esc(r.compliance.msl) },
    { key: 'coo', label: 'Origin', cell: r =>
        esc(r.compliance.country_of_origin) },
    { key: 'hts', label: 'HTS', cls: 'mono',
      cell: r => esc(r.compliance.hts_code) },
    { key: 'eccn', label: 'ECCN', cls: 'mono',
      cell: r => esc(r.compliance.eccn) },
    { key: 'export', label: 'Export', cell: r => r.compliance.itar
        ? pill('ITAR', 'critical') : r.compliance.export_controlled
        ? pill('Controlled', 'warning') : '<span class="muted">—</span>',
      sortValue: r => r.compliance.itar ? 2
        : r.compliance.export_controlled ? 1 : 0 },
  ], rows, {
    rowId: r => r.line.line_no,
    defaultSort: { key: 'line', dir: 'asc' },
    searchText: r => [r.line.mpn, r.line.manufacturer,
                      r.compliance.country_of_origin, r.compliance.hts_code,
                      r.compliance.eccn].join(' '),
    tagMatch: (r, tag) => tag === 'noncompliant'
        ? r.compliance.rohs === 'Non-compliant'
      : tag === 'unknown' ? r.compliance.rohs === 'Unknown'
      : tag === 'svhc' ? r.compliance.reach === 'Non-compliant'
      : tag === 'export' ? (r.compliance.export_controlled || r.compliance.itar)
      : true,
  });

  $('#view-compliance').innerHTML = `
<div class="grid two">
  <div class="card">
    <h2>RoHS status</h2>
    <p class="sub">“Unknown” means no declaration was found — not that the part
      is compliant.</p>
    ${barRows(['Compliant', 'Exempt', 'Non-compliant', 'Unknown'].map(label => ({
      label, value: counts[label] || 0, kind: STATUS_CLASS[label],
    })), live, { keepZero: true })}
  </div>
  <div class="card">
    <h2>Rules applied</h2>
    <p class="sub">Change these in Settings → Analysis.</p>
    <div class="scorerow" style="grid-template-columns:1fr auto">
      <span>RoHS required</span>${pill(rules.require_rohs ? 'Yes' : 'No',
        rules.require_rohs ? 'good' : 'neutral')}</div>
    <div class="scorerow" style="grid-template-columns:1fr auto">
      <span>REACH required</span>${pill(rules.require_reach ? 'Yes' : 'No',
        rules.require_reach ? 'good' : 'neutral')}</div>
    <div class="scorerow" style="grid-template-columns:1fr auto">
      <span>Export control flagged</span>${pill(
        rules.flag_export_controlled ? 'Yes' : 'No',
        rules.flag_export_controlled ? 'good' : 'neutral')}</div>
  </div>
</div>
<div class="card">
  <h2>Per-line compliance</h2>
  <p class="sub">As supplied by the distributors queried. Confirm against the
    manufacturer's declaration before shipping.</p>
  ${toolbar('comptable', {
    placeholder: 'Filter…',
    tags: [
      { value: 'noncompliant', label: 'Not RoHS compliant' },
      { value: 'unknown', label: 'No RoHS declaration' },
      { value: 'svhc', label: 'REACH SVHC' },
      { value: 'export', label: 'Export controlled' },
    ],
  })}
  ${table.html}
</div>`;
  wireToolbar($('#view-compliance'));
  setCount($('#view-compliance'), 'comptable', table.shown, table.total);
  wireTable('comptable', $('#view-compliance'), openDrawer);
}

/* ---- findings ---- */

function renderIssues() {
  const a = S.analysis;
  if (!a) return;
  const rows = [];
  (a.issues || []).forEach(issue => rows.push({ issue, scope: 'BOM', line: null }));
  (a.results || []).forEach(result => {
    (result.line.issues || []).concat(result.issues || []).forEach(issue =>
      rows.push({ issue, scope: 'Line', line: result.line }));
  });
  const rank = { error: 2, warning: 1, info: 0 };
  rows.sort((x, y) => (rank[y.issue.severity] || 0) - (rank[x.issue.severity] || 0));

  const counts = { error: 0, warning: 0, info: 0 };
  rows.forEach(r => { counts[r.issue.severity] = (counts[r.issue.severity] || 0) + 1; });

  const table = renderTable('issuetable', [
    { key: 'sev', label: 'Severity', cell: r => pill(r.issue.severity),
      sortValue: r => rank[r.issue.severity] || 0 },
    { key: 'line', label: 'Line', num: true,
      cell: r => r.line ? num(r.line.line_no) : '<span class="muted">—</span>',
      sortValue: r => r.line ? r.line.line_no : 0 },
    { key: 'mpn', label: 'MPN', cls: 'mono',
      cell: r => esc(r.line ? r.line.mpn : '') },
    { key: 'msg', label: 'Finding', cls: 'wrap',
      cell: r => esc(r.issue.message) },
    { key: 'fix', label: 'Suggested action', cls: 'wrap',
      cell: r => `<span class="muted">${esc(r.issue.suggestion || '')}</span>` },
    { key: 'code', label: 'Code', cell: r =>
        `<span class="muted">${esc(r.issue.code)}</span>` },
  ], rows, {
    rowId: r => r.line ? r.line.line_no : '',
    searchText: r => [r.issue.message, r.issue.code, r.issue.suggestion,
                      r.line ? r.line.mpn : ''].join(' '),
    tagMatch: (r, tag) => r.issue.severity === tag,
    emptyText: 'No findings — the BOM passed every check.',
  });

  $('#view-issues').innerHTML = `
<div class="card">
  <h2>Findings</h2>
  <p class="sub">
    ${pill('error')} ${num(counts.error)} &nbsp;
    ${pill('warning')} ${num(counts.warning)} &nbsp;
    ${pill('info')} ${num(counts.info)}
  </p>
  ${toolbar('issuetable', {
    placeholder: 'Filter findings…',
    tags: [{ value: 'error', label: 'Errors' },
           { value: 'warning', label: 'Warnings' },
           { value: 'info', label: 'Info' }],
  })}
  ${table.html}
</div>`;
  wireToolbar($('#view-issues'));
  setCount($('#view-issues'), 'issuetable', table.shown, table.total);
  wireTable('issuetable', $('#view-issues'), line => {
    if (line) openDrawer(line);
  });
}

/* ---- part search ---- */

async function runSearch() {
  const query = $('#search-input').value.trim();
  if (!query) return;
  const target = $('#search-results');
  target.innerHTML = '<p class="empty"><span class="loading"></span> Searching…</p>';
  try {
    const data = await api('/api/search', { json: { query, limit: 20 } });
    if (!data.parts.length) {
      target.innerHTML = '<p class="empty">Nothing found for that query.</p>';
      return;
    }
    target.innerHTML = data.parts.map(part => `
<div class="altcard">
  <div class="top">
    <div>
      <div class="mpn">${esc(part.mpn)}</div>
      <div class="muted" style="font-size:12px">${esc(part.manufacturer)}
        ${part.category ? ' · ' + esc(part.category) : ''}</div>
      <div style="font-size:12.5px;margin-top:4px">${esc(part.description)}</div>
    </div>
    <div style="text-align:right;white-space:nowrap">
      ${pill(part.lifecycle)}<br>
      <span class="muted" style="font-size:12px">
        ${num(totalStock(part))} in stock · ${num((part.offers || []).length)} offers</span>
    </div>
  </div>
  <div class="inline" style="margin-top:9px">
    ${part.datasheet_url ? `<a href="${esc(part.datasheet_url)}"
      target="_blank" rel="noopener">Datasheet ↗</a>` : ''}
    ${part.product_url ? `<a href="${esc(part.product_url)}"
      target="_blank" rel="noopener">Product page ↗</a>` : ''}
  </div>
</div>`).join('');
  } catch (err) {
    target.innerHTML = `<div class="banner error"><span class="ico">!</span>
      <div>${esc(err.message)}</div></div>`;
  }
}

/* ---------------------------------------------------------------- 6. drawer */

function openDrawer(lineNo) {
  const a = S.analysis;
  if (!a) return;
  const result = (a.results || [])
    .find(r => String(r.line.line_no) === String(lineNo));
  if (!result) return;
  S.selectedLine = result.line.line_no;
  const cur = a.summary.currency || 'USD';
  const line = result.line, part = result.part;

  $('#drawer-title').textContent = line.mpn || line.internal_pn || `Line ${line.line_no}`;
  $('#drawer-sub').innerHTML =
    `${esc(line.manufacturer || (part ? part.manufacturer : ''))} · line
     ${num(line.line_no)}${line.source_row
       ? ` (source row ${num(line.source_row)})` : ''} · qty
     ${num(line.quantity)}${line.dnp ? ' · do not populate' : ''}`;

  const sections = [];

  /* status strip */
  sections.push(`
<section>
  <div class="inline">
    ${pill(result.risk.level + ' risk')}
    ${part ? pill(part.lifecycle) : pill('No data', 'neutral')}
    ${pill(`match ${num(result.match.confidence)}%`,
      result.match.confidence >= 90 ? 'good'
      : result.match.confidence >= 60 ? 'warning' : 'critical')}
    ${softPills(result.risk.flags)}
  </div>
</section>`);

  /* match reasoning */
  sections.push(`
<section>
  <h3>Match</h3>
  <dl class="kv">
    <dt>Matched to</dt><dd class="mono">${esc(result.match.matched_mpn) || '—'}</dd>
    <dt>Manufacturer</dt><dd>${esc(result.match.matched_manufacturer) || '—'}</dd>
    <dt>Method</dt><dd>${esc(result.match.kind)}</dd>
    <dt>Providers</dt><dd>${esc(result.match.provider) || '—'}</dd>
  </dl>
  ${(result.match.reasons || []).length ? `<ul style="margin:8px 0 0;
    padding-left:18px;font-size:12px;color:var(--ink-2)">${
    result.match.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
</section>`);

  /* risk factors */
  if ((result.risk.factors || []).length) {
    sections.push(`
<section>
  <h3>Risk factors</h3>
  ${result.risk.factors.slice().sort((x, y) => y.score - x.score).map(f => {
    const kind = f.score >= 75 ? 'critical' : f.score >= 50 ? 'serious'
      : f.score >= 25 ? 'warning' : 'good';
    return `
  <div class="factor">
    <div class="head"><b>${esc(f.label)}</b>
      <span style="color:${STATUS_VAR[kind]};font-weight:650;
        font-variant-numeric:tabular-nums">${num(f.score)}</span></div>
    <div class="detail">${esc(f.detail)}</div>
    <div class="track"><i style="width:${Math.max(2, f.score)}%;
      background:${STATUS_VAR[kind]}"></i></div>
  </div>`;
  }).join('')}
  <p class="muted" style="font-size:11.5px;margin:8px 0 0">
    Weighted blend: <b>${num(result.risk.score, 1)}</b> / 100.</p>
</section>`);
  }

  /* cost */
  const cost = result.cost;
  if (cost.best || (cost.options || []).length) {
    sections.push(`
<section>
  <h3>Cost for this build</h3>
  <dl class="kv">
    <dt>Quantity needed</dt><dd>${num(cost.required_qty)}</dd>
    <dt>Unit price</dt><dd>${money(cost.unit_price)} ${esc(cur)}</dd>
    <dt>Extended</dt><dd><b>${money(cost.extended)} ${esc(cur)}</b></dd>
    ${cost.savings_vs_worst ? `<dt>Cheapest-source saving</dt>
      <dd>${money(cost.savings_vs_worst)} ${esc(cur)}
      <span class="muted">vs the dearest offer, compared per piece</span></dd>`
      : ''}
    ${cost.price_spread_pct ? `<dt>Price spread</dt>
      <dd>${num(cost.price_spread_pct)}% between distributors</dd>` : ''}
    ${cost.estimated ? `<dt>Basis</dt><dd>${pill('estimate', 'warning')}
      aggregated median price — no purchasable offer was found</dd>` : ''}
  </dl>
  ${(cost.notes || []).length ? `<p class="muted" style="font-size:12px;
    margin:8px 0 0">${esc(cost.notes.join(' '))}</p>` : ''}
  ${(cost.options || []).length ? `
  <div class="tablewrap" style="margin-top:10px;max-height:none">
    <table><thead><tr><th>Distributor</th><th class="num">Stock</th>
    <th class="num">Order</th><th class="num">Unit</th><th class="num">Total</th>
    <th></th></tr></thead><tbody>
    ${cost.options.map((o, i) => `<tr>
      <td>${i === 0 ? '<b>' : ''}${esc(o.distributor)}${i === 0 ? '</b>' : ''}
        ${(o.notes || []).length ? `<div class="muted"
          style="font-size:11px">${esc(o.notes.join('; '))}</div>` : ''}</td>
      <td class="num">${num(o.stock)}</td>
      <td class="num">${num(o.order_qty)}</td>
      <td class="num">${money(o.unit_price)}</td>
      <td class="num">${money(o.extended)}</td>
      <td>${o.url ? `<a href="${esc(o.url)}" target="_blank"
        rel="noopener">↗</a>` : ''}</td></tr>`).join('')}
    </tbody></table></div>` : ''}
  ${priceCurve(cost.price_curve, cur)}
</section>`);
  }

  /* part record */
  if (part) {
    const specs = Object.entries(part.specs || {}).slice(0, 40);
    sections.push(`
<section>
  <h3>Catalogue record</h3>
  <dl class="kv">
    <dt>Description</dt><dd>${esc(part.description)}</dd>
    <dt>Category</dt><dd>${esc(part.category) || '—'}</dd>
    <dt>Package</dt><dd>${esc(part.package) || '—'}
      ${part.mount ? ` (${esc(part.mount)})` : ''}</dd>
    <dt>Lifecycle</dt><dd>${pill(part.lifecycle)}
      ${esc(part.lifecycle_note)}</dd>
    <dt>Total stock</dt><dd>${num(totalStock(part))} across
      ${num(new Set((part.offers || []).map(o => o.distributor)).size)} distributors</dd>
    ${part.estimated_factory_lead_days ? `<dt>Factory lead</dt>
      <dd>${num(part.estimated_factory_lead_days)} days</dd>` : ''}
    ${part.median_price_1k ? `<dt>Median price @1k</dt>
      <dd>${money(part.median_price_1k)} USD</dd>` : ''}
  </dl>
  <div class="inline" style="margin-top:9px">
    ${part.datasheet_url ? `<a href="${esc(part.datasheet_url)}"
      target="_blank" rel="noopener">Datasheet ↗</a>` : ''}
    ${part.product_url ? `<a href="${esc(part.product_url)}"
      target="_blank" rel="noopener">Product page ↗</a>` : ''}
  </div>
  ${specs.length ? `<details style="margin-top:10px">
    <summary>Parametrics (${specs.length})</summary>
    <dl class="kv" style="margin-top:8px">${specs.map(([k, v]) =>
      `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl></details>` : ''}
</section>`);
  }

  /* compliance */
  const compliance = result.compliance;
  sections.push(`
<section>
  <h3>Compliance</h3>
  <dl class="kv">
    <dt>RoHS</dt><dd>${pill(compliance.rohs)} ${esc(compliance.rohs_note)}</dd>
    <dt>REACH</dt><dd>${pill(compliance.reach)}
      ${esc((compliance.svhc || []).join('; '))}</dd>
    <dt>Halogen free</dt><dd>${pill(compliance.halogen_free)}</dd>
    <dt>Country of origin</dt><dd>${esc(compliance.country_of_origin) || '—'}</dd>
    <dt>HTS code</dt><dd class="mono">${esc(compliance.hts_code) || '—'}</dd>
    <dt>ECCN</dt><dd class="mono">${esc(compliance.eccn) || '—'}</dd>
    <dt>Export controlled</dt><dd>${compliance.itar ? pill('ITAR', 'critical')
      : compliance.export_controlled ? pill('Yes', 'warning') : 'No'}</dd>
    <dt>MSL</dt><dd>${esc(compliance.msl) || '—'}</dd>
    ${compliance.aec_q ? `<dt>Automotive</dt><dd>${esc(compliance.aec_q)}</dd>` : ''}
  </dl>
</section>`);

  /* alternates */
  sections.push(`
<section>
  <h3>Alternates</h3>
  <div id="drawer-alternates">
  ${(result.alternates || []).length ? result.alternates.map(alt => `
  <div class="altcard">
    <div class="top">
      <div>
        <div class="mpn">${esc(alt.mpn)}</div>
        <div class="muted" style="font-size:12px">${esc(alt.manufacturer)}</div>
        <div style="font-size:12px;margin-top:3px">${esc(alt.description)}</div>
      </div>
      <div style="text-align:right;white-space:nowrap">
        <b style="font-size:15px">${num(alt.score)}</b>
        <span class="muted">/100</span><br>
        ${alt.pin_compatible === true ? pill('Drop-in', 'good')
          : alt.pin_compatible === false ? pill('Not drop-in', 'critical')
          : pill('Check fit', 'warning')}
      </div>
    </div>
    <div class="inline" style="margin-top:7px;font-size:12px">
      ${pill(alt.lifecycle)}
      <span class="muted">${num(alt.stock)} in stock</span>
      ${alt.unit_price ? `<span class="muted">${money(alt.unit_price)}
        ${esc(cur)}${alt.price_delta_pct !== null &&
          alt.price_delta_pct !== undefined
          ? ` (${alt.price_delta_pct > 0 ? '+' : ''}${num(alt.price_delta_pct, 0)}%)`
          : ''}</span>` : ''}
    </div>
    ${(alt.reasons || []).length ? `<ul>${alt.reasons.map(r =>
      `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
    ${(alt.concerns || []).length ? `<ul class="concerns">${alt.concerns.map(c =>
      `<li>${esc(c)}</li>`).join('')}</ul>` : ''}
  </div>`).join('') : '<p class="muted">None suggested for this line.</p>'}
  </div>
  <button class="btn sm" id="btn-find-alternates" style="margin-top:8px">
    Search for more alternates</button>
</section>`);

  /* findings */
  const issues = (line.issues || []).concat(result.issues || []);
  if (issues.length) {
    sections.push(`
<section>
  <h3>Findings for this line</h3>
  ${issues.map(issue => `
  <div class="factor">
    <div class="head">${pill(issue.severity)}
      <span class="muted" style="font-size:11px">${esc(issue.code)}</span></div>
    <div class="detail">${esc(issue.message)}</div>
    ${issue.suggestion ? `<div class="detail muted">${esc(issue.suggestion)}</div>`
      : ''}
  </div>`).join('')}
</section>`);
  }

  /* original row */
  const raw = Object.entries(line.raw || {});
  if (raw.length) {
    sections.push(`
<section>
  <h3>Original row</h3>
  <details><summary>${raw.length} source columns</summary>
    <dl class="kv" style="margin-top:8px">${raw.map(([k, v]) =>
      `<dt>${esc(k)}</dt><dd>${esc(v) || '<span class="muted">—</span>'}</dd>`)
      .join('')}</dl></details>
</section>`);
  }

  /* provider errors */
  const errors = Object.entries(result.provider_errors || {});
  if (errors.length) {
    sections.push(`
<section>
  <h3>Provider problems</h3>
  ${errors.map(([provider, message]) => `<div class="banner warn"
    style="margin-bottom:7px"><span class="ico">!</span>
    <div><b>${esc(provider)}</b> — ${esc(message)}</div></div>`).join('')}
</section>`);
  }

  $('#drawer-body').innerHTML = sections.join('');
  wirePriceCurve($('#drawer-body'));
  const findBtn = $('#btn-find-alternates');
  if (findBtn) {
    findBtn.addEventListener('click', () => findMoreAlternates(result));
  }
  $('#drawer').classList.add('open');
  $('#drawer').setAttribute('aria-hidden', 'false');
  renderCurrentView();
}

async function findMoreAlternates(result) {
  const button = $('#btn-find-alternates');
  button.disabled = true;
  button.innerHTML = '<span class="loading"></span> Searching…';
  try {
    const data = await api('/api/alternates', {
      json: {
        mpn: result.line.mpn,
        manufacturer: result.line.manufacturer,
        package: result.line.package,
        value: result.line.value,
        quantity: result.cost.required_qty,
      },
    });
    if (!data.found || !data.alternates.length) {
      toast('No further alternates were found.');
    } else {
      result.alternates = data.alternates;
      openDrawer(result.line.line_no);
      toast(`Found ${data.alternates.length} alternate(s).`, 'success');
    }
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = 'Search for more alternates';
    }
  }
}

function closeDrawer() {
  $('#drawer').classList.remove('open');
  $('#drawer').setAttribute('aria-hidden', 'true');
  S.selectedLine = null;
  renderCurrentView();
}

/* ------------------------------------------------------ small data helpers */

function totalStock(part) {
  return (part.offers || []).reduce((sum, o) => sum + (Number(o.stock) || 0), 0);
}
function hasStock(part) {
  return (part.offers || []).some(o => Number(o.stock) > 0);
}
function bestLead(result) {
  if (!result.part) return 9999;
  const values = (result.part.offers || [])
    .map(o => o.lead_time_days).filter(v => v !== null && v !== undefined);
  return values.length ? Math.min(...values) : 9999;
}
function leadLabel(result) {
  const days = bestLead(result);
  return days === 9999 ? '' : leadText(days);
}
function leadText(days) {
  if (days === null || days === undefined) return '';
  if (days <= 0) return 'stock';
  if (days < 14) return days + ' d';
  if (days < 120) return Math.round(days / 7) + ' wk';
  return Math.round(days / 30) + ' mo';
}
function truncate(text, limit) {
  text = String(text || '');
  return text.length <= limit ? text : text.slice(0, limit - 1) + '…';
}

/* ------------------------------------------------------ 7. settings/export */

const ANALYSIS_FIELDS = [
  ['build_quantity', 'Build quantity', 'number',
   'How many assemblies you intend to build. Drives stock checks and price breaks.'],
  ['currency', 'Currency', 'currency', 'All prices are converted to this.'],
  ['target_region', 'Sourcing region', 'text',
   'Used to pick regional catalogues, e.g. global, emea, apac, india, americas.'],
  ['lead_time_warn_days', 'Lead-time warning (days)', 'number',
   'Out-of-stock parts at or above this are flagged.'],
  ['lead_time_critical_days', 'Lead-time critical (days)', 'number', ''],
  ['stock_buffer_pct', 'Stock buffer (%)', 'number',
   'Extra headroom required on top of the build quantity.'],
  ['min_sources_ok', 'Target stocking sources', 'number',
   'Below this, a line is flagged as thinly sourced.'],
  ['fuzzy_match_threshold', 'Fuzzy match threshold', 'number',
   'Similarity (0-100) below which a part number is treated as no match.'],
  ['review_confidence_threshold', 'Review threshold', 'number',
   'Matches below this confidence are flagged for review.'],
  ['max_alternates_per_line', 'Alternates per line', 'number', ''],
  ['prefer_authorized_only', 'Authorised distributors only', 'bool',
   'Ignore brokers and the grey market when costing.'],
  ['include_non_stocking_offers', 'Include non-stocking offers', 'bool',
   'Cost against listed-but-not-stocked offers when nothing is in stock.'],
  ['auto_complete_fields', 'Suggest field completions', 'bool',
   'Fill blank manufacturer, description, package and value from catalogue data.'],
  ['auto_apply_completions', 'Apply completions automatically', 'bool',
   'Write suggested values straight onto the BOM lines.'],
  ['require_rohs', 'RoHS required', 'bool', ''],
  ['require_reach', 'REACH required', 'bool', ''],
  ['flag_export_controlled', 'Flag export-controlled parts', 'bool', ''],
];

const WEIGHT_FIELDS = [
  ['weight_lifecycle', 'Lifecycle'], ['weight_availability', 'Availability'],
  ['weight_sourcing', 'Multi-sourcing'], ['weight_lead_time', 'Lead time'],
  ['weight_compliance', 'Compliance'], ['weight_data_quality', 'Data quality'],
  ['weight_cost', 'Cost certainty'],
];

const INGEST_FIELDS = [
  ['multi_sheet', 'Read every sheet', 'bool', ''],
  ['merge_duplicate_mpns', 'Merge duplicate part numbers', 'bool', ''],
  ['expand_ref_ranges', 'Expand designator ranges', 'bool', ''],
  ['treat_blank_qty_as_one', 'Treat a blank quantity as 1', 'bool', ''],
  ['learn_templates', 'Remember column layouts', 'bool',
   'Save each confirmed mapping so the same export maps itself next time.'],
  ['header_scan_rows', 'Rows to scan for the header', 'number', ''],
  ['max_workers', 'Concurrent lookups', 'number',
   'More is faster, but distributor rate limits apply.'],
  ['cache_enabled', 'Cache provider responses', 'bool', ''],
  ['part_cache_ttl', 'Cache lifetime (seconds)', 'number', ''],
  ['http_retries', 'HTTP retries', 'number', ''],
  ['request_timeout', 'Request timeout (seconds)', 'number', ''],
];

function settingField(key, label, type, hint) {
  const value = S.settings[key];
  if (type === 'bool') {
    return `<label class="check" style="margin:7px 0">
      <input type="checkbox" data-setting="${esc(key)}" ${value ? 'checked' : ''}>
      <span>${esc(label)}${hint ? `<br><span class="hint">${esc(hint)}</span>` : ''}</span>
    </label>`;
  }
  if (type === 'currency') {
    const currencies = (S.boot && S.boot.currencies) || ['USD'];
    return `<div class="field" style="margin-bottom:11px">
      <label>${esc(label)}</label>
      <select data-setting="${esc(key)}">${currencies.map(c =>
        `<option value="${esc(c)}" ${c === value ? 'selected' : ''}>${esc(c)}</option>`)
        .join('')}</select>
      ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</div>`;
  }
  return `<div class="field" style="margin-bottom:11px">
    <label>${esc(label)}</label>
    <input type="${type === 'number' ? 'number' : 'text'}"
      data-setting="${esc(key)}" value="${esc(value)}"
      ${type === 'number' ? 'step="any"' : ''}>
    ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</div>`;
}

function renderSettings() {
  const specs = (S.boot && S.boot.provider_specs) || {};
  $('#pane-providers').innerHTML = `
<p class="sub">
  Credentials are stored in your operating system keyring when one is
  available, otherwise in a file readable only by you. They are never written
  to logs or reports.
</p>
${(S.providers || []).map(p => {
  const spec = specs[p.id] || {};
  return `
<div class="provcard">
  <div class="top">
    <label class="check" style="margin:0">
      <input type="checkbox" data-provider="${esc(p.id)}"
        ${p.selected ? 'checked' : ''}>
      <span><b>${esc(p.name)}</b></span>
    </label>
    ${p.configured ? pill('configured', 'good')
      : (spec.credentials || []).some(c => c.required)
        ? pill('no key', 'warning') : pill('no key needed', 'neutral')}
    ${p.active ? pill('active', 'info') : ''}
    <div class="spacer" style="flex:1"></div>
    ${p.signup_url ? `<a href="${esc(p.signup_url)}" target="_blank"
      rel="noopener" style="font-size:12px">Get a key ↗</a>` : ''}
    ${p.docs_url ? `<a href="${esc(p.docs_url)}" target="_blank"
      rel="noopener" style="font-size:12px">API docs ↗</a>` : ''}
  </div>
  ${p.notes ? `<div class="notes">${esc(p.notes)}</div>` : ''}
  ${(p.fields || []).length ? `<div class="creds">${p.fields.map(f => `
    <div class="field">
      <label>${esc(f.label)}${f.required ? ' *' : ''}</label>
      <input type="${f.secret ? 'password' : 'text'}"
        data-cred="${esc(p.id)}:${esc(f.key)}"
        placeholder="${esc(f.present ? f.masked : (f.help || ''))}"
        autocomplete="off" ${f.from_env ? 'disabled' : ''}>
      ${f.from_env ? `<span class="hint">Set by the environment variable
        ${esc((f.env_names || [])[0] || '')}.</span>`
        : f.help ? `<span class="hint">${esc(f.help)}</span>` : ''}
    </div>`).join('')}</div>` : ''}
  <div class="actions">
    <button class="btn sm" data-save-cred="${esc(p.id)}">Save key</button>
    <button class="btn sm" data-test="${esc(p.id)}">Test connection</button>
    ${p.configured ? `<button class="btn sm danger"
      data-clear-cred="${esc(p.id)}">Remove key</button>` : ''}
    <span class="muted" data-test-result="${esc(p.id)}"></span>
  </div>
</div>`;
}).join('')}`;

  $('#pane-analysis').innerHTML =
    ANALYSIS_FIELDS.map(([k, l, t, h]) => settingField(k, l, t, h)).join('');

  $('#pane-weights').innerHTML = `
<p class="sub">Relative weights for the per-line risk score and the BOM health
  roll-up. They are normalised, so only the ratios matter.</p>
${WEIGHT_FIELDS.map(([k, l]) => settingField(k, l, 'number', '')).join('')}`;

  $('#pane-ingest').innerHTML =
    INGEST_FIELDS.map(([k, l, t, h]) => settingField(k, l, t, h)).join('');

  const config = (S.boot && S.boot.config) || {};
  const templates = (S.boot && S.boot.templates) || [];
  $('#pane-system').innerHTML = `
<dl class="kv">
  <dt>Version</dt><dd>${esc((S.boot && S.boot.app.version) || '')}</dd>
  <dt>Python</dt><dd>${esc(config.python || '')}</dd>
  <dt>Platform</dt><dd>${esc(config.platform || '')}</dd>
  <dt>Data folder</dt><dd class="mono">${esc(config.data_dir || '')}</dd>
  <dt>Config folder</dt><dd class="mono">${esc(config.config_dir || '')}</dd>
  <dt>Database</dt><dd class="mono">${esc(config.db_path || '')}</dd>
  <dt>OS keyring</dt><dd>${config.keyring ? 'available' : 'not installed'}</dd>
  <dt>Packaged build</dt><dd>${config.frozen ? 'yes' : 'no (running from source)'}</dd>
</dl>
<div class="inline" style="margin-top:14px">
  <button class="btn sm" id="btn-prune">Prune expired cache</button>
  <button class="btn sm danger" id="btn-clear-cache">Clear all cached data</button>
</div>
<h3 style="margin-top:20px;font-size:12px;color:var(--ink-3);
  text-transform:uppercase;letter-spacing:.06em">Saved column templates</h3>
${templates.length ? templates.map(t => `
<div class="scorerow" style="grid-template-columns:1fr auto auto;gap:10px">
  <span>${esc(t.name)}</span>
  <span class="muted">used ${num(t.use_count)}×</span>
  <button class="btn sm danger" data-del-template="${esc(t.id)}">Delete</button>
</div>`).join('')
  : '<p class="muted">None yet. Confirm a column mapping and it is saved here.</p>'}`;

  wireSettings();
}

function wireSettings() {
  const modal = $('#settings-modal');
  $$('[data-provider]', modal).forEach(input => {
    input.addEventListener('change', () => {
      const id = input.dataset.provider;
      const list = S.settings.providers || [];
      S.settings.providers = input.checked
        ? Array.from(new Set(list.concat([id])))
        : list.filter(p => p !== id);
    });
  });
  $$('[data-save-cred]', modal).forEach(button => {
    button.addEventListener('click', async () => {
      const id = button.dataset.saveCred;
      const credentials = {};
      $$(`[data-cred^="${id}:"]`, modal).forEach(input => {
        if (input.disabled) return;
        const key = input.dataset.cred.split(':')[1];
        if (input.value.trim()) credentials[key] = input.value.trim();
      });
      if (!Object.keys(credentials).length) {
        toast('Enter a value first.');
        return;
      }
      try {
        const data = await api(`/api/providers/${id}/credentials`,
                               { json: { credentials, enable: true } });
        S.providers = data.providers;
        S.settings.providers = Array.from(new Set(
          (S.settings.providers || []).concat([id])));
        toast(`${id} credentials saved.`, 'success');
        renderSettings();
        renderProviderChips();
      } catch (err) { toast(err.message, 'error'); }
    });
  });
  $$('[data-clear-cred]', modal).forEach(button => {
    button.addEventListener('click', async () => {
      const id = button.dataset.clearCred;
      await api(`/api/providers/${id}/credentials`, { method: 'DELETE' });
      S.providers = await api('/api/providers');
      toast(`${id} key removed.`);
      renderSettings();
      renderProviderChips();
    });
  });
  $$('[data-test]', modal).forEach(button => {
    button.addEventListener('click', async () => {
      const id = button.dataset.test;
      const out = $(`[data-test-result="${id}"]`, modal);
      out.innerHTML = '<span class="loading"></span> testing…';
      try {
        const data = await api('/api/providers/test', { json: { providers: [id] } });
        const result = (data.results || []).find(r => r.provider === id) || {};
        out.innerHTML = result.ok
          ? `${pill('ok', 'good')} ${esc(result.message || '')}
             ${result.latency_ms ? `(${num(result.latency_ms)} ms)` : ''}`
          : `${pill('failed', 'critical')} ${esc(result.message || '')}`;
      } catch (err) {
        out.innerHTML = `${pill('failed', 'critical')} ${esc(err.message)}`;
      }
    });
  });
  $$('[data-setting]', modal).forEach(input => {
    input.addEventListener('change', () => {
      const key = input.dataset.setting;
      S.settings[key] = input.type === 'checkbox' ? input.checked
        : input.type === 'number' ? Number(input.value) : input.value;
    });
  });
  const prune = $('#btn-prune');
  if (prune) prune.addEventListener('click', async () => {
    const data = await api('/api/cache/prune', { json: {} });
    toast(`Pruned ${Object.values(data.removed || {}).reduce((a, b) => a + b, 0)} rows.`);
  });
  const clear = $('#btn-clear-cache');
  if (clear) clear.addEventListener('click', async () => {
    await api('/api/cache/clear', { json: {} });
    toast('Cached provider data cleared.', 'success');
  });
  $$('[data-del-template]', modal).forEach(button => {
    button.addEventListener('click', async () => {
      await api('/api/templates/' + button.dataset.delTemplate,
                { method: 'DELETE' });
      S.boot.templates = (await api('/api/templates')).templates;
      renderSettings();
      toast('Template deleted.');
    });
  });
}

function openSettings(pane = 'providers') {
  renderSettings();
  selectSettingsPane(pane);
  $('#settings-modal').hidden = false;
}

function selectSettingsPane(pane) {
  $$('#settings-tabs .subtab').forEach(tab => {
    tab.setAttribute('aria-selected', String(tab.dataset.pane === pane));
  });
  ['providers', 'analysis', 'weights', 'ingest', 'system'].forEach(name => {
    $('#pane-' + name).hidden = name !== pane;
  });
}

async function saveSettings() {
  $('#settings-status').textContent = 'Saving…';
  try {
    const data = await api('/api/settings', { json: S.settings });
    S.settings = data.settings;
    S.providers = await api('/api/providers');
    renderProviderChips();
    syncTopControls();
    $('#settings-status').textContent =
      data.changed.length ? `Saved ${data.changed.length} change(s).` : 'No changes.';
    toast('Settings saved.', 'success');
  } catch (err) {
    $('#settings-status').textContent = '';
    toast(err.message, 'error');
  }
}

function renderExportMenu() {
  const formats = (S.boot && S.boot.export_formats) || [];
  $('#export-list').innerHTML = formats.map(f => `
<div class="scorerow" style="grid-template-columns:1fr auto;gap:12px">
  <span><b>${esc(f.id)}</b><br><span class="hint">${esc(f.label)}</span></span>
  <button class="btn sm" data-export="${esc(f.id)}">Download</button>
</div>`).join('');
  $$('[data-export]', $('#export-modal')).forEach(button => {
    button.addEventListener('click', () => {
      const format = button.dataset.export;
      const url = `/api/analysis/${S.analysisId}/export?format=${
        encodeURIComponent(format)}&t=${encodeURIComponent(S.token)}`;
      download(url);
      toast(`Building the ${format} export…`);
    });
  });
}

/* ------------------------------------------------------------ analysis flow */

async function uploadFile(file) {
  if (!file) return;
  $('#filechip').hidden = false;
  $('#filename').textContent = file.name;
  toast(`Reading ${file.name}…`);
  const form = new FormData();
  form.append('file', file, file.name);
  try {
    const data = await api('/api/upload', { method: 'POST', body: form });
    S.upload = data;
    if (data.ok === false) {
      toast(data.error, 'error', 9000);
    }
    show('mapping');
    renderMapping();
  } catch (err) {
    toast(err.message, 'error', 9000);
    show('start');
  }
}

async function remap() {
  if (!S.upload) return;
  const mapping = {};
  $$('#map-rows select').forEach(select => {
    const index = Number(select.value);
    if (index >= 0) mapping[select.dataset.field] = index;
  });
  const payload = {
    upload_id: S.upload.upload_id,
    mapping,
    sheet_name: $('#sheet-select').value || undefined,
    header_row: Number($('#header-row').value) || undefined,
    merge_duplicate_mpns: $('#opt-merge').checked,
    expand_ref_ranges: $('#opt-refs').checked,
    treat_blank_qty_as_one: $('#opt-blankqty').checked,
    combine_matching_sheets: $('#opt-sheets').checked,
  };
  try {
    const data = await api('/api/remap', { json: payload });
    S.upload = Object.assign({}, data, { filename: S.upload.filename });
    renderMapping();
    toast('Mapping applied.', 'success');
  } catch (err) {
    toast(err.message, 'error', 8000);
  }
}

async function startAnalysis() {
  if (!S.upload) return;
  const buildQty = Number($('#map-build-qty').value) || 100;
  S.settings.build_quantity = buildQty;
  show('running');
  S.runStart = Date.now();
  $('#run-log').innerHTML = '';
  $('#run-bar').style.width = '0%';
  $('#run-title').textContent = `Analysing ${S.upload.filename || 'BOM'}`;
  try {
    const data = await api('/api/analyse', {
      json: {
        upload_id: S.upload.upload_id,
        settings: { build_quantity: buildQty },
      },
    });
    S.job = data.job;
    pollJob();
  } catch (err) {
    toast(err.message, 'error', 9000);
    show('mapping');
  }
}

function pollJob() {
  clearInterval(S.poll);
  S.poll = setInterval(async () => {
    if (!S.job) { clearInterval(S.poll); return; }
    try {
      const data = await api(`/api/jobs/${S.job.id}?events=40`);
      const job = data.job;
      S.job = job;
      const progress = job.progress || {};
      $('#run-message').textContent = progress.message || 'Working…';
      $('#run-bar').style.width = (progress.percent || 0) + '%';
      $('#run-percent').textContent = Math.round(progress.percent || 0) + '%';
      $('#run-elapsed').textContent =
        `· ${((Date.now() - S.runStart) / 1000).toFixed(1)} s elapsed`;
      const log = $('#run-log');
      log.innerHTML = (job.events || []).map(e =>
        `<div>[${String(e.stage || '').padEnd(10)}] ${esc(e.message || '')}</div>`)
        .join('');
      log.scrollTop = log.scrollHeight;

      if (job.state === 'done') {
        clearInterval(S.poll);
        const result = await api(`/api/jobs/${job.id}/result`);
        loadAnalysis(result.analysis, result.analysis_id);
      } else if (job.state === 'error') {
        clearInterval(S.poll);
        toast(job.error || 'The analysis failed.', 'error', 12000);
        show('mapping');
      } else if (job.state === 'cancelled') {
        clearInterval(S.poll);
        toast('Analysis cancelled.');
        show('mapping');
      }
    } catch (err) {
      clearInterval(S.poll);
      toast(err.message, 'error');
    }
  }, 450);
}

function loadAnalysis(analysis, analysisId) {
  S.analysis = analysis;
  S.analysisId = analysisId;
  S.sort = {};
  S.filters = {};
  $('#btn-export').disabled = false;
  $('#build-controls').hidden = false;
  $('#filechip').hidden = false;
  $('#filename').textContent = analysis.bom.name || analysis.bom.source_file || 'BOM';
  syncTopControls();

  const errors = (analysis.results || []).reduce((sum, r) =>
    sum + (r.issues || []).concat(r.line.issues || [])
      .filter(i => i.severity === 'error').length, 0) +
    (analysis.issues || []).filter(i => i.severity === 'error').length;
  $('#c-bom').textContent = num(analysis.summary.total_lines);
  $('#c-risk').textContent = num(
    (analysis.summary.risk_counts.Critical || 0) +
    (analysis.summary.risk_counts.High || 0));
  $('#c-alt').textContent = num((analysis.results || [])
    .reduce((sum, r) => sum + (r.alternates || []).length, 0));
  $('#c-issues').textContent = num(errors);

  show('dashboard');
  renderDashboard();
  const grade = analysis.health.grade;
  toast(`Analysis complete — health ${num(analysis.health.score)}/100 (${grade}).`,
        'success');
}

async function openProject(projectId) {
  toast('Opening project…');
  try {
    const data = await api('/api/projects/' + projectId);
    loadAnalysis(data.analysis, data.analysis_id);
  } catch (err) { toast(err.message, 'error'); }
}

async function reanalyse() {
  if (!S.upload) {
    toast('Re-run needs the original file — please upload it again.');
    return;
  }
  S.settings.build_quantity = Number($('#build-qty').value) || 100;
  S.settings.currency = $('#currency').value;
  await api('/api/settings', { json: {
    build_quantity: S.settings.build_quantity,
    currency: S.settings.currency,
  } });
  $('#map-build-qty').value = S.settings.build_quantity;
  startAnalysis();
}

function syncTopControls() {
  $('#build-qty').value = S.settings.build_quantity || 100;
  const currencies = (S.boot && S.boot.currencies) || ['USD'];
  $('#currency').innerHTML = currencies.map(c =>
    `<option value="${esc(c)}" ${c === S.settings.currency ? 'selected' : ''}>${
      esc(c)}</option>`).join('');
}

function renderProviderChips() {
  const active = (S.providers || []).filter(p => p.active);
  const offline = active.length === 1 && active[0].id === 'mock';
  $('#provider-chips').innerHTML = offline
    ? `<button class="btn sm" id="chip-offline">${pill('offline', 'warning')}
       &nbsp;add a key</button>`
    : active.map(p => `<span class="pill info" title="${esc(p.name)}">${
        esc(p.name.split(' ')[0])}</span>`).join(' ');
  const chip = $('#chip-offline');
  if (chip) chip.addEventListener('click', () => openSettings('providers'));
}

/* ------------------------------------------------------------------ 8. boot */

function wireGlobal() {
  /* file input + drop zone */
  $('#btn-pick').addEventListener('click', () => $('#file-input').click());
  $('#file-input').addEventListener('change', event => {
    uploadFile(event.target.files[0]);
    event.target.value = '';
  });
  const zone = $('#dropzone');
  ['dragenter', 'dragover'].forEach(name =>
    zone.addEventListener(name, event => {
      event.preventDefault();
      zone.classList.add('hot');
    }));
  ['dragleave', 'drop'].forEach(name =>
    zone.addEventListener(name, event => {
      event.preventDefault();
      zone.classList.remove('hot');
    }));
  zone.addEventListener('drop', event => {
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) uploadFile(file);
  });
  window.addEventListener('dragover', event => event.preventDefault());
  window.addEventListener('drop', event => event.preventDefault());

  $('#btn-sample').addEventListener('click', loadSample);

  /* tabs */
  $$('#tabs .tab').forEach(tab => {
    tab.addEventListener('click', () => {
      show(tab.dataset.view);
      renderCurrentView();
    });
  });

  /* mapping */
  $('#btn-remap').addEventListener('click', remap);
  $('#btn-analyse').addEventListener('click', startAnalysis);
  $('#btn-cancel').addEventListener('click', async () => {
    if (S.job) await api(`/api/jobs/${S.job.id}/cancel`, { json: {} });
  });

  /* top bar */
  $('#btn-close-file').addEventListener('click', () => {
    S.analysis = null; S.analysisId = null; S.upload = null;
    $('#filechip').hidden = true;
    $('#build-controls').hidden = true;
    $('#btn-export').disabled = true;
    closeDrawer();
    show('start');
    renderStart();
  });
  $('#btn-reanalyse').addEventListener('click', reanalyse);
  $('#btn-theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light'
      ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('bomiq.theme', next); } catch (_) {}
  });
  $('#btn-settings').addEventListener('click', () => openSettings());
  $('#btn-export').addEventListener('click', () => {
    renderExportMenu();
    $('#export-modal').hidden = false;
  });

  /* modals */
  $('#btn-settings-close').addEventListener('click',
    () => { $('#settings-modal').hidden = true; });
  $('#btn-settings-cancel').addEventListener('click',
    () => { $('#settings-modal').hidden = true; });
  $('#btn-settings-save').addEventListener('click', saveSettings);
  $$('#settings-tabs .subtab').forEach(tab =>
    tab.addEventListener('click', () => selectSettingsPane(tab.dataset.pane)));
  $('#btn-export-close').addEventListener('click',
    () => { $('#export-modal').hidden = true; });
  $('#btn-export-cancel').addEventListener('click',
    () => { $('#export-modal').hidden = true; });
  $('#btn-save-project').addEventListener('click', async () => {
    try {
      const data = await api('/api/projects', {
        json: { analysis_id: S.analysisId, name: S.analysis.bom.name },
      });
      S.boot.projects = data.projects;
      toast('Saved. It will appear on the start screen.', 'success');
    } catch (err) { toast(err.message, 'error'); }
  });

  /* drawer */
  $('#btn-drawer-close').addEventListener('click', closeDrawer);

  /* search */
  $('#btn-search').addEventListener('click', runSearch);
  $('#search-input').addEventListener('keydown', event => {
    if (event.key === 'Enter') runSearch();
  });

  /* keyboard */
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      if (!$('#settings-modal').hidden) { $('#settings-modal').hidden = true; return; }
      if (!$('#export-modal').hidden) { $('#export-modal').hidden = true; return; }
      if ($('#drawer').classList.contains('open')) { closeDrawer(); return; }
    }
    if ((event.ctrlKey || event.metaKey) && event.key === 'k' && S.analysis) {
      event.preventDefault();
      show('bom');
      renderBom();
      const input = $('[data-filter="bomtable:text"]');
      if (input) input.focus();
    }
  });

  /* modal backdrop clicks */
  ['settings-modal', 'export-modal'].forEach(id => {
    const modal = $('#' + id);
    modal.addEventListener('click', event => {
      if (event.target === modal) modal.hidden = true;
    });
  });
}

const SAMPLE_CSV = `Project: Sample sensor board,,,,,,,
Rev: B,,,,,,,
,,,,,,,
Item,Qty,Manufacturer,Mfr Part Number,Description,Reference,Package,Notes
1,4,Yageo,RC0603FR-0710KL,RES 10K 1% 1/10W 0603,R1-R4,0603,
2,6,Murata,GRM188R71C104KA01D,CAP CER 0.1UF 16V X7R 0603,C1-C6,0603,
3,2,Samsung,CL10B104KB8NNNC,CAP CER 0.1UF 50V X7R 0603,C7;C8,0603,
4,1,Texas Instruments,LM358DR,IC OPAMP GP 2 CIRCUIT SOIC-8,U1,SOIC-8,
5,1,Analog Devices,MAX3232ECPE+,IC TRANSCEIVER RS232 DIP-16,U2,DIP-16,legacy interface
6,1,TDK,MPU-6050,SENSOR 6-AXIS IMU I2C QFN-24,U3,QFN-24,
7,1,STMicroelectronics,STM32F103C8T6,IC MCU 32BIT 64KB LQFP-48,U4,LQFP-48,
8,2,Nexperia,BAT54S,DIODE SCHOTTKY 30V SOT-23,D1 D2,SOT-23,
9,1,Abracon,ABM8-8.000MHZ-B2-T,CRYSTAL 8.000MHZ 18PF SMD,Y1,3225,
10,1,Wurth Elektronik,61300411121,CONN HEADER VERT 4POS 2.54MM,J1,THT,
11,3,Littelfuse,0451002.MRL,FUSE BOARD MOUNT 2A 125V 1206,F1-F3,1206,
12,1,,DNP,MOUNTING HOLE,MH1,,do not populate
13,2,AVX,TAJB106K016RNJ,CAP TANT 10UF 16V 10% 1210,C9 C10,1210,
14,1,Espressif Systems,ESP32-WROOM-32E,MODULE WIFI/BT 4MB FLASH,U5,SMD Module,
TOTAL,26,,,,,,
`;

async function loadSample() {
  const blob = new Blob([SAMPLE_CSV], { type: 'text/csv' });
  const file = new File([blob], 'sample-sensor-board.csv', { type: 'text/csv' });
  await uploadFile(file);
}

async function boot() {
  try {
    const saved = localStorage.getItem('bomiq.theme');
    if (saved) document.documentElement.dataset.theme = saved;
  } catch (_) {}

  wireGlobal();

  if (!S.token) {
    // Try the API anyway: the server may have been started without a token.
    try {
      await api('/api/bootstrap');
    } catch (err) {
      document.body.innerHTML = `
<div style="max-width:560px;margin:14vh auto;padding:0 24px;font:14px/1.6 system-ui">
  <h1 style="font-size:20px">Session token required</h1>
  <p>This window was opened without the session token that protects the local
     API, so BOM-IQ cannot talk to its own service.</p>
  <p>Open the link the launcher printed in the terminal, or restart the app.</p>
</div>`;
      return;
    }
  }

  try {
    S.boot = await api('/api/bootstrap');
  } catch (err) {
    toast(err.message, 'error', 15000);
    return;
  }
  S.settings = S.boot.settings;
  S.providers = S.boot.providers;
  S.fields = S.boot.fields;
  $('#version').textContent = 'v' + S.boot.app.version;
  renderProviderChips();
  syncTopControls();
  renderStart();
  show('start');
}

boot();
