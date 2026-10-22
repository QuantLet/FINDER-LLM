'use strict';

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="studio-token"]').content;
const state = {mode: 'custom', step: 1, paper: null, layers: [], template: 'review', running: false, job: null, result: null, activeRun: null, lastRun: null};
const sampleTask = 'A research team is choosing how to check a new anomaly detection method. Compare a time-ordered holdout test with a random split. Explain one strength and one risk of each. Use only this task; do not invent data or report test results.';
const defaultSchema = {type: 'object', properties: {summary: {type: 'string'}, next_step: {type: 'string'}}, required: ['summary', 'next_step'], additionalProperties: false};
const escapeText = value => String(value).replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
const model = () => $('default-model').value.trim() || 'gpt-5-mini';
const agent = (role, prompt) => ({role, prompt, model: model()});

function newTemplate(name) {
  if (name === 'debate') return [{name: 'Discuss the evidence', mode: 'debate', rounds: 2, agents: [agent('Advocate', 'Build the strongest supported case for the proposed approach. Respond to earlier objections. State what the evidence does and does not support.'), agent('Skeptic', 'Test the proposed approach. Identify specific weaknesses and respond to the advocate. Accept claims when the evidence supports them.')]}, {name: 'Reach a conclusion', mode: 'parallel', rounds: 1, agents: [agent('Judge', 'Compare the arguments with the task and available evidence. Give a balanced conclusion and a clear next step. Do not favour a role because it spoke first or last.')]}];
  if (name === 'blank') return [{name: 'First layer', mode: 'parallel', rounds: 1, agents: [agent('Researcher', 'Answer the task using the supplied information. Separate evidence, assumptions, and unknown facts.')]}];
  return [{name: 'Independent views', mode: 'parallel', rounds: 1, agents: [agent('Researcher', 'Answer the task using the supplied information. Explain the main evidence and assumptions.'), agent('Reviewer', 'Review the task independently. Look for missing information, risks, and alternative explanations. Do not invent facts.')]}, {name: 'Bring the views together', mode: 'parallel', rounds: 1, agents: [agent('Editor', 'Compare the earlier replies. Give a clear, balanced answer to the task. State disagreements and a practical next step.')]}];
}

function showError(message) {
  $('error').textContent = message;
  $('error').hidden = false;
  $('error').scrollIntoView({block: 'nearest'});
}

function clearError() { $('error').hidden = true; }

