'use strict';

const $ = id => document.getElementById(id);
const state = {index: null, view: 'overview', analysis: 'overview', benchmarks: null, comparisonMarket: null, comparisonAll: [], comparisonRows: [], comparisonModels: [], comparisonSelected: [], comparisonPoint: 0, comparisonFilter: null, comparisonBounds: null, comparisonRange: null, agreement: [], agreementAll: [], agreementPoint: 0, monthlyCells: [], popup: null, cache: new Map(), market: null, caseMarket: null, caseDate: null, caseRole: 'decision_supervisor', casePage: 0, caseDates: new Map(), caseMatches: [], timeline: [], point: 0, validRange: null, chartBounds: null, prompts: null, figures: [], loadId: 0};
const metricNames = {roc_auc: 'ROC AUC', ap: 'Average precision', f1: 'F1 score', mcc: 'MCC'};
const metricNotes = {roc_auc: 'ROC AUC measures ranking across score thresholds. The chance ranking reference is 0.5.', ap: 'Average precision summarizes precision and recall across score thresholds. It is useful when anomaly dates are rare.', f1: 'F1 balances precision and recall for the saved threshold decisions. Values range from 0 to 1.', mcc: 'Matthews correlation coefficient uses all four outcome counts. Values range from −1 to 1.'};
const shortRole = {quantitative_expert: 'Quantitative', chart_expert: 'Chart', news_reader: 'News Reader', news_assistant: 'News Assistant', news_expert: 'News Expert', decision_supervisor: 'Supervisor'};
const number = value => value === null || value === undefined || value === '' ? null : Number.isFinite(Number(value)) ? Number(value) : null;
const fixed = (value, digits = 3) => number(value) === null ? 'Not available' : number(value).toFixed(digits);
const integer = value => number(value) === null ? 'Not available' : new Intl.NumberFormat('en-US').format(number(value));
const escapeText = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
const dateText = value => /^\d{4}-\d{2}-\d{2}$/.test(value || '') ? new Intl.DateTimeFormat('en-GB', {day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC'}).format(new Date(`${value}T00:00:00Z`)) : value || 'Not available';
const humanKey = key => key.replace(/_/g, ' ').replace(/^./, char => char.toUpperCase());
const roleName = id => state.index.roles.find(role => role.id === id)?.name || shortRole[id] || humanKey(id);
const marketInfo = id => state.index.markets.find(market => market.id === id);
const marketLabel = market => ({'^GSPC': 'S&P 500', FTSE100: 'FTSE 100', GDAXI: 'DAX', CACT: 'CAC All-Tradable', STOXX: 'STOXX Europe 600', 'CBU0.L': 'CBU0.L'}[market.id] || market.name);
const scoredRoles = () => state.index.roles.filter(role => role.id !== 'news_assistant');
const metricFor = (market, role) => state.index.metrics.find(row => row.market === market && row.agent === role);
const observedLabel = label => number(label) === 1 ? 'Observed anomaly' : number(label) === 0 ? 'No observed anomaly' : 'No saved label';
const badge = label => number(label) === 1 ? '<span class="badge anomaly">Observed anomaly</span>' : number(label) === 0 ? '<span class="badge">No observed anomaly</span>' : '<span class="badge">No saved label</span>';

function localPath(path) {
  const url = new URL(path, location.href);
  if (url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) throw new Error('A research asset has an invalid path.');
  const chartCommit = document.querySelector('meta[name="chart-commit"]')?.content;
  if (/^[a-f0-9]{40}$/.test(chartCommit || '') && /^charts\/[a-f0-9]{64}\.webp$/.test(path)) return `https://raw.githubusercontent.com/QuantLet/FINDER-LLM/${chartCommit}/Dashboard/${path}`;
  return url.href;
}

async function readJSON(path) {
  const response = await fetch(localPath(path));
  if (!response.ok) throw new Error('A saved research file could not be loaded. Serve the repository root and open Dashboard/.');
  return response.json();
}

function showError(error) {
  $('error').textContent = error.message || 'The saved data could not be loaded.';
  $('error').hidden = false;
}

async function loadMarket(id) {
  if (!state.cache.has(id)) state.cache.set(id, readJSON(marketInfo(id).file).catch(error => { state.cache.delete(id); throw error; }));
  return state.cache.get(id);
}

function table(headers, rows, className = '') {
  return `<table class="${className}"><thead><tr>${headers.map(header => `<th scope="col">${escapeText(header)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(value => `<td>${value}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

function stat(label, value, detail, small = false) {
  return `<div class="stat"><span class="stat-label">${escapeText(label)}</span><strong class="stat-value${small ? ' small' : ''}">${escapeText(value)}</strong><span class="stat-detail">${escapeText(detail)}</span></div>`;
}

function setOptions(element, rows, valueKey = 'id', labelKey = 'name') {
  element.replaceChildren(...rows.map(row => { const option = document.createElement('option'); option.value = row[valueKey]; option.textContent = row[labelKey]; return option; }));
}

async function showView(view, focus = true) {
  if (!['overview', 'markets', 'cases', 'library'].includes(view)) view = 'overview';
  state.view = view;
  closeDayPopover();
  hideTooltip();
  $('error').hidden = true;
  document.querySelectorAll('.view').forEach(section => { section.hidden = section.id !== `view-${view}`; });
  document.querySelectorAll('.sidebar [data-view]').forEach(button => { if (button.dataset.view === view) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
  history.replaceState(null, '', `#${view}`);
  if (focus) { $('main').focus({preventScroll: true}); window.scrollTo({top: 0}); }
  try {
    if (view === 'overview') await renderComparison();
    if (view === 'markets') await setAnalysis(state.analysis);
    if (view === 'cases') await renderCases();
    if (view === 'library') await renderGallery();
  } catch (error) { showError(error); }
}

function renderOverview() {
  const dates = state.index.markets.flatMap(market => market.dates).sort();
  const cases = state.index.markets.reduce((sum, market) => sum + market.count, 0);
  $('study-stats').innerHTML = stat('Markets', integer(state.index.markets.length), 'Separate market evaluations') + stat('Saved cases', integer(cases), 'One market and target date per case') + stat('Workflow roles', integer(state.index.roles.length), `${scoredRoles().length} roles have published risk scores`) + stat('Target date range', `${dateText(dates[0])}`, `to ${dateText(dates.at(-1))}`, true);
  $('footer-scope').textContent = `${integer(cases)} cases · ${dateText(dates[0])} – ${dateText(dates.at(-1))}`;
  renderMetricMatrix();
  renderBenchmark();
}

function renderMetricMatrix() {
  const metric = $('overview-metric').value;
  $('metric-definition').textContent = metricNotes[metric] + ' Darker cells show larger values.';
  const roles = scoredRoles();
  $('role-matrix').innerHTML = `<table class="matrix-table"><thead><tr><th scope="col">Market</th>${roles.map(role => `<th scope="col" title="${escapeText(role.name)}">${escapeText(shortRole[role.id] || role.name)}</th>`).join('')}</tr></thead><tbody>${state.index.markets.map(market => `<tr><td><button type="button" class="market-link" data-market="${escapeText(market.id)}" title="${escapeText(market.name)}">${escapeText(marketLabel(market))}</button></td>${roles.map(role => { const value = number(metricFor(market.id, role.id)?.[metric]); const shade = value === null ? 'missing' : `heat-${Math.max(0, Math.min(5, Math.floor((metric === 'mcc' ? (value + 1) / 2 : value) * 6)))}`; return `<td class="${shade}"><button type="button" class="metric-cell" data-market="${escapeText(market.id)}" data-metric-role="${escapeText(role.id)}" aria-label="${escapeText(`${market.name}, ${role.name}, ${metricNames[metric]} ${fixed(value)}. Open score timeline.`)}">${value === null ? '—' : fixed(value)}</button></td>`; }).join('')}</tr>`).join('')}</tbody></table>`;
  $('supervisor-metric-label').textContent = `${metricNames[metric]} · Full sample`;
  const width = 490, height = state.index.markets.length * 40 + 40, left = 115, right = 45, top = 12;
  const minimum = metric === 'mcc' ? -1 : 0, span = metric === 'mcc' ? 2 : 1;
  const x = value => left + (value - minimum) / span * (width - left - right);
  const ticks = metric === 'mcc' ? [-1, 0, 1] : [0, .5, 1];
  let svg = `<svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Decision Supervisor ${metricNames[metric]} by market"><title>Decision Supervisor ${metricNames[metric]} by market</title>`;
  ticks.forEach(value => { svg += `<line class="chart-grid" x1="${x(value)}" x2="${x(value)}" y1="3" y2="${height - 28}"></line><text x="${x(value)}" y="${height - 7}" text-anchor="middle">${value.toFixed(1)}</text>`; });
  state.index.markets.forEach((market, index) => { const y = top + index * 40, value = number(metricFor(market.id, 'decision_supervisor')?.[metric]); svg += `<text x="0" y="${y + 17}">${escapeText(marketLabel(market))}</text>`; if (value !== null) svg += `<rect class="bar-fill" x="${Math.min(x(0), x(value))}" y="${y + 2}" width="${Math.abs(x(value) - x(0))}" height="23"></rect><text x="${width - right + 10}" y="${y + 17}">${fixed(value)}</text>`; });
  svg += '</svg>';
  $('market-bars').innerHTML = svg;
  $('bar-note').textContent = metric === 'roc_auc' ? 'The same 0–1 scale is used for every market. The 0.5 gridline is the chance ranking reference.' : metric === 'mcc' ? 'The same −1 to 1 scale is used for every market. Zero is the no-correlation reference.' : 'The same 0–1 scale is used for every market. Values are rounded to three decimals in the table above.';
}

function renderBenchmark() {
  const market = $('benchmark-market').value;
  const rows = state.index.comparison.filter(row => row.market === market);
  $('benchmark-table').innerHTML = rows.length ? table(['Model', 'ROC AUC', 'Average precision', 'F1', 'MCC'], rows.map(row => [escapeText(row.model), fixed(row.auc), fixed(row.ap), fixed(row.f1), fixed(row.mcc)])) : '<p class="empty">No reported comparison is available for this market.</p>';
}

async function renderMarket(reset = false) {
  const id = $('timeline-market').value;
  closeDayPopover();
  hideTooltip();
  $('error').hidden = true;
  const loadId = ++state.loadId;
  $('timeline-status').hidden = false;
  $('timeline-status').textContent = 'Loading saved market records…';
  try {
    const data = await loadMarket(id);
    if (loadId !== state.loadId) return;
    if (state.market !== id || reset) {
      $('timeline-start').value = data.records[0]?.target_date || '';
      $('timeline-end').value = data.records.at(-1)?.target_date || '';
      state.point = data.records.length - 1;
    }
    state.market = id;
    $('timeline-start').min = $('timeline-end').min = data.records[0]?.target_date || '';
    $('timeline-start').max = $('timeline-end').max = data.records.at(-1)?.target_date || '';
    const start = $('timeline-start').value, end = $('timeline-end').value;
    if (start && end && start > end) {
      if (state.validRange?.market === id) { $('timeline-start').value = state.validRange.start; $('timeline-end').value = state.validRange.end; }
      throw new Error('The start date must be on or before the end date. The previous valid date range has been restored.');
    }
    state.validRange = {market: id, start, end};
    state.timeline = data.records.filter(row => (!start || row.target_date >= start) && (!end || row.target_date <= end));
    state.point = Math.max(0, Math.min(state.point, state.timeline.length - 1));
    renderTimeline();
  } finally { if (loadId === state.loadId) $('timeline-status').hidden = true; }
}

function renderTimeline() {
  const rows = state.timeline, role = $('timeline-role').value;
  const labels = rows.filter(row => number(row.label) !== null), anomalies = labels.filter(row => number(row.label) === 1);
  const predictions = rows.filter(row => number(row.predictions?.[role]) !== null), alerts = predictions.filter(row => number(row.predictions[role]) === 1);
  $('market-summary').innerHTML = stat('Target dates', integer(rows.length), 'Current date range') + stat('Observed anomalies', integer(anomalies.length), `${integer(labels.length)} dates have a saved label`) + stat('Saved alerts', integer(alerts.length), `${integer(predictions.length)} saved decisions`) + stat('Selected role', shortRole[role] || roleName(role), marketInfo(state.market).name, true);
  $('timeline-title').textContent = `${marketInfo(state.market).name} · ${roleName(role)}`;
  $('timeline-scope').textContent = rows.length ? `${dateText(rows[0].target_date)} – ${dateText(rows.at(-1).target_date)} · One point per saved target date` : 'No saved dates in this range';
  $('chart-date').max = Math.max(0, rows.length - 1);
  $('chart-date').value = state.point;
  $('chart-date').disabled = !rows.length;
  drawTimeline();
  renderPoint();
  const counts = {tp: 0, fp: 0, fn: 0, tn: 0};
  rows.forEach(row => { const actual = number(row.label), prediction = number(row.predictions?.[role]); if (![0, 1].includes(actual) || ![0, 1].includes(prediction)) return; counts[actual ? prediction ? 'tp' : 'fn' : prediction ? 'fp' : 'tn'] += 1; });
  $('confusion-counts').innerHTML = [['tp', 'Alert + anomaly'], ['fp', 'Alert + no anomaly'], ['fn', 'No alert + anomaly'], ['tn', 'No alert + no anomaly']].map(([key, label]) => `<div class="outcome-box${['fp', 'fn'].includes(key) ? ' unmatched' : ''}"><span>${label}</span><strong>${integer(counts[key])}</strong></div>`).join('');
  const metric = metricFor(state.market, role);
  $('market-role-metrics').innerHTML = metric ? table(['Measure', 'Saved value'], [['ROC AUC', fixed(metric.roc_auc)], ['Average precision', fixed(metric.ap)], ['F1', fixed(metric.f1)], ['MCC', fixed(metric.mcc)], ['Precision', fixed(metric.precision)], ['Recall', fixed(metric.recall)], ['Evaluated dates', integer(metric.n)]]) : '<p class="empty">No saved full-sample metrics.</p>';
  $('timeline-table').innerHTML = rows.length ? table(['Target date', 'Cutoff', 'Score', 'Threshold', 'Label', 'Prediction'], rows.map(row => [`<button class="market-link" type="button" data-case-date="${escapeText(row.target_date)}">${escapeText(row.target_date)}</button>`, escapeText(row.information_cutoff), fixed(row.scores?.[role], 4), fixed(row.thresholds?.[role], 4), row.label === null ? '—' : escapeText(row.label), row.predictions?.[role] === null || row.predictions?.[role] === undefined ? '—' : escapeText(row.predictions[role])])) : '<p class="empty">No records match this date range.</p>';
}

function drawTimeline() {
  const rows = state.timeline, role = $('timeline-role').value;
  if (!rows.length) { $('timeline-chart').innerHTML = '<p class="empty">No saved dates in this range. Choose another range or select Full period.</p>'; return; }
  const width = Math.max(300, $('timeline-chart').clientWidth || 1000), height = 305, left = 42, right = 12, top = 15, bottom = 38;
  state.chartBounds = {width, left, right};
  const times = rows.map(row => Date.parse(row.target_date)), first = times[0], span = Math.max(1, times.at(-1) - first);
  const x = index => rows.length === 1 ? (left + width - right) / 2 : left + (times[index] - first) / span * (width - left - right);
  const y = value => top + (1 - value) * (height - top - bottom);
  let scorePath = '', thresholdPath = '', previousThreshold = null, lastScore = null;
  rows.forEach((row, index) => {
    const score = number(row.scores?.[role]), threshold = number(row.thresholds?.[role]);
    if (score !== null) scorePath += `${lastScore === null ? 'M' : 'L'}${x(index).toFixed(2)},${y(score).toFixed(2)} `;
    lastScore = score;
    if (threshold !== null) thresholdPath += previousThreshold === null ? `M${x(index).toFixed(2)},${y(threshold).toFixed(2)} ` : `H${x(index).toFixed(2)} V${y(threshold).toFixed(2)} `;
    previousThreshold = threshold;
  });
  let svg = `<svg id="timeline-svg" class="chart-svg interactive-chart" tabindex="0" viewBox="0 0 ${width} ${height}" role="img" aria-label="Saved risk scores and thresholds. Use left and right arrow keys to inspect dates, then Enter for a day summary."><title>${escapeText(roleName(role))}: risk score and saved threshold</title>`;
  [0, .25, .5, .75, 1].forEach(value => { svg += `<line class="chart-grid" x1="${left}" x2="${width - right}" y1="${y(value)}" y2="${y(value)}"></line><text x="${left - 12}" y="${y(value) + 4}" text-anchor="end">${value.toFixed(2)}</text>`; });
  const intervals = width < 500 ? 1 : 3;
  const dateIndices = [...new Set(Array.from({length: intervals + 1}, (_, index) => Math.round(index * (rows.length - 1) / intervals)))];
  dateIndices.forEach((index, position) => { svg += `<text x="${x(index)}" y="${height - 10}" text-anchor="${position === 0 ? 'start' : position === dateIndices.length - 1 ? 'end' : 'middle'}">${escapeText(dateText(rows[index].target_date))}</text>`; });
  svg += `<path class="chart-threshold" d="${thresholdPath}"></path><path class="chart-score" d="${scorePath}"></path>`;
  if (rows.length === 1 && number(rows[0].scores?.[role]) !== null) svg += `<circle class="chart-point" cx="${x(0)}" cy="${y(number(rows[0].scores[role]))}" r="4"></circle>`;
  if (rows.length === 1 && number(rows[0].thresholds?.[role]) !== null) svg += `<line class="chart-threshold" x1="${x(0) - 12}" x2="${x(0) + 12}" y1="${y(number(rows[0].thresholds[role]))}" y2="${y(number(rows[0].thresholds[role]))}"></line>`;
  rows.forEach((row, index) => { const outcome = decisionOutcome(row.label, row.predictions?.[role]), score = number(row.scores?.[role]); if (score !== null && (outcome !== 'tn' || rows.length <= 40)) svg += outcomeMarker(outcome, x(index), y(score), rows.length <= 40 ? 4 : 3, '#4169E1'); });
  svg += `<rect class="chart-spine" x="${left}" y="${top}" width="${width-left-right}" height="${height-top-bottom}"></rect>`;
  svg += `<line id="chart-cursor" class="chart-cursor" x1="${x(state.point)}" x2="${x(state.point)}" y1="${top}" y2="${height - bottom}"></line></svg>`;
  $('timeline-chart').innerHTML = svg;
  const nearest = event => {
    const point = svgPoint(event, $('timeline-svg')).x;
    return rows.reduce((closest, row, index) => Math.abs(x(index) - point) < Math.abs(x(closest) - point) ? index : closest, 0);
  };
  $('timeline-svg').addEventListener('pointermove', event => {
    if (state.popup) return;
    const index = nearest(event), cursor = $('chart-cursor');
    cursor.setAttribute('x1', x(index)); cursor.setAttribute('x2', x(index));
    showDayTooltip(rows[index], state.market, role, event);
  });
  $('timeline-svg').addEventListener('pointerleave', () => { hideTooltip(); if (!state.popup) renderPoint(); });
  $('timeline-svg').addEventListener('click', event => {
    state.point = nearest(event);
    $('chart-date').value = state.point;
    renderPoint();
    showDayPopover(rows[state.point], state.market, role, event);
  });
  $('timeline-svg').addEventListener('keydown', event => {
    if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); state.point = Math.max(0, Math.min(rows.length - 1, state.point + (event.key === 'ArrowRight' ? 1 : -1))); $('chart-date').value = state.point; renderPoint(); }
    if (event.key === 'Enter') { event.preventDefault(); showDayPopover(rows[state.point], state.market, role, event); }
  });
}

function renderPoint() {
  const row = state.timeline[state.point], role = $('timeline-role').value;
  if (!row) { $('point-details').innerHTML = ''; $('chart-date-label').textContent = ''; return; }
  const items = [['Target date', dateText(row.target_date)], ['Information cutoff', dateText(row.information_cutoff)], ['Risk score', fixed(row.scores?.[role], 4)], ['Threshold', fixed(row.thresholds?.[role], 4)], ['Saved alert', number(row.predictions?.[role]) === null ? 'Not available' : number(row.predictions[role]) === 1 ? 'Yes' : 'No']];
  $('point-details').innerHTML = items.map(([label, value]) => `<div class="point-item"><span>${label}</span><strong>${escapeText(value)}</strong></div>`).join('') + `<button type="button" class="primary" data-case-date="${escapeText(row.target_date)}">Read this case →</button>`;
  $('chart-date-label').textContent = row.target_date;
  $('chart-date').setAttribute('aria-valuetext', `${dateText(row.target_date)}, risk score ${fixed(row.scores?.[role], 4)}`);
  const cursor = $('chart-cursor');
  if (cursor) { const first = Date.parse(state.timeline[0].target_date), last = Date.parse(state.timeline.at(-1).target_date); const {width, left, right} = state.chartBounds; const x = state.timeline.length === 1 ? (left + width - right) / 2 : left + (Date.parse(row.target_date) - first) / Math.max(1, last - first) * (width - left - right); cursor.setAttribute('x1', x); cursor.setAttribute('x2', x); }
}

async function openCase(market, date) {
  $('case-market').value = market;
  $('case-search').value = '';
  $('case-outcome').value = 'all';
  state.caseDate = date;
  state.caseDates.set(market, date);
  state.casePage = 0;
  state.caseMarket = null;
  await showView('cases');
}

async function renderCases(reset = false) {
  const market = $('case-market').value, loadId = ++state.loadId;
  $('case-status').hidden = false;
  $('case-status').textContent = 'Loading saved case reports…';
  try {
    const data = await loadMarket(market);
    if (loadId !== state.loadId) return;
    const query = $('case-search').value.trim().toLowerCase(), outcome = $('case-outcome').value;
    const dateQuery = /^\d{4}(?:-\d{2}(?:-\d{2})?)?$/.test(query);
    state.caseMatches = data.records.filter(row => (outcome === 'all' || outcome === 'missing' && number(row.label) === null || String(row.label) === outcome) && (!query || (dateQuery ? row.target_date.startsWith(query) : JSON.stringify(row.agents.map(agent => agent.output)).toLowerCase().includes(query))));
    if (reset || state.caseMarket !== market) state.casePage = 0;
    if (!state.caseMatches.some(row => row.target_date === state.caseDate)) {
      const preferred = state.caseDates.get(market) || (market === '^GSPC' && !query && outcome === 'all' ? '2025-04-03' : null);
      state.caseDate = state.caseMatches.find(row => row.target_date === preferred)?.target_date || state.caseMatches[0]?.target_date || null;
    }
    if (state.caseDate) state.caseDates.set(market, state.caseDate);
    if (state.caseMarket !== market && state.caseDate) state.casePage = Math.floor(state.caseMatches.findIndex(row => row.target_date === state.caseDate) / 12);
    state.caseMarket = market;
    state.casePage = Math.min(state.casePage, Math.max(0, Math.ceil(state.caseMatches.length / 12) - 1));
    renderCaseList();
    await renderCaseDetail();
  } finally { if (loadId === state.loadId) $('case-status').hidden = true; }
}

function renderCaseList() {
  const start = state.casePage * 12;
  $('case-count').textContent = integer(state.caseMatches.length);
  $('case-list').innerHTML = state.caseMatches.length ? state.caseMatches.slice(start, start + 12).map(row => `<button type="button" class="case-row" data-select-case="${escapeText(row.target_date)}" aria-pressed="${row.target_date === state.caseDate}"><strong>${escapeText(dateText(row.target_date))}</strong><span class="${number(row.label) === 1 ? 'event-label' : ''}">${number(row.label) === 1 ? 'Observed anomaly' : number(row.label) === 0 ? 'No observed anomaly' : 'No saved label'} · ${fixed(row.scores?.decision_supervisor, 3)}</span></button>`).join('') : '<p class="empty">No matching cases.</p>';
  $('case-page').textContent = state.caseMatches.length ? `${state.casePage + 1} / ${Math.ceil(state.caseMatches.length / 12)}` : '0 / 0';
  $('case-prev').disabled = state.casePage === 0;
  $('case-next').disabled = start + 12 >= state.caseMatches.length;
}

function appendMaskedText(element, text) {
  const parts = String(text).split('[...]');
  parts.forEach((part, index) => { if (index) { const mask = document.createElement('span'); mask.className = 'masked'; mask.textContent = '[...]'; element.append(mask); } element.append(document.createTextNode(part)); });
}

function renderValue(value, depth = 0) {
  const container = document.createElement('div');
  container.className = 'report-content';
  if (value === null || value === undefined) { const paragraph = document.createElement('p'); paragraph.textContent = 'Not available in this saved record.'; container.append(paragraph); return container; }
  if (typeof value !== 'object') { const paragraph = document.createElement('p'); appendMaskedText(paragraph, String(value)); container.append(paragraph); return container; }
  if (Array.isArray(value)) {
    if (!value.length) { const paragraph = document.createElement('p'); paragraph.textContent = 'No items in this saved field.'; container.append(paragraph); return container; }
    const list = document.createElement('ol');
    value.forEach((item, index) => { const entry = document.createElement('li'); if (typeof item !== 'object' || item === null) appendMaskedText(entry, String(item ?? 'Not available')); else if (depth > 1) { const detail = document.createElement('details'); const title = document.createElement('summary'); title.textContent = `Item ${index + 1}`; detail.append(title, renderValue(item, depth + 1)); entry.append(detail); } else entry.append(renderValue(item, depth + 1)); list.append(entry); });
    container.append(list);
    return container;
  }
  Object.entries(value).forEach(([key, item]) => {
    const field = document.createElement(depth > 1 && item && typeof item === 'object' ? 'details' : 'div');
    field.className = 'report-field';
    const heading = document.createElement(field.tagName === 'DETAILS' ? 'summary' : 'h4'); heading.textContent = humanKey(key);
    field.append(heading, renderValue(item, depth + 1)); container.append(field);
  });
  return container;
}

async function renderCaseDetail() {
  const record = state.caseMatches.find(row => row.target_date === state.caseDate);
  if (!record) { $('case-detail').innerHTML = '<div class="panel empty">No case matches these filters. Clear the search or choose another outcome.</div>'; return; }
  if (!record.agents.some(agent => agent.id === state.caseRole)) state.caseRole = record.agents[0]?.id;
  const agent = record.agents.find(item => item.id === state.caseRole), recordMarket = state.caseMarket;
  $('case-detail').innerHTML = `<section class="panel"><div class="case-heading"><div><h2>${escapeText(dateText(record.target_date))}</h2><p>${escapeText(marketInfo(state.caseMarket).name)} · Information cutoff: ${escapeText(dateText(record.information_cutoff))}</p></div>${badge(record.label)}</div><div class="case-scores">${scoredRoles().map(role => `<div class="case-score"><span>${escapeText(shortRole[role.id] || role.name)}</span><strong>${fixed(record.scores?.[role.id], 4)}</strong><span>Threshold ${fixed(record.thresholds?.[role.id], 4)}</span></div>`).join('')}</div></section><section class="panel"><div class="role-tabs" role="group" aria-label="Read a role report">${record.agents.map(item => `<button type="button" data-role="${escapeText(item.id)}" aria-pressed="${item.id === state.caseRole}">${escapeText(shortRole[item.id] || item.name)}</button>`).join('')}</div><div class="agent-meta"><h3>${escapeText(agent?.name || 'Saved role')}</h3><span class="model-tag">${escapeText(agent?.model || 'Model not recorded')}</span></div><div id="agent-output"></div><details class="agent-details"><summary>Read the role prompt</summary><p id="prompt-note" class="hint"></p><div id="agent-prompts"></div></details><details class="agent-details"><summary>Inspect the masked input</summary><p class="hint">Input fields are presented for the paper workflow. Private values are shown as <code>[...]</code>; obsolete non-paper fields are omitted.</p><pre id="agent-input"></pre></details></section><section class="panel"><div class="section-header"><div><h2>Saved input charts</h2><p>Three input charts captured for this target date.</p></div></div><div id="case-charts" class="input-chart-grid"></div></section>`;
  $('agent-output').append(renderValue(agent?.output));
  $('agent-input').textContent = JSON.stringify(agent?.input ?? null, null, 2);
  const charts = record.charts || [];
  $('case-charts').innerHTML = charts.length ? charts.map((chart, index) => `<button type="button" class="input-chart" data-chart-index="${index}"><img src="${escapeText(localPath(chart.src))}" alt="${escapeText(chart.title || `Input chart ${index + 1}`)}" loading="lazy"><span>${escapeText(chart.title || `Input chart ${index + 1}`)}</span></button>`).join('') : '<p class="empty">No input chart is included for this saved case.</p>';
  try {
    if (!state.prompts) state.prompts = await readJSON('data/prompts.json');
    if (recordMarket !== state.caseMarket || record.target_date !== state.caseDate || agent?.id !== state.caseRole) return;
    $('prompt-note').textContent = state.prompts.source_note || state.index.prompt_note || 'Paper prompt presentation from saved requests. Obsolete fields are omitted and field names may be normalized; this is not a byte-identical archived request.';
    const ids = agent?.prompt_ids || [];
    $('agent-prompts').replaceChildren(...ids.map(id => { const block = document.createElement('div'); const label = document.createElement('p'); label.className = 'prompt-id'; label.textContent = id; const text = document.createElement('pre'); text.textContent = state.prompts.prompts[id] || 'This prompt is not included in the public export.'; block.append(label, text); return block; }));
    if (!ids.length) $('agent-prompts').textContent = 'No prompt is included for this saved role.';
  } catch { $('agent-prompts').textContent = 'The saved prompt file could not be loaded.'; }
}

async function renderGallery() {
  if (!state.figures.length) state.figures = await readJSON('../Figures/manifest.json');
  const group = $('figure-filter').value;
  const figures = state.figures.map((figure, index) => { const name = figure.submission_file.replace(/\.png$/i, ''); const category = /Figure (01|05|06)\b/.test(name) ? 'method' : /Figure (02|03|04|07)\b/.test(name) ? 'inputs' : 'results'; return {...figure, index, name, category}; }).filter(figure => group === 'all' || figure.category === group);
  $('figure-gallery').innerHTML = figures.map(figure => `<button type="button" class="figure-card" data-figure="${figure.index}"><img src="${escapeText(localPath('../' + figure.file))}" alt="${escapeText(figure.name)}" loading="lazy"><span class="figure-label"><strong>${escapeText(figure.name)}</strong><small>${{method: 'Method', results: 'Results', inputs: 'Input example'}[figure.category]} · Saved manuscript figure</small></span></button>`).join('');
}

function openFigure(src, title, caption = 'Saved research image. The original file is unchanged.') {
  $('figure-large').src = localPath(src);
  $('figure-large').alt = title;
  $('figure-dialog-title').textContent = title;
  $('figure-caption').textContent = caption;
  $('figure-dialog').showModal();
}

function svgPoint(event, svg) {
  const point = svg.createSVGPoint();
  point.x = event.clientX; point.y = event.clientY;
  return point.matrixTransform(svg.getScreenCTM().inverse());
}

function floatAt(element, event) {
  const target = event?.target?.closest?.('button, input') || event?.currentTarget || $('main');
  const anchor = target.getBoundingClientRect();
  const pointer = typeof event?.clientX === 'number' && (event.clientX || event.clientY);
  const x = pointer ? event.clientX : anchor.left + anchor.width / 2;
  const y = pointer ? event.clientY : anchor.top + Math.min(anchor.height, 100);
  element.hidden = false;
  const box = element.getBoundingClientRect();
  element.style.left = `${Math.max(10, Math.min(innerWidth - box.width - 10, x + 15))}px`;
  element.style.top = `${Math.max(10, Math.min(innerHeight - box.height - 10, y + 16))}px`;
}

function hideTooltip() { $('chart-tooltip').hidden = true; }

function showTooltip(content, event) {
  if (state.popup) return;
  $('chart-tooltip').innerHTML = content;
  floatAt($('chart-tooltip'), event);
}

function dayValues(record, role, secondRole = null) {
  const values = [[roleName(role), fixed(record.scores?.[role], 4)], ['Saved threshold', fixed(record.thresholds?.[role], 4)]];
  if (secondRole) values.push([roleName(secondRole), fixed(record.scores?.[secondRole], 4)], ['Absolute score gap', fixed(Math.abs(number(record.scores[role]) - number(record.scores[secondRole])), 4)]);
  values.push(['Observed outcome', number(record.label) === 1 ? 'Anomaly' : number(record.label) === 0 ? 'No anomaly' : 'No saved label']);
  return values;
}

function showDayTooltip(record, market, role, event, secondRole = null) {
  const values = dayValues(record, role, secondRole);
  showTooltip(`<strong>${escapeText(dateText(record.target_date))}</strong><span class="tooltip-market">${escapeText(marketLabel(marketInfo(market)))}</span><dl>${values.map(([label, value]) => `<dt>${escapeText(label)}</dt><dd>${escapeText(value)}</dd>`).join('')}</dl><small>Select to pin the day summary.</small>`, event);
}

function savedExcerpt(record, role) {
  for (const id of [role, 'decision_supervisor']) {
    const agent = record.agents.find(item => item.id === id), report = agent?.output?.report;
    const text = agent?.output?.assessment?.summary || (typeof report === 'string' ? report : report?.summary || report?.assessment?.summary || report?.rationale || report?.conclusion || report?.evidence);
    if (typeof text === 'string' && text.trim()) { const clean = text.replace(/\s+/g, ' ').trim(); return {role: agent.name || roleName(id), text: clean.length > 240 ? clean.slice(0, 240).replace(/\s+\S*$/, '').trimEnd() + '…' : clean}; }
  }
  return {role: roleName(role), text: 'Open the day details to read the structured saved response.'};
}

function showDayPopover(record, market, role, event, secondRole = null) {
  if (!record) return;
  hideTooltip();
  const excerpt = savedExcerpt(record, role);
  state.popup = {record, market, role, previousFocus: document.activeElement};
  $('day-popover-market').textContent = marketInfo(market).name;
  $('day-popover-title').textContent = dateText(record.target_date);
  $('day-popover-values').innerHTML = dayValues(record, role, secondRole).map(([label, value]) => `<div><span>${escapeText(label)}</span><strong>${escapeText(value)}</strong></div>`).join('') + `<div><span>Information cutoff</span><strong>${escapeText(dateText(record.information_cutoff))}</strong></div>`;
  $('day-popover-summary').replaceChildren();
  const label = document.createElement('strong'); label.className = 'excerpt-label'; label.textContent = `${excerpt.role} · Saved excerpt`;
  $('day-popover-summary').append(label);
  appendMaskedText($('day-popover-summary'), excerpt.text);
  floatAt($('day-popover'), event);
  $('day-popover-close').focus({preventScroll: true});
}

function closeDayPopover(returnFocus = false) {
  const previous = state.popup?.previousFocus;
  state.popup = null;
  $('day-popover').hidden = true;
  if (returnFocus && previous?.isConnected) previous.focus({preventScroll: true});
}

async function setAnalysis(mode) {
  const controls = {overview: 'timeline-market', timeline: 'timeline-market', agreement: 'agreement-market', monthly: 'monthly-market'};
  if (state.analysis !== mode) $(controls[mode]).value = $(controls[state.analysis]).value;
  state.analysis = mode;
  closeDayPopover(); hideTooltip(); $('error').hidden = true;
  document.querySelectorAll('.analysis-view').forEach(view => { view.hidden = view.id !== `analysis-${mode}`; });
  document.querySelectorAll('[data-analysis]').forEach(button => button.setAttribute('aria-pressed', button.dataset.analysis === mode ? 'true' : 'false'));
  if (mode === 'timeline') await renderMarket();
  if (mode === 'agreement') await renderAgreement();
  if (mode === 'monthly') await renderMonthly();
}

async function renderAgreement() {
  closeDayPopover(); hideTooltip();
  const market = $('agreement-market').value, loadId = ++state.loadId;
  $('agreement-chart').innerHTML = '<p class="loading" role="status">Loading saved score pairs…</p>';
  const data = await loadMarket(market);
  if (loadId !== state.loadId) return;
  state.agreementAll = data.records;
  filterAgreement();
}

function filterAgreement() {
  closeDayPopover(); hideTooltip();
  const roleA = $('agreement-role-a').value, roleB = $('agreement-role-b').value, minimum = Number($('agreement-gap').value);
  const valid = state.agreementAll.filter(row => number(row.scores?.[roleA]) !== null && number(row.scores?.[roleB]) !== null);
  state.agreement = valid.filter(row => Math.abs(number(row.scores[roleA]) - number(row.scores[roleB])) + 1e-12 >= minimum);
  state.agreementPoint = Math.max(0, Math.min(state.agreementPoint, state.agreement.length - 1));
  const anomalies = state.agreement.filter(row => number(row.label) === 1).length;
  $('agreement-gap-label').textContent = minimum === 0 ? '0.00 · all pairs' : `At least ${minimum.toFixed(2)}`;
  $('agreement-gap').setAttribute('aria-valuetext', `Absolute score gap of at least ${minimum.toFixed(2)}`);
  $('agreement-scope').textContent = `${integer(state.agreement.length)} of ${integer(valid.length)} dates with both scores · ${integer(anomalies)} observed anomalies · ${marketLabel(marketInfo($('agreement-market').value))}`;
  $('agreement-date').max = Math.max(0, state.agreement.length - 1);
  $('agreement-date').value = state.agreementPoint;
  $('agreement-date').disabled = $('agreement-summary').disabled = !state.agreement.length;
  drawAgreement();
  renderAgreementPoint();
}

function drawAgreement() {
  const rows = state.agreement, roleA = $('agreement-role-a').value, roleB = $('agreement-role-b').value;
  if (!rows.length) { $('agreement-chart').innerHTML = '<p class="empty">No saved dates meet this score gap. Lower the gap or choose another role pair.</p>'; return; }
  const width = Math.max(300, $('agreement-chart').clientWidth), height = Math.min(470, width + 35), left = 48, right = 18, top = 16, bottom = 55;
  const x = value => left + value * (width - left - right), y = value => top + (1 - value) * (height - top - bottom);
  let svg = `<svg id="agreement-svg" class="chart-svg interactive-chart" viewBox="0 0 ${width} ${height}" tabindex="0" role="img" aria-label="Daily role score comparison. Left and right arrows select a date; Enter opens its summary."><title>${escapeText(roleName(roleA))} versus ${escapeText(roleName(roleB))}</title>`;
  [0, .25, .5, .75, 1].forEach(value => { svg += `<line class="chart-grid" x1="${x(value)}" x2="${x(value)}" y1="${y(0)}" y2="${y(1)}"></line><line class="chart-grid" x1="${x(0)}" x2="${x(1)}" y1="${y(value)}" y2="${y(value)}"></line><text x="${x(value)}" y="${height - bottom + 21}" text-anchor="middle">${value.toFixed(2)}</text><text x="${left - 11}" y="${y(value) + 4}" text-anchor="end">${value.toFixed(2)}</text>`; });
  svg += `<rect class="chart-spine" x="${left}" y="${top}" width="${width-left-right}" height="${height-top-bottom}"></rect><line class="agreement-diagonal" x1="${x(0)}" x2="${x(1)}" y1="${y(0)}" y2="${y(1)}"></line><text x="${(x(0) + x(1)) / 2}" y="${height - 5}" text-anchor="middle">${escapeText(roleName(roleA))} score</text><text transform="translate(12 ${(y(0) + y(1)) / 2}) rotate(-90)" text-anchor="middle">${escapeText(roleName(roleB))} score</text>`;
  rows.forEach(row => { svg += `<circle class="agreement-dot ${number(row.label) === 1 ? 'observed-anomaly' : number(row.label) === 0 ? 'ordinary-date' : 'unknown-date'}" cx="${x(number(row.scores[roleA]))}" cy="${y(number(row.scores[roleB]))}" r="4"></circle>`; });
  const selected = rows[state.agreementPoint];
  svg += `<circle id="agreement-focus" class="agreement-focus" cx="${x(number(selected.scores[roleA]))}" cy="${y(number(selected.scores[roleB]))}" r="7"></circle></svg>`;
  $('agreement-chart').innerHTML = svg;
  const nearest = event => { const point = svgPoint(event, $('agreement-svg')); return rows.reduce((best, row, index) => Math.hypot(x(number(row.scores[roleA])) - point.x, y(number(row.scores[roleB])) - point.y) < Math.hypot(x(number(rows[best].scores[roleA])) - point.x, y(number(rows[best].scores[roleB])) - point.y) ? index : best, 0); };
  const moveFocus = index => { const row = rows[index], circle = $('agreement-focus'); circle.setAttribute('cx', x(number(row.scores[roleA]))); circle.setAttribute('cy', y(number(row.scores[roleB]))); };
  $('agreement-svg').addEventListener('pointermove', event => { if (state.popup) return; const index = nearest(event); moveFocus(index); showDayTooltip(rows[index], $('agreement-market').value, roleA, event, roleB); });
  $('agreement-svg').addEventListener('pointerleave', () => { hideTooltip(); if (!state.popup) moveFocus(state.agreementPoint); });
  $('agreement-svg').addEventListener('click', event => { state.agreementPoint = nearest(event); $('agreement-date').value = state.agreementPoint; moveFocus(state.agreementPoint); renderAgreementPoint(); showDayPopover(rows[state.agreementPoint], $('agreement-market').value, roleA, event, roleB); });
  $('agreement-svg').addEventListener('keydown', event => { if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); state.agreementPoint = Math.max(0, Math.min(rows.length - 1, state.agreementPoint + (event.key === 'ArrowRight' ? 1 : -1))); $('agreement-date').value = state.agreementPoint; moveFocus(state.agreementPoint); renderAgreementPoint(); } if (event.key === 'Enter') { event.preventDefault(); showDayPopover(rows[state.agreementPoint], $('agreement-market').value, roleA, event, roleB); } });
}