async function api(path, body) {
  const options = {headers: {'X-Studio-Token': token}};
  if (body !== undefined) { options.method = 'POST'; options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The request did not finish.');
  return data;
}

function setStep(number) {
  clearError();
  state.step = number;
  document.querySelectorAll('.step-panel').forEach(panel => { panel.hidden = panel.id !== `step-${number}`; });
  document.querySelectorAll('.steps [data-step]').forEach(button => { button.classList.toggle('active', Number(button.dataset.step) === number); if (Number(button.dataset.step) === number) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current'); });
  if (number === 3) renderSummary();
  $('main').focus({preventScroll: true});
  window.scrollTo({top: 0});
}

function setMode(mode) {
  if (state.running) return showError('Wait for the active run to finish before changing workflow mode.');
  state.mode = mode;
  ['paper', 'custom'].forEach(value => { $(`mode-${value}`).classList.toggle('active', mode === value); $(`mode-${value}`).setAttribute('aria-pressed', mode === value ? 'true' : 'false'); });
  $('paper-design').hidden = mode !== 'paper';
  $('custom-design').hidden = mode !== 'custom';
  $('design-intro').textContent = mode === 'paper' ? 'The paper method uses fixed roles, prompts, and outputs.' : 'Start small. Add a role or layer only when it helps your task.';
  state.result = null;
  $('results-panel').hidden = true;
  $('progress-panel').hidden = true;
  $('run-status').hidden = true;
  if (state.step === 3) renderSummary();
}

function connection() {
  const mode = document.querySelector('[name="connection-mode"]:checked').value;
  return mode === 'demo' ? {mode} : {mode, endpoint: $('endpoint').value.trim(), api_key: $('api-key').value.trim()};
}

function updateConnection() {
  const live = connection().mode === 'live';
  $('connection-live').hidden = !live;
  $('demo-note').hidden = live;
  if (state.step === 3) renderSummary();
}

function loadTemplate(name) {
  state.layers = newTemplate(name);
  state.template = name;
  $('workflow-name').value = {review: 'Parallel review', debate: 'Short debate', blank: 'My workflow'}[name];
  document.querySelectorAll('[data-template]').forEach(button => button.classList.toggle('active', button.dataset.template === name));
  renderLayers();
}

function renderLayers() {
  $('add-layer').disabled = state.layers.length >= 6;
  $('layers').innerHTML = state.layers.map((layer, index) => `<section class="card layer" data-layer="${index}" aria-label="Layer ${index + 1}">
    <div class="layer-heading"><span class="layer-index">LAYER ${index + 1}</span><input aria-label="Layer ${index + 1} name" data-field="name" value="${escapeText(layer.name)}" maxlength="100"><div class="button-row"><button type="button" class="quiet" data-action="up" ${index === 0 ? 'disabled' : ''} aria-label="Move layer ${index + 1} up">↑</button><button type="button" class="quiet" data-action="down" ${index === state.layers.length - 1 ? 'disabled' : ''} aria-label="Move layer ${index + 1} down">↓</button><button type="button" class="danger" data-action="remove-layer" ${state.layers.length === 1 ? 'disabled' : ''}>Remove layer</button></div></div>
    <div class="field-row"><label>How roles work<select data-field="mode"><option value="parallel" ${layer.mode === 'parallel' ? 'selected' : ''}>Independent — same input, no shared replies</option><option value="sequential" ${layer.mode === 'sequential' ? 'selected' : ''}>In order — read earlier replies</option><option value="debate" ${layer.mode === 'debate' ? 'selected' : ''}>Debate — take turns for several rounds</option></select></label>${layer.mode === 'debate' ? `<label>Rounds<input type="number" data-field="rounds" min="1" max="5" value="${layer.rounds}"></label>` : ''}</div>
    <p class="hint">${layer.mode === 'parallel' ? 'These roles run at the same time. Each sees the task and all earlier layers, but no reply from this layer.' : layer.mode === 'sequential' ? 'Each role sees the task, earlier layers, and every earlier reply in this layer.' : 'Roles speak in the order below. Each turn can use all earlier replies, including earlier rounds.'}</p>
    ${layer.agents.map((item, agentIndex) => `<div class="agent" data-agent="${agentIndex}"><div class="agent-heading"><h3>Role ${agentIndex + 1}</h3><div class="button-row"><button type="button" class="quiet" data-action="agent-up" ${agentIndex === 0 ? 'disabled' : ''} aria-label="Move role ${agentIndex + 1} up in layer ${index + 1}">↑</button><button type="button" class="quiet" data-action="agent-down" ${agentIndex === layer.agents.length - 1 ? 'disabled' : ''} aria-label="Move role ${agentIndex + 1} down in layer ${index + 1}">↓</button><button type="button" class="danger" data-action="remove-agent" ${layer.agents.length === 1 ? 'disabled' : ''}>Remove role</button></div></div><div class="field-row"><label>Role name<input data-agent-field="role" value="${escapeText(item.role)}" maxlength="80"></label><label>Model<input data-agent-field="model" value="${escapeText(item.model)}" list="model-list" maxlength="150"></label></div><details><summary>Role prompt</summary><label>Instructions for this role<textarea data-agent-field="prompt" rows="4" maxlength="12000">${escapeText(item.prompt)}</textarea></label></details></div>`).join('')}
    <button type="button" class="quiet" data-action="add-agent" ${layer.agents.length >= 6 ? 'disabled' : ''}>Add a role to this layer</button></section>`).join('');
}

function readConfig(requireTask = true) {
  let schema = {};
  if ($('output-format').value === 'json') { try { schema = JSON.parse($('output-schema').value); } catch { throw new Error('The JSON schema is not valid JSON. Check commas and quotes.'); } }
  const config = {name: $('workflow-name').value.trim(), task: $('task-input').value.trim(), layers: state.layers, output: $('output-format').value, schema, max_tokens: Number($('max-tokens').value)};
  if (!config.name) throw new Error('Give your workflow a name.');
  if (requireTask && !config.task) throw new Error('Add a task and input in the Design step.');
  if (config.layers.some(layer => !layer.name || layer.agents.some(item => !item.role.trim() || !item.model.trim() || !item.prompt.trim()))) throw new Error('Each layer needs a name. Each role needs a name, model, and prompt.');
  if (!Number.isInteger(config.max_tokens) || config.max_tokens < 128 || config.max_tokens > 8000) throw new Error('Use an output limit from 128 to 8,000 tokens.');
  return config;
}

function paperConfig() {
  return {ticker: $('paper-ticker')?.value || state.paper?.defaults?.ticker || '^GSPC', start_date: $('paper-start').value, end_date: $('paper-end').value, max_windows: Number($('paper-windows').value)};
}

function settingsKey() {
  const conn = connection();
  const config = state.mode === 'paper' ? paperConfig() : {name: $('workflow-name').value, task: $('task-input').value, layers: state.layers, output: $('output-format').value, schema: $('output-schema').value, max_tokens: $('max-tokens').value};
  return JSON.stringify({mode: state.mode, connection: {mode: conn.mode, endpoint: conn.endpoint}, config});
}

function renderPaper() {
  const paper = state.paper;
  if (!paper) return;
  $('paper-start').value = paper.defaults?.start_date || '';
  $('paper-end').value = paper.defaults?.end_date || '';
  $('paper-windows').value = paper.defaults?.max_windows || 1;
  const markets = paper.markets || [{id: paper.defaults?.ticker || '^GSPC', name: paper.defaults?.ticker || '^GSPC'}];
  let tickerLabel = $('paper-ticker-label');
  if (!tickerLabel) { tickerLabel = document.createElement('label'); tickerLabel.id = 'paper-ticker-label'; $('paper-start').closest('.field-row').before(tickerLabel); }
  tickerLabel.innerHTML = `Market<select id="paper-ticker">${markets.map(market => `<option value="${escapeText(market.id)}" ${market.id === paper.defaults?.ticker ? 'selected' : ''}>${escapeText(market.name || market.id)}</option>`).join('')}</select>`;
  $('paper-ticker').addEventListener('change', renderReadiness);
  const details = document.createElement('details');
  const summary = document.createElement('summary'); summary.textContent = 'Fixed models and output fields'; details.append(summary);
  const pre = document.createElement('pre'); pre.textContent = JSON.stringify({models: paper.models, score_fields: paper.fields, new_output_folder: paper.output}, null, 2); details.append(pre);
  $('paper-details').replaceChildren(details);
  renderReadiness();
}

function renderReadiness() {
  const ready = state.paper?.readiness?.[paperConfig().ticker];
  $('paper-status').classList.toggle('warning', !ready?.ready);
  $('paper-status').textContent = ready?.ready ? 'Required local input files are present. The runner will check their content before model calls.' : 'Local inputs are not ready. Prepare your own data before a paper run. See Required input files below.';
  $('paper-missing').textContent = ready?.missing?.length ? ready.missing.join('\n') : 'All required input files are present.';
}

function renderSummary() {
  const live = connection().mode === 'live';
  let rows, note;
  if (state.mode === 'paper') {
    const config = paperConfig();
    rows = [['Workflow', 'Fixed paper method'], ['Market', config.ticker], ['Cutoff dates', `${config.start_date || 'Choose a date'} to ${config.end_date || 'choose a date'}`], ['Windows', config.max_windows], ['Output', state.paper?.output || 'Separate local Runs folder']];
    note = live ? 'The run uses the fixed paper models. It may make several paid API calls per date. New outputs use a unique local folder.' : 'Paper execution needs Live API and prepared local data. Choose Custom workflow to try the free demo.';
  } else {
    const calls = state.layers.reduce((sum, layer) => sum + layer.agents.length * (layer.mode === 'debate' ? Number(layer.rounds) : 1), 0);
    rows = [['Workflow', $('workflow-name').value], ['Layers / calls', `${state.layers.length} layers / ${calls} calls`], ['Output', $('output-format').value === 'json' ? 'JSON, checked against your schema' : 'Text'], ['Storage', 'In memory; download when finished']];
    note = live ? `Your input goes to ${connection().endpoint}. This run may make ${calls} paid calls, with no automatic retries. Custom outputs are not paper results.` : 'This free demo returns sample text. It does not analyse your input or produce paper results.';
  }
  $('run-summary').innerHTML = rows.map(([key, value]) => `<dt>${escapeText(key)}</dt><dd>${escapeText(value)}</dd>`).join('');
  $('run-note').textContent = note;
  $('run-badge').textContent = live ? 'Live API' : 'Free demo';
  $('start-run').textContent = live ? 'Start live run' : 'Run free demo';
  $('start-run').disabled = state.running || (state.mode === 'paper' && (!live || !state.paper?.readiness?.[paperConfig().ticker]?.ready));
  if (!state.running && state.result && state.lastRun) {
    const changed = state.lastRun.key !== settingsKey();
    $('progress-panel').hidden = changed;
    $('run-status').className = changed ? 'notice warning' : 'notice';
    $('run-status').textContent = changed ? `Last completed run: ${state.lastRun.name}. Current settings have not been run. You can still download the earlier result below.` : `Run complete: ${state.lastRun.name}. Your results are ready below.`;
  }
}

function download(name, value) {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2) + '\n'], {type: 'application/json'}));
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function setRunning(running) {
  state.running = running;
  $('start-run').disabled = running;
  $('stop-run').hidden = !running;
  $('stop-run').disabled = false;
  // Freeze configuration while a run is active, so the visible plan stays accurate.
  document.querySelectorAll('#step-1 input, #step-1 select, #step-2 input, #step-2 select, #step-2 textarea, #step-2 button, #mode-paper, #mode-custom, #try-demo').forEach(element => { element.disabled = running; });
  if (!running) { renderLayers(); renderSummary(); }
}

async function startRun() {
  clearError();
  let config;
  try {
    const conn = connection();
    if (conn.mode === 'live') {
      const endpoint = new URL(conn.endpoint);
      if (!conn.api_key && !['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname)) throw new Error('Add your API key in the Connection step.');
    }
    config = state.mode === 'paper' ? paperConfig() : readConfig();
    if (state.mode === 'paper' && (!config.start_date || !config.end_date || config.end_date < config.start_date)) throw new Error('Choose a start date and an end date in order.');
    setRunning(true);
    state.activeRun = {name: state.mode === 'paper' ? 'Paper workflow' : config.name, key: settingsKey()};
    state.result = null;
    $('results-panel').hidden = true;
    $('progress-panel').hidden = false;
    $('run-events').replaceChildren();
    $('progress-bar').value = 0;
    $('progress-count').textContent = '';
    $('run-status').hidden = false;
    $('run-status').className = 'notice';
    $('run-status').textContent = 'Starting the run…';
    const response = await api(state.mode === 'paper' ? '/api/paper-run' : '/api/run', {config, connection: conn});
    state.job = response.id;
    $('api-key').value = '';
    pollJob();
  } catch (error) { setRunning(false); showError(error.message); $('run-status').hidden = true; }
}

async function pollJob() {
  try {
    const job = await api(`/api/jobs/${state.job}`);
    const lastWindow = [...job.events].reverse().find(event => Number.isInteger(event.completed));
    const completed = job.paper ? (lastWindow?.completed || 0) : job.events.filter(event => event.kind === 'completed').length;
    const total = job.paper ? (lastWindow?.total || 1) : (job.events.find(event => event.total)?.total || 1);
    $('progress-bar').max = total;
    $('progress-bar').value = job.status === 'completed' ? total : completed;
    $('progress-bar').setAttribute('aria-label', job.paper ? 'Completed prediction windows' : 'Completed model calls');
    $('progress-count').textContent = `${completed} of ${total} ${job.paper ? 'windows' : 'calls'} complete`;
    $('run-events').replaceChildren(...job.events.slice(-30).map(event => { const item = document.createElement('li'); item.textContent = event.message || `${event.kind === 'completed' ? 'Done' : 'Running'}: ${event.role} · layer ${event.layer}, round ${event.round}`; return item; }));
    if (['queued', 'running'].includes(job.status)) { $('run-status').textContent = job.cancel ? 'Stopping at the next safe step. Active API calls may still finish and be charged.' : 'Run in progress. Keep this page open.'; setTimeout(pollJob, 750); return; }
    setRunning(false);
    if (job.status === 'failed') { $('run-status').textContent = job.paper ? 'The paper run stopped. Check the error before starting a new run.' : 'The run stopped. No automatic retry was made.'; $('run-status').className = 'notice warning'; return showError(job.error); }
    if (job.status === 'cancelled') { $('run-status').textContent = 'Run stopped. Completed and active API calls may still be charged.'; return; }
    state.result = job.result;
    state.lastRun = state.activeRun;
    $('run-status').textContent = 'Run complete. Your results are ready below.';
    renderResults();
    renderSummary();
  } catch (error) { setRunning(false); $('run-status').textContent = 'Connection to the local app was lost. A started run may still be active.'; showError(error.message); }
}

function renderResults() {
  const result = state.result;
  $('results-panel').hidden = false;
  $('results-title').textContent = `Last completed run: ${state.lastRun?.name || 'Workflow'}`;
  $('results').replaceChildren();
  const paper = result.mode === 'paper';
  $('results-note').textContent = paper ? (result.message || `Saved in ${result.output_directory}.`) : result.mode === 'demo' ? 'Demo output. No API call or evidence analysis was made.' : 'Custom experiment output. It is not a reproduction of the paper.';
  const outputs = paper ? [{role: 'Paper scores', output: result.rows}] : result.outputs || [];
  outputs.forEach(output => { const card = document.createElement('article'); card.className = 'card'; const title = document.createElement('h2'); title.textContent = output.role; const text = document.createElement('div'); text.className = 'result-output'; text.textContent = typeof output.output === 'string' ? output.output : JSON.stringify(output.output, null, 2); card.append(title, text); $('results').append(card); });
  $('trace').textContent = JSON.stringify(paper ? result.reports : result.trace, null, 2);
}

$('mode-paper').addEventListener('click', () => setMode('paper'));
$('mode-custom').addEventListener('click', () => setMode('custom'));
document.querySelectorAll('[data-step]').forEach(button => button.addEventListener('click', () => setStep(Number(button.dataset.step))));
document.querySelectorAll('[name="connection-mode"]').forEach(input => input.addEventListener('change', updateConnection));
$('connection-next').addEventListener('click', () => setStep(2));
$('design-next').addEventListener('click', () => { try { if (state.mode === 'custom') readConfig(); setStep(3); } catch (error) { showError(error.message); } });
$('try-demo').addEventListener('click', () => { setMode('custom'); document.querySelector('[name="connection-mode"][value="demo"]').checked = true; updateConnection(); if (!$('task-input').value.trim()) $('task-input').value = sampleTask; setStep(2); });
$('provider').addEventListener('change', event => { const value = event.target.value; $('endpoint').value = value === 'ollama' ? 'http://127.0.0.1:11434/v1' : value === 'openai' ? 'https://api.openai.com/v1' : ''; if (value !== 'compatible') { $('default-model').value = value === 'ollama' ? 'gemma4:e2b' : 'gpt-5-mini'; $('default-model').dispatchEvent(new Event('change')); } $('api-key').value = ''; if (value === 'compatible') $('endpoint').closest('details').open = true; });
$('default-model').addEventListener('change', () => { state.layers.forEach(layer => layer.agents.forEach(item => { item.model = model(); })); renderLayers(); });
document.querySelectorAll('[data-template]').forEach(button => button.addEventListener('click', () => { if (state.template !== button.dataset.template && window.confirm('Replace the current roles and layers with this template? Your task will stay.')) loadTemplate(button.dataset.template); }));
$('sample-input').addEventListener('click', () => { if (!$('task-input').value.trim() || window.confirm('Replace the current task with a sample task?')) $('task-input').value = sampleTask; });
$('add-layer').addEventListener('click', () => { if (state.layers.length < 6) state.layers.push({name: `Layer ${state.layers.length + 1}`, mode: 'parallel', rounds: 1, agents: [agent('Reviewer', 'Review the task and earlier replies. Give a clear answer based on the supplied evidence.')]}); renderLayers(); });
$('layers').addEventListener('input', event => { const layerElement = event.target.closest('[data-layer]'); if (!layerElement) return; const layer = state.layers[Number(layerElement.dataset.layer)]; const agentElement = event.target.closest('[data-agent]'); if (event.target.dataset.agentField && agentElement) layer.agents[Number(agentElement.dataset.agent)][event.target.dataset.agentField] = event.target.value; else if (event.target.dataset.field) layer[event.target.dataset.field] = event.target.dataset.field === 'rounds' ? Number(event.target.value) : event.target.value; });
$('layers').addEventListener('change', event => { if (event.target.dataset.field === 'mode') renderLayers(); });
$('layers').addEventListener('click', event => {
  const button = event.target.closest('[data-action]'); if (!button) return;
  const index = Number(button.closest('[data-layer]').dataset.layer), layer = state.layers[index], action = button.dataset.action;
  if (action === 'add-agent') layer.agents.push(agent(`Role ${layer.agents.length + 1}`, 'Review the task and available context. State your evidence and conclusion.'));
  const roleIndex = Number(button.closest('[data-agent]')?.dataset.agent);
  if (action === 'remove-agent') layer.agents.splice(roleIndex, 1);
  if (action === 'agent-up') [layer.agents[roleIndex - 1], layer.agents[roleIndex]] = [layer.agents[roleIndex], layer.agents[roleIndex - 1]];
  if (action === 'agent-down') [layer.agents[roleIndex + 1], layer.agents[roleIndex]] = [layer.agents[roleIndex], layer.agents[roleIndex + 1]];
  if (action === 'remove-layer') state.layers.splice(index, 1);
  if (action === 'up') [state.layers[index - 1], state.layers[index]] = [state.layers[index], state.layers[index - 1]];
  if (action === 'down') [state.layers[index + 1], state.layers[index]] = [state.layers[index], state.layers[index + 1]];
  renderLayers();
});
$('output-format').addEventListener('change', () => { $('schema-panel').hidden = $('output-format').value !== 'json'; });
$('export-config').addEventListener('click', () => { try { download('finder-workflow.json', {format: 'finder-studio-v1', workflow: readConfig(false)}); } catch (error) { showError(error.message); } });
$('import-config').addEventListener('click', () => $('import-file').click());
$('import-file').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file) return;
  try {
    if (file.size > 500000) throw new Error('Choose a configuration file smaller than 500 KB.');
    const data = JSON.parse(await file.text()), config = data.workflow;
    if (data.format !== 'finder-studio-v1' || !config || !Array.isArray(config.layers) || !config.layers.length || config.layers.length > 6) throw new Error('Choose a FINDER Studio configuration with 1–6 layers.');
    const layers = config.layers.map(layer => { if (!layer || !Array.isArray(layer.agents) || !layer.agents.length || layer.agents.length > 6 || !['parallel', 'sequential', 'debate'].includes(layer.mode)) throw new Error('Each layer needs 1–6 roles and a valid interaction mode.'); return {name: String(layer.name || 'Layer').slice(0, 100), mode: layer.mode, rounds: Number.isInteger(layer.rounds) && layer.rounds >= 1 && layer.rounds <= 5 ? layer.rounds : 1, agents: layer.agents.map(item => ({role: String(item.role || '').slice(0, 80), model: String(item.model || '').slice(0, 150), prompt: String(item.prompt || '').slice(0, 12000)}))}; });
    state.layers = layers;
    state.template = null;
    $('workflow-name').value = String(config.name || 'Imported workflow').slice(0, 100);
    $('task-input').value = String(config.task || '').slice(0, 100000);
    $('output-format').value = config.output === 'json' ? 'json' : 'text';
    $('output-schema').value = JSON.stringify(config.schema || defaultSchema, null, 2);
    $('max-tokens').value = Number.isInteger(config.max_tokens) ? config.max_tokens : 1500;
    $('schema-panel').hidden = $('output-format').value !== 'json';
    document.querySelectorAll('[data-template]').forEach(button => button.classList.remove('active'));
    renderLayers(); clearError();
  } catch (error) { showError(error instanceof SyntaxError ? 'This file is not valid JSON.' : error.message); }
  event.target.value = '';
});
$('start-run').addEventListener('click', startRun);
$('stop-run').addEventListener('click', async () => { try { await api('/api/cancel', {id: state.job}); $('stop-run').disabled = true; } catch (error) { showError(error.message); } });
$('download-results').addEventListener('click', () => { if (state.result) download(`finder-${state.result.mode}-${state.job}.json`, state.result); });
window.addEventListener('beforeunload', event => { if (state.running) { event.preventDefault(); event.returnValue = ''; } });
$('output-schema').value = JSON.stringify(defaultSchema, null, 2);
loadTemplate('review');
api('/api/paper-config').then(config => { state.paper = config; renderPaper(); }).catch(() => { $('paper-status').textContent = 'Paper settings could not be loaded. Check the local configuration files. Custom demo is still available.'; $('paper-status').classList.add('warning'); });