function renderAgreementPoint() {
  const row = state.agreement[state.agreementPoint], roleA = $('agreement-role-a').value, roleB = $('agreement-role-b').value;
  if (!row) { $('agreement-selected').innerHTML = ''; $('agreement-date-label').textContent = ''; return; }
  const values = [['Target date', dateText(row.target_date)], [shortRole[roleA], fixed(row.scores[roleA], 4)], [shortRole[roleB], fixed(row.scores[roleB], 4)], ['Absolute score gap', fixed(Math.abs(number(row.scores[roleA]) - number(row.scores[roleB])), 4)]];
  $('agreement-selected').innerHTML = values.map(([label, value]) => `<div class="point-item"><span>${escapeText(label)}</span><strong>${escapeText(value)}</strong></div>`).join('') + badge(row.label);
  $('agreement-date-label').textContent = row.target_date;
  $('agreement-date').setAttribute('aria-valuetext', dateText(row.target_date));
}

async function renderMonthly() {
  closeDayPopover(); hideTooltip();
  const market = $('monthly-market').value, loadId = ++state.loadId;
  $('monthly-matrix').innerHTML = '<p class="loading" role="status">Grouping saved dates by month…</p>';
  const data = await loadMarket(market);
  if (loadId !== state.loadId) return;
  const measure = $('monthly-measure').value, months = [...new Set(data.records.map(row => row.target_date.slice(0, 7)))];
  state.monthlyCells = [];
  $('monthly-title').textContent = `${marketLabel(marketInfo(market))} · Month-by-role view`;
  $('monthly-scope').textContent = `${months.length} saved months · ${integer(data.records.length)} target dates`;
  $('monthly-definition').textContent = measure === 'risk' ? 'Average risk score = the mean of available saved scores in that target-date month. Scores are not calibrated probabilities.' : 'Saved alert share = saved alerts divided by dates with a saved decision in that target-date month. It is not the share of observed anomalies.';
  const rows = months.map(month => {
    const records = data.records.filter(row => row.target_date.startsWith(month));
    return `<tr><th scope="row">${escapeText(new Intl.DateTimeFormat('en-GB', {month: 'short', year: 'numeric', timeZone: 'UTC'}).format(new Date(`${month}-01T00:00:00Z`)))}</th>${scoredRoles().map(role => {
      const values = records.map(row => measure === 'risk' ? number(row.scores?.[role.id]) : number(row.predictions?.[role.id])).filter(value => measure === 'risk' ? value !== null : value === 0 || value === 1);
      const sum = values.reduce((total, value) => total + value, 0), value = values.length ? sum / values.length : null;
      const cell = {market, role: role.id, month, measure, value, n: values.length, alerts: measure === 'alerts' ? sum : null, first: records[0].target_date, last: records.at(-1).target_date};
      const index = state.monthlyCells.push(cell) - 1, label = value === null ? '—' : measure === 'risk' ? value.toFixed(4) : `${(value * 100).toFixed(1)}%`;
      const shade = value === null ? 'missing' : `heat-${Math.max(0, Math.min(5, Math.floor(value * 6)))}`;
      return `<td class="${shade}"><button type="button" class="monthly-cell" data-month-cell="${index}" aria-label="${escapeText(`${month}, ${role.name}, ${measure === 'risk' ? 'average risk score' : 'saved alert share'} ${label}, ${values.length} dates. Open this month’s timeline.`)}"><strong>${label}</strong><small>n = ${values.length}</small></button></td>`;
    }).join('')}</tr>`;
  });
  $('monthly-matrix').innerHTML = `<table class="matrix-table monthly-table"><thead><tr><th scope="col">Target month</th>${scoredRoles().map(role => `<th scope="col" title="${escapeText(role.name)}">${escapeText(shortRole[role.id])}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`;
}

function showMonthTooltip(button, event) {
  const cell = state.monthlyCells[Number(button.dataset.monthCell)];
  if (!cell) return;
  const measure = cell.measure === 'risk' ? `Average risk score: ${fixed(cell.value, 4)}` : `Saved alert share: ${cell.value === null ? 'Not available' : (cell.value * 100).toFixed(1) + '%'}`;
  const count = cell.measure === 'risk' ? `${cell.n} saved scores` : `${cell.alerts} saved alerts / ${cell.n} saved decisions`;
  showTooltip(`<strong>${escapeText(cell.month)} · ${escapeText(shortRole[cell.role])}</strong><p>${escapeText(measure)}</p><p>${escapeText(count)}</p><p>${escapeText(dateText(cell.first))} – ${escapeText(dateText(cell.last))}</p><small>Select to inspect this month’s target dates.</small>`, event);
}

async function openMonth(cell) {
  hideTooltip();
  $('timeline-market').value = cell.market;
  $('timeline-role').value = cell.role;
  state.market = cell.market;
  $('timeline-start').value = cell.first;
  $('timeline-end').value = cell.last;
  state.point = 0;
  await setAnalysis('timeline');
  $('main').focus({preventScroll: true});
  window.scrollTo({top: 0});
}

const comparisonNames = {decision_supervisor: 'Decision Supervisor', lstm_ind: 'LSTM-IND', lstm_news: 'LSTM-(IND+NEWS)'};
const outcomeNames = {tp: 'TP · alert + anomaly', fn: 'FN · no alert + anomaly', fp: 'FP · alert + no anomaly', tn: 'TN · no alert + no anomaly'};

function decisionOutcome(label, prediction) {
  if (![0, 1].includes(number(label)) || ![0, 1].includes(number(prediction))) return null;
  return number(label) === 1 ? number(prediction) === 1 ? 'tp' : 'fn' : number(prediction) === 1 ? 'fp' : 'tn';
}

function comparisonValue(value, model, scale = $('comparison-scale').value) {
  const raw = number(value);
  if (raw === null || scale === 'raw') return raw;
  const mean = number(model.standardization?.mean), sd = number(model.standardization?.std_ddof0);
  return mean === null || sd === null || sd <= 0 ? null : (raw - mean) / sd;
}

async function renderComparison(reset = false) {
  const market = $('comparison-market').value, loadId = ++state.loadId;
  closeDayPopover(); hideTooltip(); $('error').hidden = true;
  $('comparison-status').hidden = false;
  $('comparison-status').textContent = 'Loading saved model scores…';
  try {
    const [data, benchmarks] = await Promise.all([loadMarket(market), state.benchmarks || readJSON('data/benchmarks.json')]);
    if (loadId !== state.loadId) return;
    state.benchmarks = benchmarks;
    const benchmark = benchmarks.markets.find(item => item.id === market);
    if (!benchmark) throw new Error('No saved benchmark data are available for this market.');
    state.comparisonModels = [{id: 'decision_supervisor', standardization: benchmark.standardization.decision_supervisor}, ...benchmark.models];
    const lookup = new Map(benchmark.models.map(model => [model.id, new Map(model.records.map(row => [row.target_date, row]))]));
    state.comparisonAll = data.records.map(record => ({...record, models: Object.fromEntries(state.comparisonModels.map(model => [model.id, model.id === 'decision_supervisor' ? {score: record.scores.decision_supervisor, threshold: record.thresholds.decision_supervisor, prediction: record.predictions.decision_supervisor, label: record.label} : lookup.get(model.id).get(record.target_date)]))}));
    const first = data.records[0]?.target_date || '', last = data.records.at(-1)?.target_date || '';
    if (state.comparisonMarket !== market || reset) {
      const initial = state.comparisonMarket === null && market === '^GSPC' && !reset;
      $('comparison-start').value = initial ? '2025-04-01' : first;
      $('comparison-end').value = initial ? '2025-04-14' : last;
      state.comparisonFilter = null; state.comparisonPoint = 0;
    }
    state.comparisonMarket = market;
    $('comparison-start').min = $('comparison-end').min = first;
    $('comparison-start').max = $('comparison-end').max = last;
    let start = $('comparison-start').value, end = $('comparison-end').value;
    if (start > end) {
      const previous = state.comparisonRange;
      $('comparison-start').value = previous?.market === market ? previous.start : first;
      $('comparison-end').value = previous?.market === market ? previous.end : last;
      throw new Error('The start date must be on or before the end date. The previous valid range has been restored.');
    }
    state.comparisonRange = {market, start, end};
    state.comparisonRows = state.comparisonAll.filter(row => (!start || row.target_date >= start) && (!end || row.target_date <= end));
    $('comparison-title').textContent = `${marketLabel(marketInfo(market))} · ${$('comparison-scale').value === 'raw' ? 'Raw scores' : 'Paper z-scores'}`;
    const rows = state.comparisonRows, anomalies = rows.filter(row => number(row.label) === 1).length;
    $('comparison-scope').textContent = rows.length ? `${dateText(rows[0].target_date)} – ${dateText(rows.at(-1).target_date)} · ${rows.length} target dates · ${anomalies} observed anomalies` : 'No saved target dates in this range';
    $('comparison-scale-note').textContent = $('comparison-scale').value === 'raw' ? 'Supervisor: uncalibrated risk score. LSTMs: model probabilities. Supervisor thresholds update from earlier resolved cases; LSTM thresholds stay fixed. Date filters refit neither.' : 'z = (score − full-period mean) / full-period standard deviation (ddof = 0). Score and threshold use the same saved constants. Supervisor thresholds are sequential; LSTM thresholds are fixed. Date filters refit neither.';
    const comparisonLabel = marketInfo(market).comparison_name;
    if (comparisonLabel) { $('benchmark-market').value = comparisonLabel; renderBenchmark(); }
    renderComparisonOutcomes();
    filterComparisonDates();
    drawDistributions();
  } finally { if (loadId === state.loadId) $('comparison-status').hidden = true; }
}

function renderComparisonOutcomes() {
  const rows = state.comparisonRows;
  $('comparison-confusion').innerHTML = `<table class="confusion-table"><thead><tr><th scope="col">Model</th>${['tp', 'fn', 'fp', 'tn'].map(key => `<th scope="col" class="outcome-${key}" title="${escapeText(outcomeNames[key])}">${key.toUpperCase()}</th>`).join('')}</tr></thead><tbody>${state.comparisonModels.map(model => `<tr><td>${comparisonNames[model.id]}</td>${['tp', 'fn', 'fp', 'tn'].map(key => { const n = rows.filter(row => decisionOutcome(row.label, row.models[model.id]?.prediction) === key).length; return `<td class="outcome-${key}"><button type="button" class="outcome-cell" data-outcome-model="${model.id}" data-outcome="${key}" aria-pressed="${state.comparisonFilter?.model === model.id && state.comparisonFilter?.outcome === key}" aria-label="${comparisonNames[model.id]}, ${outcomeNames[key]}, ${n} dates. Filter dates.">${n}</button></td>`; }).join('')}</tr>`).join('')}</tbody></table>`;
}

function filterComparisonDates() {
  closeDayPopover(); hideTooltip();
  const filter = state.comparisonFilter;
  state.comparisonSelected = state.comparisonRows.filter(row => !filter || decisionOutcome(row.label, row.models[filter.model]?.prediction) === filter.outcome);
  state.comparisonPoint = Math.max(0, Math.min(state.comparisonPoint, state.comparisonSelected.length - 1));
  $('comparison-clear').hidden = !filter;
  $('comparison-filter-label').textContent = filter ? `${comparisonNames[filter.model]} · ${outcomeNames[filter.outcome]} · ${state.comparisonSelected.length} of ${state.comparisonRows.length} dates. The timeline retains the full selected range; outlined marks and the date slider follow this filter.` : 'All outcome groups. Select a count to list its dates and inspect their reports.';
  $('comparison-matches').innerHTML = filter ? state.comparisonSelected.map(row => `<button type="button" data-comparison-case="${row.target_date}">${escapeText(dateText(row.target_date))}</button>`).join('') || '<p class="hint">No dates match this outcome in the selected range.</p>' : '';
  $('comparison-date').max = Math.max(0, state.comparisonSelected.length - 1);
  $('comparison-date').value = state.comparisonPoint;
  $('comparison-date').disabled = $('comparison-summary').disabled = !state.comparisonSelected.length;
  drawComparison(); renderComparisonPoint();
}

function outcomeMarker(outcome, x, y, radius = 3, seriesColor = '#89929D') {
  if (outcome === 'tp') return `<circle cx="${x}" cy="${y}" r="${radius}" fill="#B43C4B"></circle>`;
  if (outcome === 'fn') return `<path d="M${x-radius},${y-radius}L${x+radius},${y+radius}M${x-radius},${y+radius}L${x+radius},${y-radius}" stroke="#B43C4B" stroke-width="2.3" fill="none"></path>`;
  if (outcome === 'fp') return `<path d="M${x},${y-radius-1}L${x+radius+1},${y+radius}L${x-radius-1},${y+radius}Z" fill="#AD7625"></path>`;
  if (outcome === 'tn') return `<circle cx="${x}" cy="${y}" r="${Math.max(1.6, radius-1)}" fill="${seriesColor}"></circle>`;
  return '';
}

function comparisonDomain() {
  if ($('comparison-scale').value === 'raw') return [-.04, 1.18];
  const values = state.comparisonModels.flatMap(model => state.comparisonAll.flatMap(row => [comparisonValue(row.models[model.id]?.score, model), comparisonValue(row.models[model.id]?.threshold, model)])).filter(value => value !== null);
  return values.length ? [Math.floor(Math.min(...values) - .25), Math.ceil(Math.max(...values) + 1.15)] : [-3, 3];
}

function drawComparison() {
  const rows = state.comparisonRows;
  if (!rows.length) { $('comparison-chart').innerHTML = '<p class="empty">No saved dates in this range.</p>'; return; }
  const width = Math.max(290, $('comparison-chart').clientWidth), left = 46, right = 10, top = 8, panelHeight = width < 500 ? 142 : 120, gap = 13;
  const lane = top + 3 * (panelHeight + gap) + 19, height = lane + 55, first = Date.parse(rows[0].target_date), last = Date.parse(rows.at(-1).target_date);
  const x = date => rows.length === 1 ? (left + width-right)/2 : left + (Date.parse(date)-first)/Math.max(1,last-first)*(width-left-right);
  const [low, high] = comparisonDomain();
  const y = (value, panel) => top + panel * (panelHeight + gap) + (high-value)/(high-low)*panelHeight;
  state.comparisonBounds = {width, left, right, first, last, top, bottom: lane+18, single: rows.length === 1};
  const tickStep = Math.max(1, Math.ceil((high-low)/4));
  const ticks = $('comparison-scale').value === 'raw' ? [0, .5, 1] : Array.from({length: Math.floor(high/tickStep)-Math.ceil(low/tickStep)+1}, (_, i) => (Math.ceil(low/tickStep)+i)*tickStep);
  let svg = `<svg id="comparison-svg" class="chart-svg interactive-chart" tabindex="0" viewBox="0 0 ${width} ${height}" role="img" aria-label="Three aligned model score panels and a shared observed outcome lane. Arrow keys select dates. Enter opens a day summary."><title>Saved model scores, thresholds, and observed outcomes</title>`;
  state.comparisonModels.forEach((model, panel) => {
    const panelTop = top + panel * (panelHeight+gap);
    rows.forEach((row,index) => { if (number(row.label)!==1) return; const before=index ? (x(rows[index-1].target_date)+x(row.target_date))/2 : left, after=index<rows.length-1 ? (x(rows[index+1].target_date)+x(row.target_date))/2 : width-right; svg += `<rect x="${before}" y="${panelTop}" width="${Math.max(.8,after-before)}" height="${panelHeight}" fill="#B43C4B" opacity=".055"></rect>`; });
    ticks.forEach(value => { svg += `<line x1="${left-4}" x2="${left}" y1="${y(value,panel)}" y2="${y(value,panel)}" stroke="#111"></line><text x="${left-8}" y="${y(value,panel)+4}" text-anchor="end">${$('comparison-scale').value==='raw' ? value.toFixed(1) : value}</text>`; });
    if (low < 0 && high > 0) svg += `<line x1="${left}" x2="${width-right}" y1="${y(0,panel)}" y2="${y(0,panel)}" stroke="#b5bcc5" stroke-width=".6"></line>`;
    let scorePath='',thresholdPath='',previousScore=null,previousThreshold=null;
    rows.forEach(row => { const values=row.models[model.id], score=comparisonValue(values?.score,model), threshold=comparisonValue(values?.threshold,model); if(score!==null) scorePath+=`${previousScore===null?'M':'L'}${x(row.target_date)},${y(score,panel)} `; previousScore=score; if(threshold!==null) thresholdPath+=previousThreshold===null?`M${x(row.target_date)},${y(threshold,panel)} `:`H${x(row.target_date)}V${y(threshold,panel)} `; previousThreshold=threshold; });
    svg+=`<path d="${thresholdPath}" class="chart-threshold"></path><path d="${scorePath}" fill="none" stroke="${model.id==='decision_supervisor'?'#4169E1':'#555F6B'}" stroke-width="1.5"${model.id==='lstm_news'?' stroke-dasharray="5 3"':''}></path>`;
    rows.forEach(row => { const score=comparisonValue(row.models[model.id]?.score,model), outcome=decisionOutcome(row.label,row.models[model.id]?.prediction); if(score===null)return; const px=x(row.target_date),py=y(score,panel); if(outcome!=='tn'||rows.length<=40)svg+=outcomeMarker(outcome,px,py,rows.length<=40?4:2.6,model.id==='decision_supervisor'?'#4169E1':'#555F6B'); if(state.comparisonFilter?.model===model.id&&outcome===state.comparisonFilter.outcome)svg+=`<circle cx="${px}" cy="${py}" r="6" fill="none" stroke="#111" stroke-width="1"></circle>`; if(rows.length===1){ svg+=`<line class="chart-threshold" x1="${px-12}" x2="${px+12}" y1="${y(comparisonValue(row.models[model.id]?.threshold,model),panel)}" y2="${y(comparisonValue(row.models[model.id]?.threshold,model),panel)}"></line>`; } });
    svg+=`<rect class="chart-spine" x="${left}" y="${panelTop}" width="${width-left-right}" height="${panelHeight}"></rect><rect x="${left+5}" y="${panelTop+3}" width="${model.id==='lstm_news'?147:153}" height="18" fill="white" opacity=".95"></rect><text class="comparison-model" x="${left+8}" y="${panelTop+16}">${comparisonNames[model.id]}</text>`;
  });
  svg+=`<text x="${left}" y="${lane-6}" font-weight="700">Observed outcome${width<500 ? ' · red = anomaly' : ''}</text>${width>=500 ? `<text x="${width-right}" y="${lane-6}" text-anchor="end">Anomaly = red · No anomaly = grey</text>` : ''}`;
  rows.forEach((row,index)=>{const before=index?(x(rows[index-1].target_date)+x(row.target_date))/2:left,after=index<rows.length-1?(x(rows[index+1].target_date)+x(row.target_date))/2:width-right;svg+=`<rect x="${before}" y="${lane}" width="${Math.max(.8,after-before)}" height="18" fill="${number(row.label)===1?'#B43C4B':number(row.label)===0?'#e1e3e6':'white'}"></rect>`;});
  svg+=`<rect class="chart-spine" x="${left}" y="${lane}" width="${width-left-right}" height="18"></rect>`;
  const tickCount=rows.length<=12&&width>700?rows.length:width<500?2:4;
  [...new Set(Array.from({length:tickCount},(_,i)=>Math.round(i*(rows.length-1)/Math.max(1,tickCount-1))))].forEach((index,i,all)=>{svg+=`<text x="${x(rows[index].target_date)}" y="${height-10}" text-anchor="${i===0?'start':i===all.length-1?'end':'middle'}">${escapeText(rows.length<=12 ? dateText(rows[index].target_date).replace(/ \d{4}$/, '') : dateText(rows[index].target_date))}</text>`;});
  svg+=`<line id="comparison-cursor" class="chart-cursor" x1="${left}" x2="${left}" y1="${top}" y2="${lane+18}"></line></svg>`;
  $('comparison-chart').innerHTML=svg;
  const nearest=event=>{const point=svgPoint(event,$('comparison-svg')).x,selected=state.comparisonSelected;return selected.reduce((best,row,index)=>Math.abs(x(row.target_date)-point)<Math.abs(x(selected[best].target_date)-point)?index:best,0);};
  $('comparison-svg').addEventListener('pointermove',event=>{if(state.popup||!state.comparisonSelected.length)return;const row=state.comparisonSelected[nearest(event)];moveComparisonCursor(row);showComparisonTooltip(row,event);});
  $('comparison-svg').addEventListener('pointerleave',()=>{hideTooltip();if(!state.popup)renderComparisonPoint();});
  $('comparison-svg').addEventListener('click',event=>{if(!state.comparisonSelected.length)return;state.comparisonPoint=nearest(event);$('comparison-date').value=state.comparisonPoint;renderComparisonPoint();showComparisonPopover(state.comparisonSelected[state.comparisonPoint],event);});
  $('comparison-svg').addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();state.comparisonPoint=Math.max(0,Math.min(state.comparisonSelected.length-1,state.comparisonPoint+(event.key==='ArrowRight'?1:-1)));$('comparison-date').value=state.comparisonPoint;renderComparisonPoint();}if(event.key==='Enter'){event.preventDefault();showComparisonPopover(state.comparisonSelected[state.comparisonPoint],event);}});
}

function moveComparisonCursor(row) {
  const cursor=$('comparison-cursor');if(!cursor)return;
  cursor.style.display=row?'':'none';if(!row)return;
  const b=state.comparisonBounds,x=b.single?(b.left+b.width-b.right)/2:b.left+(Date.parse(row.target_date)-b.first)/Math.max(1,b.last-b.first)*(b.width-b.left-b.right);
  cursor.setAttribute('x1',x);cursor.setAttribute('x2',x);
}

function comparisonTable(row, scale = $('comparison-scale').value) {
  return table(['Model',scale==='raw'?'Score':'z-score',scale==='raw'?'Threshold':'Threshold z','Outcome'],state.comparisonModels.map(model=>{const values=row.models[model.id],outcome=decisionOutcome(row.label,values?.prediction);return [comparisonNames[model.id],fixed(comparisonValue(values?.score,model,scale),4),fixed(comparisonValue(values?.threshold,model,scale),4),`<span class="outcome-${outcome}">${outcome?.toUpperCase()||'Unavailable'}</span>`];}),'comparison-selected-table');
}

function renderComparisonPoint() {
  const row=state.comparisonSelected[state.comparisonPoint];moveComparisonCursor(row);
  $('comparison-date-label').textContent=row?.target_date||'No matching date';
  $('comparison-date').setAttribute('aria-valuetext',row?dateText(row.target_date):'No matching date');
  $('comparison-selected').innerHTML=row?`<p class="selected-date"><strong>${escapeText(dateText(row.target_date))}</strong> · ${observedLabel(row.label)}</p>`+comparisonTable(row):'<p class="hint">No date matches the current outcome filter.</p>';
}

function showComparisonTooltip(row,event) {
  showTooltip(`<strong>${escapeText(dateText(row.target_date))}</strong><span class="tooltip-market">${escapeText(marketLabel(marketInfo(state.comparisonMarket)))}</span><p>${observedLabel(row.label)}</p>${comparisonTable(row)}<small>Select for the daily summary and saved reports.</small>`,event);
}

function showComparisonPopover(row,event) {
  if(!row)return;
  showDayPopover(row,state.comparisonMarket,'decision_supervisor',event);
  $('day-popover-values').innerHTML=`<div class="popover-comparison"><p>${observedLabel(row.label)} · Information cutoff ${escapeText(dateText(row.information_cutoff))}</p>${comparisonTable(row)}${$('comparison-scale').value==='standardized'?'<p class="hint">Table shows paper z-scores; original saved values are unchanged.</p>':''}</div>`;
  floatAt($('day-popover'),event);
}

function distributionBins(rows, model, low, high, bins = 16) {
  return [0,1].map(label=>{const values=rows.filter(row=>number(row.label)===label).map(row=>comparisonValue(row.models[model.id]?.score,model)).filter(value=>value!==null),counts=Array(bins).fill(0);values.forEach(value=>{const index=Math.max(0,Math.min(bins-1,Math.floor((value-low)/(high-low)*bins)));counts[index]+=1;});return {label,n:values.length,counts,shares:values.length?counts.map(count=>count/values.length):[]};});
}

function drawDistributions() {
  const rows=state.comparisonRows,raw=$('comparison-scale').value==='raw',[low,high]=raw?[0,1]:comparisonDomain();
  if (!rows.length) { $('distribution-scope').textContent = 'No saved dates in the selected range'; $('comparison-distributions').innerHTML = '<p class="empty">No scores are available for this date range.</p>'; return; }
  const groups=state.comparisonModels.map(model=>({model,groups:distributionBins(rows,model,low,high)}));
  const ymax=Math.max(.05,...groups.flatMap(item=>item.groups.flatMap(group=>group.shares)))*1.12;
  $('distribution-scope').textContent=`${rows.length} dates in the selected range · Shares within each outcome group · Outcome count filter does not change these distributions`;
  $('comparison-distributions').innerHTML=groups.map(({model,groups})=>{
    const width=330,height=220,left=43,right=12,top=12,bottom=43,plotWidth=width-left-right,plotHeight=height-top-bottom;
    const x=value=>left+(value-low)/(high-low)*plotWidth,y=value=>top+(1-value/ymax)*plotHeight;
    let svg=`<svg class="chart-svg interactive-chart" data-distribution-model="${model.id}" tabindex="0" viewBox="0 0 ${width} ${height}" role="img" aria-label="${comparisonNames[model.id]} score distribution. Use left and right arrows to inspect the 16 score bins."><title>${comparisonNames[model.id]}: scores by observed outcome</title>`;
    [0,ymax/2,ymax].forEach(value=>{svg+=`<text x="${left-7}" y="${y(value)+4}" text-anchor="end">${(value*100).toFixed(0)}%</text>`;});
    [low,(low+high)/2,high].forEach(value=>{svg+=`<text x="${x(value)}" y="${height-25}" text-anchor="middle">${value.toFixed(1)}</text>`;});
    groups.forEach(group=>{if(!group.n)return;let path=`M${left},${y(0)}`;group.shares.forEach((value,index)=>{const bx=left+index/group.shares.length*plotWidth,bx2=left+(index+1)/group.shares.length*plotWidth;path+=`H${bx}V${y(value)}H${bx2}`;});path+=`V${y(0)}`;svg+=`<path d="${path}" fill="none" stroke="${group.label===1?'#B43C4B':'#555F6B'}" stroke-width="1.7"${group.label===0?' stroke-dasharray="5 2"':''}><title>${group.label===1?'Observed anomaly':'No observed anomaly'}: n=${group.n}</title></path>`;});
    svg+=`<rect class="distribution-bin-focus" x="${left}" y="${top}" width="${plotWidth/16}" height="${plotHeight}" fill="#4169E1" opacity="0"></rect><rect class="chart-spine" x="${left}" y="${top}" width="${plotWidth}" height="${plotHeight}"></rect><text x="${left+plotWidth/2}" y="${height-5}" text-anchor="middle">${raw?'Raw score':'Paper z-score'}</text></svg>`;
    return `<figure><figcaption>${comparisonNames[model.id]}</figcaption><p class="distribution-count">Anomaly n = ${groups[1].n}${groups[1].n?'':' (no saved scores)'} · No anomaly n = ${groups[0].n}${groups[0].n?'':' (no saved scores)'}</p>${svg}</figure>`;
  }).join('');
  document.querySelectorAll('[data-distribution-model]').forEach(svg => {
    const item = groups.find(group => group.model.id === svg.dataset.distributionModel), bins = 16, left = 43, plotWidth = 275;
    let selected = 0;
    const preview = (index, event) => {
      selected = Math.max(0, Math.min(bins - 1, index));
      const start = low + selected * (high-low)/bins, end = low + (selected+1) * (high-low)/bins;
      const focus = svg.querySelector('.distribution-bin-focus'); focus.setAttribute('x', left + selected/bins*plotWidth); focus.setAttribute('opacity', '.10');
      const interval = `${fixed(start, 4)} ≤ ${raw ? 'score' : 'z'} ${selected === bins-1 ? '≤' : '<'} ${fixed(end, 4)}`;
      const groupRows = item.groups.map(group => [group.label === 1 ? 'Observed anomaly' : 'No observed anomaly', `${group.counts[selected]} / ${group.n}`, group.n ? `${(group.shares[selected]*100).toFixed(2)}%` : 'No saved scores']);
      showTooltip(`<strong>${comparisonNames[item.model.id]}</strong><p>${escapeText(interval)}</p>${table(['Outcome group', 'Bin count / n', 'Group share'], groupRows, 'comparison-selected-table')}`, event);
      svg.setAttribute('aria-label', `${comparisonNames[item.model.id]}, ${interval}. ${groupRows.map(row => row.join(', ')).join('. ')}. Left and right arrows change bins.`);
    };
    svg.addEventListener('pointermove', event => { if (!state.popup) preview(Math.floor((svgPoint(event, svg).x-left)/plotWidth*bins), event); });
    svg.addEventListener('pointerleave', () => { hideTooltip(); svg.querySelector('.distribution-bin-focus').setAttribute('opacity', '0'); });
    svg.addEventListener('focus', event => preview(selected, event));
    svg.addEventListener('blur', () => { hideTooltip(); svg.querySelector('.distribution-bin-focus').setAttribute('opacity', '0'); });
    svg.addEventListener('keydown', event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); preview(event.key === 'Home' ? 0 : event.key === 'End' ? bins-1 : selected + (event.key === 'ArrowRight' ? 1 : -1), event); } });
  });
}

async function initialise() {
  try {
    state.index = await readJSON('data/index.json');
    if (!Array.isArray(state.index.markets) || !Array.isArray(state.index.metrics) || !state.index.markets.length) throw new Error('The public research index is not complete.');
    setOptions($('comparison-market'), state.index.markets);
    setOptions($('timeline-market'), state.index.markets);
    setOptions($('case-market'), state.index.markets);
    setOptions($('agreement-market'), state.index.markets);
    setOptions($('monthly-market'), state.index.markets);
    setOptions($('agreement-role-a'), scoredRoles());
    setOptions($('agreement-role-b'), scoredRoles());
    $('agreement-role-a').value = 'quantitative_expert';
    $('agreement-role-b').value = 'decision_supervisor';
    setOptions($('timeline-role'), scoredRoles());
    $('timeline-role').value = 'decision_supervisor';
    const comparisons = [...new Set(state.index.comparison.map(row => row.market))].map(name => ({id: name, name}));
    setOptions($('benchmark-market'), comparisons);
    renderOverview();

    $('load-status').hidden = true;
    await showView(location.hash.slice(1) || 'overview', false);
  } catch (error) { $('load-status').hidden = true; if (location.protocol === 'file:') error = new Error('Open this dashboard through a local web server. From the repository root, run python -m http.server 8765 and open http://localhost:8765/Dashboard/.'); showError(error); }
}

$('comparison-market').addEventListener('change', () => renderComparison().catch(showError));
['comparison-start', 'comparison-end', 'comparison-scale'].forEach(id => $(id).addEventListener('change', () => renderComparison().catch(showError)));
$('comparison-reset').addEventListener('click', () => renderComparison(true).catch(showError));
$('comparison-april').addEventListener('click', () => { $('comparison-market').value = '^GSPC'; state.comparisonMarket = '^GSPC'; $('comparison-start').value = '2025-04-01'; $('comparison-end').value = '2025-04-14'; $('comparison-scale').value = 'raw'; state.comparisonFilter = null; state.comparisonPoint = 0; renderComparison().catch(showError); });
$('comparison-date').addEventListener('input', () => { closeDayPopover(); hideTooltip(); state.comparisonPoint = Number($('comparison-date').value); renderComparisonPoint(); });
$('comparison-date').addEventListener('keydown', event => { if (event.key === 'Enter') showComparisonPopover(state.comparisonSelected[state.comparisonPoint], event); });
$('comparison-summary').addEventListener('click', event => showComparisonPopover(state.comparisonSelected[state.comparisonPoint], event));
$('comparison-confusion').addEventListener('click', event => { const button = event.target.closest('[data-outcome-model]'); if (!button) return; state.comparisonFilter = {model: button.dataset.outcomeModel, outcome: button.dataset.outcome}; state.comparisonPoint = 0; renderComparisonOutcomes(); filterComparisonDates(); });
$('comparison-clear').addEventListener('click', () => { state.comparisonFilter = null; state.comparisonPoint = 0; renderComparisonOutcomes(); filterComparisonDates(); });
$('comparison-matches').addEventListener('click', event => { const button = event.target.closest('[data-comparison-case]'); if (button) { state.caseRole = 'decision_supervisor'; openCase(state.comparisonMarket, button.dataset.comparisonCase); } });

document.querySelectorAll('[data-analysis]').forEach(button => button.addEventListener('click', () => setAnalysis(button.dataset.analysis).catch(showError)));
$('timeline-summary').addEventListener('click', event => showDayPopover(state.timeline[state.point], state.market, $('timeline-role').value, event));
$('chart-date').addEventListener('keydown', event => { if (event.key === 'Enter') showDayPopover(state.timeline[state.point], state.market, $('timeline-role').value, event); });
$('agreement-market').addEventListener('change', () => renderAgreement().catch(showError));
['agreement-role-a', 'agreement-role-b'].forEach(id => $(id).addEventListener('change', filterAgreement));
$('agreement-gap').addEventListener('input', filterAgreement);
$('agreement-date').addEventListener('input', () => { closeDayPopover(); hideTooltip(); state.agreementPoint = Number($('agreement-date').value); drawAgreement(); renderAgreementPoint(); });
$('agreement-summary').addEventListener('click', event => showDayPopover(state.agreement[state.agreementPoint], $('agreement-market').value, $('agreement-role-a').value, event, $('agreement-role-b').value));
$('agreement-date').addEventListener('keydown', event => { if (event.key === 'Enter') showDayPopover(state.agreement[state.agreementPoint], $('agreement-market').value, $('agreement-role-a').value, event, $('agreement-role-b').value); });
['monthly-market', 'monthly-measure'].forEach(id => $(id).addEventListener('change', () => renderMonthly().catch(showError)));
$('monthly-matrix').addEventListener('pointermove', event => { const button = event.target.closest('[data-month-cell]'); if (button) showMonthTooltip(button, event); else hideTooltip(); });
$('monthly-matrix').addEventListener('pointerleave', hideTooltip);
$('monthly-matrix').addEventListener('focusin', event => { const button = event.target.closest('[data-month-cell]'); if (button) showMonthTooltip(button, event); });
$('monthly-matrix').addEventListener('focusout', hideTooltip);
$('monthly-matrix').addEventListener('click', event => { const button = event.target.closest('[data-month-cell]'); if (button) openMonth(state.monthlyCells[Number(button.dataset.monthCell)]).catch(showError); });
$('day-popover-close').addEventListener('click', () => closeDayPopover(true));
$('day-popover-details').addEventListener('click', () => { if (!state.popup) return; const {record, market, role} = state.popup; state.caseRole = role; closeDayPopover(); openCase(market, record.target_date); });
document.addEventListener('pointerdown', event => { if (state.popup && !$('day-popover').contains(event.target)) closeDayPopover(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') { closeDayPopover(true); hideTooltip(); } });
window.addEventListener('scroll', hideTooltip, {capture: true, passive: true});

document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => { if (state.index) showView(button.dataset.view); }));
$('overview-metric').addEventListener('change', renderMetricMatrix);
$('benchmark-market').addEventListener('change', renderBenchmark);
$('role-matrix').addEventListener('click', event => { const button = event.target.closest('[data-market]'); if (button) { $('timeline-market').value = button.dataset.market; if (button.dataset.metricRole) $('timeline-role').value = button.dataset.metricRole; state.market = null; state.analysis = 'timeline'; showView('markets'); } });
$('timeline-market').addEventListener('change', () => renderMarket().catch(showError));
$('timeline-role').addEventListener('change', () => { closeDayPopover(); hideTooltip(); renderTimeline(); });
$('timeline-start').addEventListener('change', () => renderMarket().catch(showError));
$('timeline-end').addEventListener('change', () => renderMarket().catch(showError));
$('reset-dates').addEventListener('click', () => renderMarket(true).catch(showError));
$('chart-date').addEventListener('input', () => { closeDayPopover(); hideTooltip(); state.point = Number($('chart-date').value); renderPoint(); });
$('view-markets').addEventListener('click', event => { const button = event.target.closest('[data-case-date]'); if (button) openCase(state.market, button.dataset.caseDate); });
$('case-market').addEventListener('change', () => { state.caseDate = state.caseDates.get($('case-market').value) || null; renderCases(true).catch(showError); });
$('case-outcome').addEventListener('change', () => renderCases(true).catch(showError));
let searchTimer;
$('case-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => renderCases(true).catch(showError), 180); });
document.querySelectorAll('[data-paper-case]').forEach(button => button.addEventListener('click', () => openCase('^GSPC', button.dataset.paperCase)));
$('case-list').addEventListener('click', event => { const button = event.target.closest('[data-select-case]'); if (button) { state.caseDate = button.dataset.selectCase; state.caseDates.set(state.caseMarket, state.caseDate); renderCaseList(); renderCaseDetail().catch(showError); } });
$('case-prev').addEventListener('click', () => { state.casePage -= 1; renderCaseList(); });
$('case-next').addEventListener('click', () => { state.casePage += 1; renderCaseList(); });
$('case-detail').addEventListener('click', event => {
  const roleButton = event.target.closest('[data-role]');
  if (roleButton) { state.caseRole = roleButton.dataset.role; renderCaseDetail().catch(showError); return; }
  const chartButton = event.target.closest('[data-chart-index]');
  if (chartButton) { const record = state.caseMatches.find(row => row.target_date === state.caseDate), chart = record.charts[Number(chartButton.dataset.chartIndex)]; openFigure(chart.src, `${record.target_date} · ${chart.title || 'Input chart'}`, 'Input chart captured for this target date.'); }
});
$('figure-filter').addEventListener('change', () => renderGallery().catch(showError));
$('figure-gallery').addEventListener('click', event => { const button = event.target.closest('[data-figure]'); if (button) { const figure = state.figures[Number(button.dataset.figure)]; openFigure('../' + figure.file, figure.submission_file.replace(/\.png$/i, ''), figure.public_redaction || 'Saved manuscript figure.'); } });
$('figure-close').addEventListener('click', () => $('figure-dialog').close());
$('figure-dialog').addEventListener('click', event => { if (event.target === $('figure-dialog')) { const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.target.close(); } });
let resizeTimer;
window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (state.view === 'overview' && state.comparisonRows.length) { drawComparison(); renderComparisonPoint(); drawDistributions(); } if (state.view === 'markets') { closeDayPopover(); hideTooltip(); if (state.analysis === 'timeline' && state.timeline.length) { drawTimeline(); renderPoint(); } if (state.analysis === 'agreement') drawAgreement(); } }, 120); });
window.addEventListener('hashchange', () => { if (state.index) showView(location.hash.slice(1), false); });
initialise();
