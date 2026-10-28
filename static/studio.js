/* Vouch Studio front end.
 *
 * Every node is built with textContent (see h()), so job descriptions, profile
 * text and engine output can never inject markup. Only http(s) links render.
 * State-changing requests carry X-Vouch-Studio, which the server requires.
 */
'use strict';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key === 'style') for (const [p, v] of Object.entries(value)) el.style.setProperty(p, v);
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (key === 'value') el.value = value;
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, String(value));
  }
  return append(el, ...kids);
}

function append(el, ...kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

/** Small stroked SVG icons (bold weight, one stroke width), built without markup strings. */
const ICON_PATHS = {
  external: ['M8 16 16 8', 'M9.5 8H16v6.5'],
  check: ['M5 12.5 9.5 17 19 7.5'],
  cross: ['M7 7l10 10', 'M17 7 7 17'],
  arrow: ['M5 12h14', 'M13 6l6 6-6 6'],
};
function icon(name, size = 13) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  for (const [k, v] of Object.entries({ viewBox: '0 0 24 24', width: size, height: size, fill: 'none',
    stroke: 'currentColor', 'stroke-width': 2.25, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    'aria-hidden': 'true', class: 'icon' })) svg.setAttribute(k, String(v));
  for (const d of ICON_PATHS[name]) {
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

function clear(el, ...kids) { el.replaceChildren(); return append(el, ...kids); }
const $ = (sel, root = document) => root.querySelector(sel);

function safeUrl(raw) {
  if (!raw) return null;
  try {
    const url = new URL(raw.includes('://') ? raw : `https://${raw}`);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch { return null; }
}

function extLink(raw, label) {
  const href = safeUrl(raw);
  return href ? h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, label || raw) : (label || raw);
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = 'GET', body } = {}) {
  const options = { method, headers: {} };
  if (method !== 'GET') {
    options.headers['X-Vouch-Studio'] = '1';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body ?? {});
  }
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let detail = data.detail || res.statusText;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
    throw new ApiError(res.status, detail);
  }
  return data;
}

function toast(message, kind = '') {
  const el = h('div', { class: `toast ${kind}` }, message);
  $('#toasts').append(el);
  setTimeout(() => el.remove(), kind === 'fail' ? 7000 : 4000);
}

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

const STATUS_CHIP = {
  matched: 'pass', explicitly_no: 'fail', not_evidenced: '', ambiguous: 'warn',
  passed: 'pass', failed: 'fail', not_run: '',
};
const statusLabel = (s) => String(s || '').replace(/_/g, ' ');
const chip = (text, kind = '') => h('span', { class: `chip ${kind}` }, text);
const statusChip = (status) => chip(statusLabel(status), `tag ${STATUS_CHIP[status] ?? ''}`);

function lineChip(n, label) {
  return h('a', { class: 'chip link accent', href: `#/profile?line=${n}`, title: 'Open in Profile Studio' },
    label || `L${n}`);
}

function scoreBadge(job, inline = false) {
  const cls = inline ? ' inline' : '';
  if (job.insufficient) return h('span', { class: `score thin${cls}`, title: 'Fewer than 3 recognized skill requirements' }, 'Too little detail');
  if (job.score == null) return h('span', { class: `score thin${cls}`, title: 'No full description to score' }, job.complete === false ? 'Listing' : 'Not scored');
  const kind = job.score >= state.threshold ? 'proceed' : job.score >= 40 ? 'mid' : '';
  return h('span', { class: `score ${kind}${cls}` }, job.score,
    !inline && job.requirement_count != null ? h('small', {}, `${job.requirement_count} req${job.requirement_count === 1 ? '' : 's'}`) : null);
}

// ---------------------------------------------------------------------------
// App state, routing and the global task indicator
// ---------------------------------------------------------------------------
const state = {
  meta: null,
  threshold: 65,
  jobsQuery: { q: '', region: '', levels: [], source: '', verdict: '', sort: 'match', tailored: false },
  resume: null,           // { profile, threshold, scored, proceed, thin } from /api/resume
  jobsPage: { offset: 0, rows: [] },
  lastJob: null,
  editor: null,           // { kind, text, sha, dirty }
  streams: new Map(),     // task id -> EventSource
};

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, query] = raw.split('?');
  const parts = (path || 'jobs').split('/').map(decodeURIComponent);
  return { name: parts[0] || 'jobs', args: parts.slice(1), params: new URLSearchParams(query || '') };
}

async function route() {
  const { name, args, params } = parseHash();
  if (state.restoringHash) {  // we just undid a cancelled navigation: keep the open editor
    state.restoringHash = false;
    return;
  }
  if (state.editor?.dirty && name !== state.editor.kind) {
    if (!confirm('You have unsaved edits in Profile Studio. Leave without saving?')) {
      state.restoringHash = true;
      history.back();
      return;
    }
    state.editor.dirty = false;
  }
  if (!['profile', 'template'].includes(name)) state.editor = null;
  document.querySelectorAll('[data-nav]').forEach((a) => {
    const nav = a.dataset.nav;
    a.classList.toggle('active', nav === name || (nav === 'profile' && name === 'template'));
    if (nav === 'job' && state.lastJob) a.href = `#/job/${encodeURIComponent(state.lastJob)}`;
  });
  const view = $('#view');
  document.body.dataset.view = name;
  window.scrollTo(0, 0);
  try {
    if (name === 'job') {
      const id = args[0] || state.lastJob;
      if (!id) { clear(view, emptyState('Pick a job first', 'Open a job from the Jobs list to tailor it.')); return; }
      await renderWorkspace(view, id, args[1] || 'fit');
    } else if (name === 'resume') {
      await renderResume(view);
    } else if (name === 'profile' || name === 'template') {
      await renderStudio(view, name, Number(params.get('line')) || null);
    } else {
      await renderJobs(view);
    }
  } catch (err) {
    clear(view, emptyState('Something went wrong', err.message));
  }
}

function emptyState(title, text) {
  return h('div', { class: 'panel empty' }, h('strong', {}, title), text);
}

async function loadMeta() {
  state.meta = await api('/api/meta');
  state.threshold = state.meta.threshold;
  const tools = $('#tools');
  clear(tools, ['pdflatex', 'pdftotext', 'pdfinfo'].map((name) =>
    h('span', { class: `chip ${state.meta.tools[name] ? 'pass' : 'fail'}` },
      icon(state.meta.tools[name] ? 'check' : 'cross', 11), name)));
  const active = state.meta.active_task;
  if (active && active.status === 'running') showTaskPill(active);
}

function showTaskPill(task) {
  const pill = $('#task-pill');
  if (!task || task.status !== 'running') { pill.classList.add('hidden'); return; }
  clear(pill, h('span', { class: 'spinner' }), task.label);
  pill.classList.remove('hidden');
  pill.onclick = () => {
    if (task.kind === 'tailor' && task.job_id) location.hash = `#/job/${encodeURIComponent(task.job_id)}`;
    else openFetchDialog(task);
  };
}

/** Stream a task's log into a console element; resolves with the finished task. */
function streamTask(task, consoleEl, { onLine } = {}) {
  showTaskPill(task);
  if (consoleEl) clear(consoleEl);
  return new Promise((resolve) => {
    const source = new EventSource(`/api/tasks/${task.id}/events`);
    state.streams.set(task.id, source);
    source.addEventListener('log', (event) => {
      const line = JSON.parse(event.data);
      if (consoleEl) {
        const stick = consoleEl.scrollTop + consoleEl.clientHeight >= consoleEl.scrollHeight - 30;
        consoleEl.append(h('div', { class: consoleClass(line) }, line || ' '));
        if (stick) consoleEl.scrollTop = consoleEl.scrollHeight;
      }
      onLine?.(line);
    });
    source.addEventListener('done', (event) => {
      source.close();
      state.streams.delete(task.id);
      const finished = JSON.parse(event.data);
      showTaskPill(null);
      resolve(finished);
    });
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        state.streams.delete(task.id);
        showTaskPill(null);
        resolve({ ...task, status: 'failed', outcome: 'connection lost' });
      }
    };
  });
}

function consoleClass(line) {
  if (line.startsWith('$ ')) return 'l-cmd';
  if (/^\[(pdflatex|xelatex|lualatex) #\d+\] compiling/.test(line)) return 'l-compile';
  if (/exit 0\b|passed|Resume: |verified/.test(line)) return 'l-ok';
  if (/ERROR|error|failed|exit [1-9]|REJECT/.test(line)) return 'l-err';
  return '';
}

// ---------------------------------------------------------------------------
// Jobs view: the board (stats, filters, every job) ranked against one profile
// ---------------------------------------------------------------------------
const LEVELS = ['Intern', 'New grad', 'Junior', 'Mid or unspecified', 'Senior', 'Staff and principal',
  'Manager', 'Director and above'];
const GAP_LABEL = { explicitly_no: 'no', not_evidenced: 'missing', ambiguous: 'unclear' };

const revealer = 'IntersectionObserver' in window ? new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (entry.isIntersecting) { entry.target.classList.add('in'); revealer.unobserve(entry.target); }
  }
}, { rootMargin: '0px 0px -40px 0px' }) : null;

/** Fade a block in as it enters the viewport; `index` staggers siblings. */
function reveal(node, index = 0) {
  node.classList.add('reveal');
  node.style.setProperty('--index', String(Math.min(index, 8)));
  if (revealer) revealer.observe(node); else node.classList.add('in');
  return node;
}

function profileLine(profile) {
  if (!profile) return 'Loading the profile…';
  if (profile.error) return `Profile problem: ${profile.error}`;
  const mode = profile.mode === 'master' ? 'master profile' : 'plain resume';
  return `${profile.filename} · ${mode} · ${profile.evidence_lines} evidence lines`;
}

async function renderJobs(view) {
  const query = state.jobsQuery;
  const search = h('input', { type: 'search', placeholder: 'Title, company or city', value: query.q,
    'aria-label': 'Search jobs' });
  const select = (key, label, options) => {
    const el = h('select', { 'aria-label': label },
      h('option', { value: '' }, label),
      options.map(([value, text]) => h('option', { value }, text)));
    el.value = query[key] || '';
    el.addEventListener('change', () => { query[key] = el.value; reload(); });
    return el;
  };
  const sort = h('select', { 'aria-label': 'Sort' },
    [['match', 'Best match'], ['score', 'Fit score'], ['recent', 'Newest posting'],
      ['tailored', 'Recently tailored'], ['company', 'Company A-Z']].map(([v, t]) => h('option', { value: v }, t)));
  sort.value = query.sort;
  sort.addEventListener('change', () => { query.sort = sort.value; reload(); });
  const tailored = h('input', { type: 'checkbox' });
  tailored.checked = query.tailored;
  tailored.addEventListener('change', () => { query.tailored = tailored.checked; reload(); });
  search.addEventListener('input', debounce(() => { query.q = search.value; reload(); }, 250));
  const sourceSelect = select('source', 'All sources', []);

  const banner = h('div', { class: 'banner' });
  const bento = h('div', { class: 'bento' });
  const levelRow = h('div', { class: 'level-row', role: 'group', 'aria-label': 'Filter by level' });
  const count = h('span', { class: 'count', 'aria-live': 'polite' });
  const summary = h('p', { class: 'jobs-summary' });
  const list = h('div', { class: 'panel job-list' }, h('div', { class: 'empty' }, 'Loading jobs…'));
  const more = h('div', { class: 'load-more' });
  const foot = h('footer', { class: 'foot' });

  clear(view,
    reveal(h('header', { class: 'page-head' },
      h('p', { class: 'eyebrow' }, 'Vouch · job board'),
      h('h1', {}, 'Every open role, scored against your experience.'),
      h('p', { class: 'lede' }, 'Browse every role and level across the US and Canada. Each job gets an ',
        'evidence-based fit score; open one to tailor a verified one-page resume for it.'))),
    reveal(banner, 1),
    bento,
    h('div', { class: 'section-head' }, h('h2', {}, 'All jobs'), count, h('span', { class: 'spacer' }),
      h('button', { class: 'btn', type: 'button', onclick: () => openUploadDialog() }, 'Add job'),
      h('button', { class: 'btn primary', type: 'button', onclick: () => openFetchDialog() }, 'Fetch jobs')),
    h('div', { class: 'toolbar' },
      search,
      select('verdict', 'Any status', [['proceed', `At or above ${state.threshold}`], ['low_fit', 'Below threshold'],
        ['thin', 'Not enough detail'], ['listing', 'Listing only']]),
      select('region', 'Anywhere', [['US', 'US'], ['Canada', 'Canada']]),
      sourceSelect,
      sort,
      h('label', { class: 'check' }, tailored, 'Tailored only')),
    levelRow, summary, list, more, foot);

  const toggleLevel = (name) => {
    query.levels = query.levels.includes(name) ? query.levels.filter((l) => l !== name)
      : [...query.levels, name];
    reload();
  };

  function renderBanner(profile) {
    const upload = profile?.source === 'upload';
    clear(banner,
      h('p', {}, h('span', { class: 'src' }, upload ? 'Ranked against your upload' : 'Ranked against your master profile'),
        profileLine(profile)),
      upload ? h('button', { class: 'btn ghost', type: 'button', onclick: async () => {
        await clearUploadedResume(); reload();
      } }, 'Use master profile') : null,
      h('a', { class: 'btn', href: '#/resume' }, upload ? 'Change resume' : 'Upload a resume'));
  }

  function renderStats(data) {
    const v = data.facets.verdict;
    const regionCount = (name) => Object.entries(data.facets.region)
      .filter(([r]) => r.includes(name)).reduce((sum, [, n]) => sum + n, 0);
    const levelCounts = data.facets.level;
    const stat = (value, label, cls, i) => reveal(h('div', { class: `card ${cls}` },
      h('p', { class: 'stat-big' }, value), h('p', { class: 'stat-label' }, label)), i);
    clear(bento,
      stat(data.total.toLocaleString(), 'Jobs on the board', 'span-2', 0),
      stat(((v.proceed || 0) + (v.low_fit || 0)).toLocaleString(),
        `With enough detail to score (${state.meta?.min_skill_requirements || 3}+ recognized skills)`, 'span-2', 1),
      stat((v.proceed || 0).toLocaleString(), `At or above the ${state.threshold} fit threshold`, 'span-2 wide', 2),
      reveal(h('div', { class: 'card span-3' }, h('h3', {}, 'Regions'),
        h('div', { class: 'split' },
          h('div', {}, h('p', { class: 'stat-big' }, regionCount('US').toLocaleString()), h('p', { class: 'stat-label' }, 'United States')),
          h('div', {}, h('p', { class: 'stat-big' }, regionCount('Canada').toLocaleString()), h('p', { class: 'stat-label' }, 'Canada')))), 3),
      reveal(h('div', { class: 'card span-3' }, h('h3', {}, 'Levels'),
        h('ul', { class: 'levels' }, LEVELS.map((name) => h('li', {},
          h('button', { type: 'button', 'aria-pressed': String(query.levels.includes(name)),
            onclick: () => toggleLevel(name) },
          h('span', {}, name), h('span', {}, (levelCounts[name] || 0).toLocaleString())))))), 4));
    clear(levelRow, h('span', { class: 'label' }, 'Level'),
      LEVELS.filter((name) => levelCounts[name]).map((name) => h('button', {
        class: 'level-chip', type: 'button', 'aria-pressed': String(query.levels.includes(name)),
        onclick: () => toggleLevel(name) }, name, h('span', { class: 'n' }, levelCounts[name]))),
      query.levels.length ? h('button', { class: 'btn ghost small', type: 'button',
        onclick: () => { query.levels = []; reload(); } }, 'Clear') : null);
  }

  async function reload(append = false) {
    const offset = append ? state.jobsPage.rows.length : 0;
    const params = new URLSearchParams({ sort: query.sort, offset, limit: 40 });
    for (const key of ['q', 'region', 'source', 'verdict']) if (query[key]) params.set(key, query[key]);
    if (query.levels.length) params.set('level', query.levels.join(','));
    if (query.tailored) params.set('tailored', 'true');
    const data = await api(`/api/jobs?${params}`);
    state.jobsPage.rows = append ? state.jobsPage.rows.concat(data.jobs) : data.jobs;
    if (!append) {
      const current = query.source;
      clear(sourceSelect, h('option', { value: '' }, 'All sources'),
        Object.entries(data.facets.source).map(([s, n]) => h('option', { value: s }, `${s} (${n})`)));
      sourceSelect.value = current;
      renderBanner(data.profile);
      renderStats(data);
    }
    count.textContent = `${state.jobsPage.rows.length.toLocaleString()} of ${data.matching.toLocaleString()} shown`;
    clear(summary, data.profile?.error ? chip(`Profile: ${data.profile.error}`, 'fail') : null);
    renderRows(list, state.jobsPage.rows, append);
    clear(more, state.jobsPage.rows.length < data.matching
      ? h('button', { class: 'btn', type: 'button', onclick: () => reload(true) },
        `Show more (${(data.matching - state.jobsPage.rows.length).toLocaleString()} left)`)
      : null);
    clear(foot, h('span', {}, `Index built ${String(data.index_generated || '').replace('T', ' ')} · ${data.total.toLocaleString()} jobs`),
      h('span', {}, 'Fetch jobs adds new postings and refreshes the index'));
  }
  state.reloadJobs = reload;
  await reload();
}

function regionChip(region) {
  return chip(region, region.includes('US') ? 'accent tag' : region.includes('Canada') ? 'warn tag' : 'tag');
}

function renderRows(list, rows, keepScroll) {
  if (!rows.length) {
    clear(list, emptyState('No jobs match these filters', 'Clear a filter, or fetch new postings.'));
    return;
  }
  const scrollY = window.scrollY;
  clear(list, rows.map((job, i) => {
    const open = () => { location.hash = `#/job/${encodeURIComponent(job.id)}`; };
    const scored = job.score != null && !job.insufficient;
    const pass = scored && job.score >= state.threshold;
    const tags = h('div', { class: 'tags' },
      scored ? chip(pass ? `${state.threshold}+` : `Below ${state.threshold}`, `${pass ? 'pass' : 'fail'} tag`)
        : job.insufficient ? chip('Not enough detail', 'tag') : !job.complete ? chip('Listing only', 'tag') : null,
      job.region ? regionChip(job.region) : null, chip(job.level, 'tag'), chip(job.source, 'tag'));
    let detail = null;
    if (scored) {
      const matched = job.matched.map((m) => h('span', { class: 'chip pass', title: m }, h('span', { class: 'clip' }, m)));
      const gaps = job.gaps.map(([name, status]) => h('span', { class: `chip ${status === 'explicitly_no' ? 'fail' : ''}`, title: name },
        h('span', { class: 'clip' }, name), h('em', {}, GAP_LABEL[status] || status)));
      detail = h('div', { class: 'detail' },
        h('div', {}, h('h4', {}, 'Evidence matched'), h('div', { class: 'chips' }, matched.length ? matched : h('span', { class: 'faint small' }, 'None'))),
        h('div', {}, h('h4', {}, 'Gaps'), h('div', { class: 'chips' }, gaps.length ? gaps : h('span', { class: 'faint small' }, 'None'))));
    } else if (job.insufficient) {
      detail = h('p', { class: 'faint small', style: { margin: '10px 0 0' } },
        `Only ${job.skill_requirements} recognized skill requirement${job.skill_requirements === 1 ? '' : 's'}; a score needs ${state.meta?.min_skill_requirements || 3}. Read the posting to judge fit.`);
    } else if (!job.complete) {
      detail = h('p', { class: 'faint small', style: { margin: '10px 0 0' } },
        'This source lists the job without a description, so it cannot be scored.');
    }
    const t = job.tailored;
    const posting = safeUrl(job.url);
    return reveal(h('article', { class: 'job-row', role: 'link', tabindex: 0, onclick: open,
      onkeydown: (e) => { if (e.key === 'Enter') open(); } },
    scoreBadge(job),
    h('div', { class: 'grow' },
      h('div', { class: 'title' }, job.title),
      h('div', { class: 'sub' }, [job.company || 'Unknown company', job.location, job.posted ? `posted ${job.posted}` : null]
        .filter(Boolean).join(' · ')),
      tags, detail),
    h('div', { class: 'side-col' },
      t ? chip(t.resume_status === 'verified' ? 'Verified PDF' : statusLabel(t.resume_status || t.verdict),
        `tag ${t.resume_status === 'verified' ? 'pass' : t.verdict === 'PROCEED' ? 'warn' : ''}`) : null,
      posting ? h('a', { href: posting, target: '_blank', rel: 'noopener noreferrer', class: 'small',
        onclick: (e) => e.stopPropagation() }, 'Posting', icon('external')) : null)), i % 8);
  }));
  if (keepScroll) window.scrollTo(0, scrollY);
}

// ---------------------------------------------------------------------------
// Your resume: rank the board against an upload (memory only) or the master
// ---------------------------------------------------------------------------
const RESUME_KEY = 'vouch.resume.v1';
const MAX_REMEMBER_CHARS = 2_800_000;
const store = {
  get() { try { return JSON.parse(localStorage.getItem(RESUME_KEY)); } catch { return null; } },
  set(value) { try { localStorage.setItem(RESUME_KEY, JSON.stringify(value)); return true; } catch { return false; } },
  clear() { try { localStorage.removeItem(RESUME_KEY); } catch { /* storage unavailable */ } },
};

function bufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

async function uploadResume(filename, contentBase64) {
  state.resume = await api('/api/resume', { method: 'POST', body: { filename, content_base64: contentBase64 } });
  return state.resume;
}

async function clearUploadedResume() {
  store.clear();
  state.resume = await api('/api/resume/clear', { method: 'POST' });
  toast('Ranking against your master profile again');
}

/** On load, re-send a remembered upload when the server (restarted) no longer has it. */
async function restoreResume() {
  state.resume = await api('/api/resume');
  const saved = store.get();
  if (state.resume.profile?.source !== 'upload' && saved?.filename && saved?.content_base64) {
    try { await uploadResume(saved.filename, saved.content_base64); } catch { store.clear(); }
  }
}

async function renderResume(view) {
  state.resume = await api('/api/resume');
  const status = h('p', { class: 'muted small', role: 'status' });
  const remember = h('input', { type: 'checkbox' });
  remember.checked = Boolean(store.get()) || state.resume.profile?.source !== 'upload';
  const fileInput = h('input', { type: 'file', accept: '.md,.txt,.pdf' });
  const paste = h('textarea', { rows: 7, placeholder: 'Paste your resume text here' });
  const profileCard = h('div', { class: 'card' });

  const submit = async (filename, contentBase64) => {
    status.textContent = `Scoring every job against ${filename}…`;
    try {
      const result = await uploadResume(filename, contentBase64);
      if (remember.checked) {
        const ok = contentBase64.length < MAX_REMEMBER_CHARS && store.set({ filename, content_base64: contentBase64 });
        if (!ok) toast('Scored, but this browser could not remember the file.', 'fail');
      } else {
        store.clear();
      }
      status.textContent = `Scored ${result.scored.toLocaleString()} jobs; ${result.proceed.toLocaleString()} at or above ${result.threshold}. `
        + `${result.thin.toLocaleString()} had too little detail to score.`;
      renderProfile();
    } catch (err) {
      status.textContent = err.message;
    }
  };
  const handleFile = async (file) => {
    if (!file) return;
    if (file.size > 2_000_000) { status.textContent = 'That file is larger than 2 MB.'; return; }
    submit(file.name, bufferToBase64(await file.arrayBuffer()));
  };
  fileInput.addEventListener('change', () => handleFile(fileInput.files[0]));
  const drop = h('label', { class: 'drop' }, fileInput,
    h('span', { class: 'drop-title' }, 'Drop a file here, or choose one'),
    h('span', { class: 'drop-sub' }, 'Master profile (.md), plain resume (.md or .txt), or PDF. Up to 2 MB.'));
  for (const type of ['dragenter', 'dragover']) drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add('over'); });
  for (const type of ['dragleave', 'drop']) drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.remove('over'); });
  drop.addEventListener('drop', (e) => handleFile(e.dataTransfer.files[0]));

  function renderProfile() {
    const p = state.resume.profile || {};
    const upload = p.source === 'upload';
    clear(profileCard,
      h('div', { class: 'row' }, h('h2', { class: 'grow' }, upload ? 'Your upload' : 'Your master profile'),
        chip(p.mode === 'master' ? 'Master profile' : 'Plain resume', `tag ${p.mode === 'master' ? 'pass' : 'warn'}`)),
      h('p', { class: 'profile-file' }, p.filename || '—'),
      h('p', { class: 'stat-label' }, `${p.evidence_lines ?? 0} evidence lines · ${(p.skills || []).length} recognized skills · `
        + `${state.resume.proceed.toLocaleString()} jobs at or above ${state.resume.threshold}`),
      (p.warnings || []).length ? h('div', { class: 'notice warn small' }, p.warnings.join(' ')) : null,
      p.error ? h('div', { class: 'notice fail small' }, p.error) : null,
      h('h3', { style: { margin: '20px 0 8px' } }, 'Skills with evidence'),
      h('div', { class: 'row' }, (p.skills || []).length ? p.skills.map((s) => chip(s, 'pass'))
        : h('span', { class: 'faint small' }, 'No recognized skills')),
      (p.no_skills || []).length ? [h('h3', { style: { margin: '20px 0 8px' } }, 'Marked [NO]'),
        h('div', { class: 'row' }, p.no_skills.map((s) => chip(s, 'fail')))] : null,
      h('div', { class: 'row', style: { 'margin-top': '24px' } },
        h('a', { class: 'btn primary', href: '#/jobs' }, 'See your matches'),
        upload ? h('button', { class: 'btn ghost', type: 'button', onclick: async () => {
          await clearUploadedResume(); renderProfile(); status.textContent = '';
        } }, 'Forget my upload') : null),
      h('p', { class: 'faint small', style: { margin: '16px 0 0' } },
        'Tailoring and the skill gate in the Workspace always use your master profile.'));
  }

  const faq = (q, a) => h('details', {}, h('summary', {}, q), h('p', {}, a));
  clear(view,
    reveal(h('header', { class: 'page-head' },
      h('p', { class: 'eyebrow' }, 'Vouch · your resume'),
      h('h1', {}, 'See every job through your own evidence.'),
      h('p', { class: 'lede' }, 'The board ranks jobs against your master profile. Upload a different resume to see how it ',
        'would rank them instead; nothing about it is saved on the server.'))),
    h('div', { class: 'resume-grid' },
      reveal(h('div', { class: 'card' },
        h('h2', {}, 'Upload'),
        drop,
        h('p', { class: 'or' }, 'or paste the text'),
        paste,
        h('div', { class: 'row', style: { 'margin-top': '12px' } },
          h('button', { class: 'btn primary', type: 'button', onclick: () => {
            const text = paste.value.trim();
            if (!text) { status.textContent = 'Paste your resume text first.'; return; }
            submit('pasted-resume.txt', bufferToBase64(new TextEncoder().encode(text)));
          } }, 'Score pasted text'),
          h('label', { class: 'check' }, remember, 'Remember on this device')),
        status), 1),
      reveal(profileCard, 2)),
    reveal(h('section', { class: 'faq' },
      h('h2', {}, 'How it works'),
      faq('What the fit score means', 'Each job\'s requirements are read from its description and weighted 3 for must-haves, '
        + '2 for required and 1 for nice-to-haves. The score is the share of that weight your profile backs with evidence. '
        + 'It measures coverage of what the posting asks for, not your chance of being hired.'),
      faq('Master profile versus plain resume', 'A master profile tags each line as verified or [NO], so skills you have '
        + 'ruled out never count. A plain resume has no tags: every line counts as your own claim, which makes the score '
        + 'less strict. The card shows which mode was used.'),
      faq('Why some jobs say "not enough detail"', 'A score needs at least three skill requirements the parser recognizes. '
        + 'Many sales, finance and operations postings name only a degree or terms outside the skill vocabulary, and a '
        + 'score built on one match would be misleading, so those jobs are listed without one.'),
      faq('Why "Best match" can differ from the score', 'Some postings yield only one or two recognizable requirements, so '
        + 'a 100 can rest on very little. Best match discounts scores built on few requirements; the score itself is shown '
        + 'unchanged on every job.'),
      faq('Where your resume goes', 'It is sent to Vouch Studio on your own computer and parsed in memory. It is never '
        + 'written to disk or logged, and it is dropped when you forget it or restart the server. With "Remember on this '
        + 'device" checked, a copy stays in this browser so the ranking survives a restart.')), 3));
  renderProfile();
}

// ---------------------------------------------------------------------------
// Fetch and upload dialogs
// ---------------------------------------------------------------------------
function wireDialogs() {
  for (const dialog of document.querySelectorAll('dialog')) {
    dialog.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => dialog.close()));
  }
  const source = $('#fetch-source');
  const syncSpend = () => {
    const paid = source.value === 'jobright' || source.value === 'all';
    $('#fetch-spend-wrap').classList.toggle('hidden', !paid);
    if (!paid) $('#fetch-spend').checked = false;
  };
  source.addEventListener('change', syncSpend);
  syncSpend();
  $('#fetch-start').addEventListener('click', startFetch);
  $('#upload-save').addEventListener('click', saveUpload);
  $('#upload-file').addEventListener('change', async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    if (file.size > 1_000_000) { toast('Job descriptions are limited to 1 MB', 'fail'); return; }
    $('#upload-text').value = await file.text();
  });
}

function openFetchDialog(task) {
  const dialog = $('#fetch-dialog');
  if (!dialog.open) dialog.showModal();
  if (task && task.status === 'running' && !state.streams.has(task.id)) followFetch(task);
}

async function startFetch() {
  const source = $('#fetch-source').value;
  const spend = $('#fetch-spend').checked;
  if (source === 'jobright' && !spend) {
    toast('Jobright is billable: tick "Allow Apify spend" to confirm.', 'fail');
    return;
  }
  if (spend && !confirm('This starts a billable Apify run (capped by maxTotalChargeUsd). Continue?')) return;
  const limit = $('#fetch-limit').value;
  const body = {
    source,
    query: $('#fetch-query').value.trim() || null,
    location: $('#fetch-location').value.trim() || null,
    country: $('#fetch-country').value.trim() || null,
    limit: limit ? Number(limit) : null,
    allow_apify_spend: spend,
    dry_run: $('#fetch-dry').checked,
    refresh: $('#fetch-refresh').checked,
  };
  try {
    const task = await api('/api/jobs/fetch', { method: 'POST', body });
    await followFetch(task);
  } catch (err) {
    toast(err.message, 'fail');
  }
}

async function followFetch(task) {
  const button = $('#fetch-start');
  button.disabled = true;
  $('#fetch-status').textContent = 'Running…';
  const done = await streamTask(task, $('#fetch-console'));
  button.disabled = false;
  const result = done.result || {};
  $('#fetch-status').textContent = `Finished: ${done.outcome}` +
    (result.new_jobs != null ? ` · ${result.new_jobs >= 0 ? '+' : ''}${result.new_jobs} jobs in the index` : '');
  toast(`Fetch finished (${done.outcome})`, done.status === 'done' ? '' : 'fail');
  if (parseHash().name === 'jobs' && state.reloadJobs) state.reloadJobs();
  loadMeta();
}

function openUploadDialog() {
  $('#upload-status').textContent = '';
  $('#upload-dialog').showModal();
}

async function saveUpload() {
  const text = $('#upload-text').value;
  if (!text.trim()) { $('#upload-status').textContent = 'Paste or load a description first.'; return; }
  const file = $('#upload-file').files[0];
  try {
    const saved = await api('/api/jobs/upload', { method: 'POST', body: {
      text, filename: file ? file.name : null,
      company: $('#upload-company').value.trim() || null, role: $('#upload-role').value.trim() || null,
    } });
    $('#upload-dialog').close();
    for (const id of ['#upload-text', '#upload-company', '#upload-role', '#upload-file']) $(id).value = '';
    toast(`Saved target_jobs/${saved.file}`);
    location.hash = `#/job/${encodeURIComponent(saved.id)}`;
  } catch (err) {
    $('#upload-status').textContent = err.message;
  }
}

// ---------------------------------------------------------------------------
// Workspace: tailoring, verdict, build gates, PDF and result tabs
// ---------------------------------------------------------------------------
const TABS = [['fit', 'Fit report'], ['requirements', 'Skill gate'], ['provenance', 'Provenance'],
  ['gap', 'Gap report'],
  ['briefing', 'Interview briefing'], ['description', 'Job description']];

async function renderWorkspace(view, jobId, tab) {
  const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
  state.lastJob = jobId;
  document.querySelector('[data-nav="job"]').href = `#/job/${encodeURIComponent(jobId)}`;
  const ctx = { job, jobId, tab, results: job.results, cache: {} };

  const consoleEl = h('div', { class: 'console' }, h('span', { class: 'console-empty' },
    'Run tailoring to stream pipeline.py and every pdflatex compile here.'));
  const options = {
    override_low_fit: h('input', { type: 'checkbox' }),
    analyze_gap: h('input', { type: 'checkbox' }),
    strict_no: h('input', { type: 'checkbox' }),
  };
  const runButton = h('button', { class: 'btn primary', type: 'button' }, 'Run tailoring');
  const summaryBox = h('div');
  const gatesBox = h('div');
  const pdfBox = h('div', { class: 'panel' });
  const tabBody = h('div', { class: 'tab-body' });
  const tabBar = h('div', { class: 'tabs', role: 'tablist' });

  ctx.refresh = () => {
    renderSummary(summaryBox, ctx);
    renderGates(gatesBox, ctx);
    renderPdf(pdfBox, ctx);
    renderTab(tabBody, ctx);
  };
  ctx.selectTab = (name) => {
    ctx.tab = name;
    history.replaceState(null, '', `#/job/${encodeURIComponent(jobId)}/${name}`);
    tabBar.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    renderTab(tabBody, ctx);
  };
  append(tabBar, TABS.map(([name, label]) => h('button', { type: 'button', role: 'tab', dataset: { tab: name },
    class: name === tab ? 'active' : '', onclick: () => ctx.selectTab(name) }, label)));

  const follow = async (task) => {
    runButton.disabled = true;
    runButton.textContent = 'Running…';
    const done = await streamTask(task, consoleEl);
    runButton.disabled = false;
    runButton.textContent = 'Run tailoring';
    ctx.results = done.result || (await api(`/api/tailor/${encodeURIComponent(jobId)}`));
    ctx.cache = {};
    ctx.refresh();
    toast(`Tailoring finished: ${done.outcome}`, done.status === 'done' ? '' : 'fail');
  };
  runButton.addEventListener('click', async () => {
    const body = Object.fromEntries(Object.entries(options).map(([k, el]) => [k, el.checked]));
    try {
      follow(await api(`/api/tailor/${encodeURIComponent(jobId)}`, { method: 'POST', body }));
    } catch (err) { toast(err.message, 'fail'); }
  });

  const board = job.board_score;
  const posting = safeUrl(job.url);
  clear(view,
    reveal(h('div', { class: 'job-header' },
      h('div', { class: 'grow' },
        h('h1', {}, job.title),
        h('div', { class: 'meta' },
          h('span', {}, job.company || 'Unknown company'), '·', h('span', {}, job.location || ''),
          chip(job.source || 'Manual'), job.level ? chip(job.level) : null,
          job.domain ? chip(job.domain, 'accent') : null,
          posting ? h('a', { href: posting, target: '_blank', rel: 'noopener noreferrer' }, 'Open posting', icon('external')) : null,
          h('span', { class: 'mono faint' }, `target_jobs/${job.file}`))),
      board && !board.insufficient
        ? h('div', { class: 'muted small nowrap' }, 'Skill gate ', scoreBadge(board, true)) : null)),
    reveal(h('div', { class: 'workspace' },
      h('div', { class: 'stack' },
        h('section', { class: 'panel run-panel' },
          h('div', { class: 'panel-head' }, h('h2', { class: 'grow' }, 'Tailor and build'), runButton),
          h('div', { class: 'panel-body stack' },
            h('div', { class: 'options' },
              h('label', { class: 'check', title: 'Builds a resume below 65; the verdict stays REJECT - LOW FIT' },
                options.override_low_fit, 'Build even if low fit'),
              h('label', { class: 'check', title: 'Write the gap report even when the verdict is PROCEED' },
                options.analyze_gap, 'Always write gap report'),
              h('label', { class: 'check', title: 'Every [NO] mention becomes global' },
                options.strict_no, 'Strict [NO]')),
            summaryBox,
            consoleEl)),
        h('section', {}, gatesBox),
        h('section', { class: 'panel' }, tabBar, tabBody)),
      h('aside', { class: 'side' }, pdfBox)), 1));
  ctx.refresh();
  if (job.active_task && job.active_task.status === 'running') follow(job.active_task);
}

function fitCard(label, value, sub, kind) {
  return h('div', { class: `fit-card ${kind}` },
    h('div', { class: 'fit-label' }, label),
    h('div', { class: 'fit-value' }, value == null ? 'n/a' : value.toFixed(1), h('span', {}, '/10')),
    sub ? h('div', { class: 'fit-sub' }, sub) : null);
}

function renderSummary(box, ctx) {
  const r = ctx.results;
  if (!r || !r.available) {
    clear(box, h('div', { class: 'notice' }, 'Not tailored yet. Running it scores the job against your master profile, builds and verifies the PDF when the skill gate is at least ',
      String(state.threshold), ', and writes the fit report, provenance map, gap report and briefing.'));
    return;
  }
  const proceed = r.verdict === 'PROCEED';
  const resume = r.resume || {};
  const fit = r.fit;
  const fitBlock = fit ? h('div', { class: 'stack', style: { gap: '8px' } },
    h('div', { class: 'fit-cards' },
      fitCard('Profile fit', fit.profile_fit, fit.profile_fit == null ? 'not scorable'
        : fit.profile_fit_with_hard_requirements !== fit.profile_fit
          ? `${fit.profile_fit_with_hard_requirements.toFixed(1)} with hard requirements` : 'what your verified facts support',
        fit.profile_fit == null ? '' : fit.profile_fit >= 7 ? 'good' : fit.profile_fit >= 5 ? 'mid' : 'low'),
      fitCard('Resume fit', fit.resume_fit, fit.resume_fit == null ? 'no resume built'
        : fit.underselling ? 'undersells your profile' : 'what this PDF shows',
        fit.resume_fit == null ? '' : fit.underselling ? 'low' : 'good')),
    h('div', { class: 'row' },
      (fit.hard_requirement_flags || []).map((flag) => chip(flag.replace('_', ' '),
        flag.endsWith('not_met') ? 'fail' : 'warn')),
      fit.core_requirement_unmet ? chip('a core requirement has no evidence', 'warn') : null),
    h('div', { class: 'muted small' }, 'Work authorization: ', fit.work_authorization_note),
    r.role_family ? h('div', { class: 'muted small' }, 'Role family: ',
      h('strong', {}, r.role_family.code), ` (${r.role_family.name}), `,
      `${Math.round(r.role_family.confidence * 100)}% by ${r.role_family.method}`,
      r.files && r.files['report.md'] ? [' · ', h('a', {
        href: `/api/files/${encodeURIComponent(ctx.jobId)}/report.md`, target: '_blank',
        rel: 'noopener' }, 'Run report', icon('external'))] : null) : null)
    : h('div', { class: 'notice small' }, 'This report predates profile and resume fit. Re-run tailoring.');
  clear(box, fitBlock, h('div', { class: 'verdict gate-line' },
    h('span', { class: `verdict-badge ${proceed ? 'proceed' : 'reject'}` }, proceed ? 'PROCEED' : 'LOW FIT'),
    h('span', { class: 'muted small grow' }, `Skill-coverage gate ${r.fit_score} (builds at ${r.threshold}) · `
      + `${r.primary_domain || 'domain unknown'} · resume `,
    statusChip(resume.status === 'verified' ? 'passed' : resume.status || 'not_run'),
    resume.override_low_fit ? ' built with override' : '',
    ` · last run ${r.updated.replace('T', ' ')}`)));
}

function renderGates(box, ctx) {
  const gates = ctx.results?.resume?.build_gates;
  if (!gates) {
    clear(box, ctx.results?.available && ctx.results.verdict === 'PROCEED'
      ? h('div', { class: 'notice' }, 'No build-gate data in this report (it predates the gates). Re-run tailoring.')
      : null);
    return;
  }
  const g1 = gates.gate_1_syntax;
  const g2 = gates.gate_2_single_page;
  const g3 = gates.gate_3_ats_readability;
  const g4 = gates.gate_4_layout || { status: 'not_run' };
  const log = ctx.results.log || {};
  const fitting = ctx.results.resume.fitting || {};
  const card = (num, title, gate, ...body) => h('div', { class: `gate ${gate.status}` },
    h('div', { class: 'gate-head' }, h('span', { class: 'num' }, num), h('b', { class: 'grow' }, title), statusChip(gate.status)),
    h('div', { class: 'gate-body' }, body));
  const kv = (k, v) => h('div', { class: 'kv' }, h('span', {}, k), h('span', {}, v ?? '–'));

  const pages = g2.pdfinfo_pages ?? g2.form_feed_pages;
  const pageBoxes = pages ? Array.from({ length: Math.min(pages, 4) }, (_, i) =>
    h('div', { class: `page ${i > 0 ? 'over' : ''}` }, i + 1)) : [];

  clear(box, h('div', { class: 'gates' },
    card(1, 'Syntax (pdflatex)', g1,
      kv('Exit code', g1.returncode),
      kv('Fatal errors', g1.fatal_errors?.length ?? 0),
      kv('Overfull boxes', g1.overfull_boxes),
      g1.detail ? h('div', { class: 'notice fail small' }, g1.detail) : null,
      g1.fatal_errors?.length ? h('ul', { class: 'issues mono' }, g1.fatal_errors.map((e) => h('li', {}, e))) : null,
      log.tail?.length ? h('details', { class: 'more' }, h('summary', {}, `View ${log.name} (last ${log.tail.length} lines)`),
        h('pre', { class: 'console' }, log.tail.join('\n'))) : null),
    card(2, 'Strict one page', g2,
      h('div', { class: 'pages' }, pageBoxes, pages > 1 ? h('span', { class: 'chip fail' }, `overflow: ${pages - 1} extra page(s)`) : null),
      kv('pdfinfo pages', g2.pdfinfo_pages),
      kv('Form-feed pages', g2.form_feed_pages),
      kv('Compiles', g2.compile_iterations),
      kv('Units trimmed', g2.units_trimmed),
      kv('Spacing level', g2.spacing_level),
      fitting.removed?.length ? h('details', { class: 'more' }, h('summary', {}, `What was trimmed (${fitting.removed.length})`),
        h('ul', { class: 'small' }, fitting.removed.map((u) => h('li', {}, u.unit, h('span', { class: 'faint' }, ` — ${u.reason}`))))) : null,
      fitting.diagnostics?.length ? h('div', { class: 'notice fail small' }, fitting.diagnostics.join(' ')) : null),
    card(3, 'ATS readability', g3,
      g3.status === 'not_run' ? h('span', { class: 'muted' }, 'Runs after gates 1 and 2 pass.') : [
        h('div', { class: 'order' }, (g3.section_order || []).map((t, i) => [i ? h('span', { class: 'arrow' }, icon('arrow', 11)) : null,
          chip(t, ['Experience', 'Education', 'Projects'].includes(t) || /skill/i.test(t) ? 'accent' : '')])),
        kv('Terms checked', `${g3.terms_checked - (g3.fragmented_terms?.length || 0)} / ${g3.terms_checked} intact`),
        kv('Metrics checked', `${g3.metrics_checked - (g3.missing_metrics?.length || 0)} / ${g3.metrics_checked} intact`),
        kv('LaTeX residue', g3.latex_artifacts?.length ? g3.latex_artifacts.join(' ') : 'none'),
        g3.reasons?.length ? h('ul', { class: 'issues' }, g3.reasons.map((r) => h('li', {}, r))) : null]),
    card(4, 'Layout', g4,
      g4.status === 'not_run' ? h('span', { class: 'muted' }, 'Runs after gates 1 to 3 pass.') : [
        kv('Wrapped lines', g4.wrapped?.length ?? 0),
        kv('Overlaps', g4.overlaps?.length ?? 0),
        kv('Blank gaps', g4.big_gaps?.length ?? 0),
        kv('Space at bottom', g4.bottom_space_mm != null ? `${g4.bottom_space_mm} mm` : null),
        g4.section_gaps_mm && Object.keys(g4.section_gaps_mm).length ? h('details', { class: 'more' },
          h('summary', {}, 'Section gaps'),
          Object.entries(g4.section_gaps_mm).map(([t, mm]) => kv(t, `${mm} mm`))) : null,
        (g4.warnings || []).length ? h('div', { class: 'notice warn small' }, g4.warnings.join(' ')) : null,
        g4.reasons?.length ? h('ul', { class: 'issues' }, g4.reasons.map((r) => h('li', {}, r))) : null])));
}

function renderPdf(box, ctx) {
  const pdf = ctx.results?.pdf;
  const head = h('div', { class: 'panel-head' }, h('h2', { class: 'grow' }, 'Resume PDF'));
  if (!pdf) {
    clear(box, head, h('div', { class: 'pdf-empty muted' },
      h('div', {}, h('strong', {}, 'No PDF yet'), h('p', {}, ctx.results?.available
        ? (ctx.results.resume?.reason || 'The last run did not produce a verified PDF.')
        : 'Run tailoring to build and verify a one-page resume.'))));
    return;
  }
  const label = { verified: ['Verified one page', 'pass'], previous_verified: ['Previous verified build', 'warn'],
    unverified: ['Unverified', 'fail'] }[pdf.state];
  append(head, chip(label[0], label[1]),
    h('a', { class: 'btn small', href: pdf.url, target: '_blank', rel: 'noopener' }, 'Open', icon('external')));
  clear(box, head, h('iframe', { class: 'pdf-frame', src: pdf.url, title: 'Resume PDF preview' }));
}

async function renderTab(body, ctx) {
  const r = ctx.results;
  const tab = ctx.tab;
  if (tab === 'description') { clear(body, renderMarkdown(ctx.job.text)); return; }
  if (!r || !r.available) {
    clear(body, emptyState('Nothing here yet', 'Run tailoring for this job first.'));
    return;
  }
  if (tab === 'requirements') { clear(body, renderRequirements(r)); return; }
  clear(body, h('div', { class: 'muted' }, 'Loading…'));
  try {
    if (tab === 'provenance') {
      ctx.cache.provenance ??= await api(`/api/provenance/${encodeURIComponent(ctx.jobId)}`);
      if (ctx.tab === tab) clear(body, renderProvenance(ctx.cache.provenance));
    } else if (tab === 'fit') {
      ctx.cache.fit ??= await api(`/api/fit/${encodeURIComponent(ctx.jobId)}`);
      if (ctx.tab === tab) clear(body, renderFitReport(ctx.cache.fit));
    } else {
      ctx.cache.briefing ??= await api(`/api/briefing/${encodeURIComponent(ctx.jobId)}`);
      if (ctx.tab === tab) clear(body, tab === 'gap' ? renderGap(ctx.cache.briefing.gap, r) : renderBriefing(ctx.cache.briefing));
    }
  } catch (err) {
    clear(body, emptyState(err.status === 404 ? 'Not available' : 'Could not load', err.message));
  }
}

function renderRequirements(r) {
  if (!r.requirements.length) return emptyState('No requirements parsed', 'The description had no recognizable requirements.');
  return h('table', { class: 'data' },
    h('thead', {}, h('tr', {}, ['Requirement', 'Category', 'Weight', 'Status', 'Master evidence'].map((t) => h('th', {}, t)))),
    h('tbody', {}, r.requirements.map((req) => h('tr', {},
      h('td', {}, h('b', {}, req.name), req.members?.length ? h('div', { class: 'faint small' }, `any of ${req.members.join(', ')}`) : null,
        h('div', { class: 'muted small' }, req.explanation)),
      h('td', {}, req.category), h('td', {}, req.weight), h('td', {}, statusChip(req.status)),
      h('td', {}, h('div', { class: 'row' }, req.evidence.map((e) => lineChip(e.line, `L${e.line}${e.tag ? ` [${e.tag}]` : ''}`))))))));
}

// ---------------------------------------------------------------------------
// Fit report: requirement coverage, evidence and the resume line that proves it
// ---------------------------------------------------------------------------
const TYPE_CHIP = { core: 'accent', responsibility: '', preferred: '' };

function coverageBar(value) {
  if (value == null) return h('span', { class: 'faint' }, 'n/a');
  return h('div', { class: 'covbar', title: `${Math.round(value * 100)}%` },
    h('div', { class: `covfill ${value >= 0.67 ? 'good' : value > 0 ? 'mid' : 'none'}`,
      style: { width: `${Math.round(value * 100)}%` } }));
}

function renderFitReport(doc) {
  const rows = doc.requirements || [];
  const termLine = (term) => {
    const p = term.profile;
    const r = term.resume;
    return h('div', { class: 'term' },
      h('b', {}, term.display), ' ',
      p ? lineChip(p.line, `L${p.line} · ${['', 'mention', 'project', 'job'][p.strength] || p.strength}`)
        : chip('gap', 'fail'),
      r ? h('div', { class: 'small' }, h('span', { class: 'muted' }, `resume (${r.role}): `), r.quote)
        : (p ? h('div', { class: 'small faint' }, 'not shown on this resume') : null),
      term.rejected_matches?.length ? h('details', { class: 'more' },
        h('summary', {}, `${term.rejected_matches.length} rejected match(es)`),
        h('ul', { class: 'small muted' }, term.rejected_matches.map((m) => h('li', {}, m)))) : null);
  };
  return h('div', { class: 'stack' },
    h('div', { class: 'fit-cards' },
      fitCard('Profile fit', doc.profile_fit, doc.profile_fit == null ? 'not scorable'
        : `${doc.profile_fit_with_hard_requirements.toFixed(1)} with hard requirements`, ''),
      fitCard('Resume fit', doc.resume_fit, doc.underselling ? 'undersells your profile' : 'calibrated to the profile', '')),
    (doc.calibration || []).length ? h('div', { class: 'notice small' }, doc.calibration.join(' · ')) : null,
    h('h3', {}, 'Hard requirements'),
    (doc.hard_requirements || []).length ? h('div', {}, doc.hard_requirements.map((hr) => h('div', { class: 'rec' },
      h('div', { class: 'stack', style: { gap: '4px' } }, chip(`${hr.type}: ${hr.status.replace('_', ' ')}`,
        hr.status === 'met' ? 'pass' : hr.status === 'not_met' ? 'fail' : 'warn'),
      h('span', { class: 'faint small' }, `JD line ${hr.jd_line}`)),
      h('div', {}, h('div', { class: 'small muted' }, hr.text), hr.detail))))
      : h('p', { class: 'muted' }, 'None found in the posting.'),
    h('div', { class: 'notice small' }, 'Work authorization: ', doc.work_authorization_note),
    h('h3', {}, 'Top changes that would raise resume fit'),
    (doc.top_changes || []).length ? h('ol', { class: 'changes' }, doc.top_changes.map((c) => h('li', {},
      c.change, h('div', { class: 'faint small' }, `proves ${c.proves.join(', ')} · +${c.resume_fit_gain.toFixed(1)}`))))
      : h('p', { class: 'muted' }, 'None: the resume shows every verified match.'),
    doc.selection_check?.violation ? h('div', { class: 'notice small' },
      chip('selection check failed', 'fail'), ' Unmapped bullets were chosen while requirements ',
      doc.selection_check.uncovered_coverable_requirements.join(', '), ' stayed uncovered.') : null,
    (doc.rewrites || []).length ? h('details', { class: 'more' },
      h('summary', {}, `${doc.rewrites.length} wording rewrite(s) from rewrites.json, re-verified`),
      h('ul', { class: 'small muted' }, doc.rewrites.map((r) => h('li', {},
        `${r.entry} (${r.kind}): `, h('s', {}, r.original), ' → ', r.rewritten)))) : null,
    h('h3', {}, `Requirements (${rows.length})`),
    h('table', { class: 'data fit-table' },
      h('thead', {}, h('tr', {}, ['Requirement', 'Profile', 'Resume', 'Evidence and proof'].map((t) => h('th', {}, t)))),
      h('tbody', {}, rows.map((row) => h('tr', {},
        h('td', {}, h('div', { class: 'row' }, h('b', {}, row.id), chip(row.type, TYPE_CHIP[row.type] ?? ''),
          h('span', { class: 'faint small' }, `w${row.weight}${row.mode === 'any_of' ? ' · any one' : ''}`)),
        h('div', { class: 'small' }, row.text)),
        h('td', {}, coverageBar(row.profile_coverage)),
        h('td', {}, coverageBar(row.resume_coverage)),
        h('td', {}, row.terms.map(termLine)))))),
    (doc.unscored_lines || []).length ? h('details', { class: 'more' },
      h('summary', {}, `${doc.unscored_lines.length} JD line(s) not scored (no skill or known concept)`),
      h('ul', { class: 'small muted' }, doc.unscored_lines.map((u) => h('li', {}, `JD line ${u.jd_line}: ${u.text}`)))) : null);
}

// ---------------------------------------------------------------------------
// Provenance inspector
// ---------------------------------------------------------------------------
const CLAIM_TYPES = { bullet: 'Bullets', entry_header: 'Entry headers', stack_item: 'Stack items',
  skills_item: 'Skills items', summary_sentence: 'Summary' };

function renderProvenance(data) {
  const proof = data.provenance;
  const claims = proof.claims;
  const right = h('div', { class: 'stack' });
  const list = h('div', { class: 'claims' });
  let filter = 'bullet';
  let selected = null;

  const lineEls = new Map();
  const master = h('div', { class: 'master-view', 'aria-label': data.master_file },
    data.master_lines.map((line) => {
      const el = h('div', { class: 'mline dim', dataset: { n: line.n } },
        h('span', { class: 'ln' }, line.n), h('span', { class: 'tx' }, line.text || ' '));
      lineEls.set(line.n, el);
      return el;
    }));

  function select(claim, card) {
    selected = claim;
    list.querySelectorAll('.claim').forEach((c) => c.classList.toggle('selected', c === card));
    for (const el of lineEls.values()) el.className = 'mline dim';
    const primary = claim.evidence.map((e) => e.line);
    const support = new Set([...(claim.skills || []).flatMap((s) => s.lines), ...(claim.metrics || []).map((m) => m.line)]);
    for (const item of claim.evidence) {
      const el = lineEls.get(item.line);
      if (el) el.className = `mline cited ${item.tag === '[R]' ? 't-r' : ''} ${item.tag === 'heading' ? 'h-heading' : ''}`;
    }
    for (const n of support) {
      const el = lineEls.get(n);
      if (el && !primary.includes(n)) el.className = 'mline support';
    }
    const first = lineEls.get(primary.length ? Math.min(...primary) : Math.min(...support));
    // The first selection happens before the view is attached; measure on the next frame.
    if (first) requestAnimationFrame(() => { master.scrollTop = first.offsetTop - master.clientHeight / 3; });
    const entry = claim.master_entry;
    clear(right,
      data.stale ? h('div', { class: 'notice warn' }, `${data.master_file} changed after this build, so line numbers may have shifted. Re-run tailoring to refresh the map.`) : null,
      h('div', { class: 'citation' },
        h('div', { class: 'row' }, chip(CLAIM_TYPES[claim.type] || claim.type, 'accent'),
          claim.traced ? h('span', { class: 'chip pass' }, icon('check', 11), 'traced') : chip('untraced', 'fail'),
          claim.wording_support != null ? chip(`wording support ${Math.round(claim.wording_support * 100)}%`) : null),
        entry ? h('div', {}, h('span', { class: 'muted' }, 'Master entry: '), h('b', {}, entry.title),
          ' ', lineChip(entry.heading_line, `heading L${entry.heading_line}`)) : null,
        entry?.links?.length ? h('div', { class: 'row' }, h('span', { class: 'muted' }, 'Repo / live links:'),
          entry.links.map((l) => extLink(l))) : null,
        h('div', { class: 'row' }, h('span', { class: 'muted' }, 'Cited lines:'),
          claim.evidence.map((e) => lineChip(e.line, `L${e.line} ${e.tag}`))),
        claim.metrics?.length ? h('div', { class: 'row' }, h('span', { class: 'muted' }, 'Metrics:'),
          claim.metrics.map((m) => h('span', { class: 'chip warn' }, `${m.value} → L${m.line}`))) : null,
        claim.skills?.length ? h('div', { class: 'row' }, h('span', { class: 'muted' }, 'Skills:'),
          claim.skills.map((s) => chip(`${s.skill}${s.lines.length ? ` → L${s.lines.join(', L')}` : ''}`))) : null),
      master);
  }

  function renderList() {
    const shown = claims.filter((c) => filter === 'all' || c.type === filter);
    const groups = new Map();
    for (const c of shown) {
      const key = `${c.section}${c.entry ? ` · ${c.entry.split(' | ')[0]}` : ''}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(c);
    }
    clear(list, [...groups.entries()].map(([group, items]) => [
      h('div', { class: 'claim-group' }, group),
      items.map((claim) => {
        const card = h('div', { class: 'claim', tabindex: 0, role: 'button' },
          h('div', { class: 'ctext' }, claim.text),
          h('div', { class: 'row' }, claim.evidence.slice(0, 4).map((e) => chip(`${e.tag} L${e.line}`, e.tag === '[R]' ? 'accent' : 'pass')),
            (claim.matched_requirements || []).map((r) => chip(r, 'accent'))));
        card.addEventListener('click', () => select(claim, card));
        card.addEventListener('keydown', (e) => { if (e.key === 'Enter') select(claim, card); });
        return card;
      })]));
    const first = list.querySelector('.claim');
    if (first) first.click();
    else clear(right, emptyState('No claims of this type', 'Pick another filter.'));
  }

  const counts = proof.summary.by_type;
  const filters = h('div', { class: 'row' }, Object.entries(CLAIM_TYPES).filter(([t]) => counts[t]).map(([type, label]) => {
    const b = h('button', { class: `btn small ${type === filter ? 'primary' : ''}`, type: 'button' }, `${label} (${counts[type]})`);
    b.addEventListener('click', () => {
      filter = type;
      filters.querySelectorAll('button').forEach((x) => x.classList.toggle('primary', x === b));
      renderList();
    });
    return b;
  }));
  const view = h('div', { class: 'stack' },
    h('div', { class: 'row' }, h('span', { class: 'chip pass' }, `${proof.summary.traced} / ${proof.summary.claims} claims traced`),
      h('span', { class: 'muted small grow' }, proof.rule)),
    filters,
    h('div', { class: 'prov' }, list, right));
  renderList();
  return view;
}

// ---------------------------------------------------------------------------
// Gap report and interview briefing
// ---------------------------------------------------------------------------
const GAP_STATUS = { NO: 'fail', NEGATED: 'fail', GAP: 'warn', OLD: 'warn', UNTAGGED: 'warn', UNCONFIRMED: 'warn',
  EXPOSURE_ONLY: 'warn', HEADING_ONLY: 'warn', MISSING: '', YEARS_NOT_INFERRED: '', DEGREE_MISMATCH: 'fail', DEGREE_UNCLEAR: 'warn' };
const ACTION_LABELS = { unlock_plan: ['Unlock plan', 'accent'], confirm_existing_mention: ['Confirm mention', 'warn'],
  add_evidence_if_true: ['Add if true', 'accent'], deepen_evidence: ['Deepen evidence', 'warn'],
  do_not_claim: ['Do not claim', 'fail'], acknowledge_gap: ['Real gap', 'fail'], not_actionable: ['Not actionable', ''],
  human_judgment: ['Your call', 'warn'] };

function renderGap(gap, results) {
  if (!gap) {
    return emptyState('No gap report for this run',
      `It is written automatically below ${state.threshold}, or when "Always write gap report" is ticked.`);
  }
  const plan = gap.unlock_plan;
  const pct = (v) => `${Math.max(0, Math.min(100, v))}%`;
  const recs = gap.actionable_recommendations;
  const unlock = recs.find((r) => r.action === 'unlock_plan');
  const skillTable = (rows) => h('table', { class: 'data' },
    h('thead', {}, h('tr', {}, ['Skill', 'Category', 'Weight', 'In master', 'Lines', 'If evidenced'].map((t) => h('th', {}, t)))),
    h('tbody', {}, rows.map((m) => h('tr', {},
      h('td', {}, h('b', {}, m.skill), m.confidence === 'low' ? h('div', { class: 'faint small' }, 'low-confidence term') : null),
      h('td', {}, m.category), h('td', {}, m.weight),
      h('td', {}, chip(statusLabel(m.master_status), GAP_STATUS[m.master_status] ?? '')),
      h('td', {}, h('div', { class: 'row' }, m.master_lines.map((l) => lineChip(l.line)))),
      h('td', {}, m.claimable ? h('b', {}, `${m.score_if_evidenced}`) : h('span', { class: 'faint' }, 'n/a'))))));
  return h('div', { class: 'stack' },
    h('div', {},
      h('div', { class: 'row' }, h('h2', { class: 'grow' }, `Match score ${gap.match_score}%`),
        chip(gap.verdict === 'PROCEED' ? 'PROCEED' : 'LOW FIT', gap.verdict === 'PROCEED' ? 'pass' : 'warn'),
        chip(`trigger: ${gap.trigger}`)),
      h('div', { class: 'scorebar', role: 'img', 'aria-label': `Score ${gap.fit_score}, projected ${plan.projected_score}, threshold ${gap.threshold}` },
        plan.projected_score > gap.fit_score ? h('div', { class: 'ghost', style: { width: pct(plan.projected_score) } }) : null,
        h('div', { class: 'fill', style: { width: pct(gap.fit_score) } }),
        h('div', { class: 'mark', style: { left: pct(gap.threshold) } }, h('span', {}, `${gap.threshold} threshold`))),
      h('div', { class: 'muted small' }, `Now ${gap.fit_score} · projected ${plan.projected_score} if the unlock plan is true · ${plan.claimable_weight_available} claimable weight available`)),
    unlock ? h('div', { class: 'callout' }, unlock.recommendation) : null,
    h('h3', {}, `Missing critical skills (${gap.missing_critical_skills.length})`),
    gap.missing_critical_skills.length ? skillTable(gap.missing_critical_skills) : h('p', { class: 'muted' }, 'None.'),
    gap.missing_preferred_skills.length ? h('details', { class: 'more' },
      h('summary', {}, `Missing preferred skills (${gap.missing_preferred_skills.length})`), skillTable(gap.missing_preferred_skills)) : null,
    h('h3', {}, 'Actionable recommendations'),
    h('div', {}, recs.filter((r) => r.action !== 'unlock_plan').map((rec) => {
      const [label, kind] = ACTION_LABELS[rec.action] || [rec.action, ''];
      return h('div', { class: 'rec' },
        h('div', { class: 'stack', style: { gap: '4px' } }, chip(label, kind), rec.requirement ? h('b', {}, rec.requirement) : null,
          rec.target_entry ? h('span', { class: 'muted small' }, `→ ${rec.target_entry}`) : null),
        h('div', {}, rec.recommendation,
          rec.score_if_evidenced != null ? h('div', { class: 'faint small' }, `score if evidenced: ${rec.score_if_evidenced}`) : null));
    })),
    h('p', { class: 'muted small' }, gap.policy, ' ', h('a', { href: '#/profile' }, 'Open Profile Studio', icon('arrow', 11))));
}

function renderBriefing(data) {
  const b = data.briefing;
  if (!b) return emptyState('No briefing yet', 'The briefing is written only after a resume passes all four build gates.');
  const raw = h('pre', { class: 'md-raw hidden' }, data.markdown || '');
  return h('div', { class: 'stack' },
    h('div', { class: 'row' },
      h('h2', { class: 'grow' }, `Interview briefing: ${b.role} at ${b.company}`),
      h('button', { class: 'btn small', type: 'button', onclick: async () => {
        await navigator.clipboard.writeText(data.markdown || '');
        toast('Briefing Markdown copied');
      } }, 'Copy Markdown'),
      h('button', { class: 'btn small', type: 'button', onclick: () => raw.classList.toggle('hidden') }, 'View Markdown')),
    raw,
    h('h3', {}, 'Technical context (verbatim from the job description)'),
    b.technical_context.length ? b.technical_context.map((item) => h('blockquote', { class: 'quote' },
      h('div', { class: 'src' }, `JD line ${item.jd_line} · ${item.section}`),
      h('div', {}, item.text),
      h('div', { class: 'row', style: { 'margin-top': '6px' } }, item.requirements.map((r) => chip(r.name, STATUS_CHIP[r.status] ?? '')))))
      : h('p', { class: 'muted' }, 'No weighted requirement lines were found.'),
    h('h3', {}, 'Requirements to prepare for'),
    h('table', { class: 'data' }, h('tbody', {}, b.requirements_to_prepare.map((r) => h('tr', {},
      h('td', {}, h('b', {}, r.name)), h('td', {}, r.category), h('td', {}, statusChip(r.status)))))),
    h('h3', {}, 'Highlighted metrics in this resume'),
    b.highlighted_metrics.length ? h('div', {}, b.highlighted_metrics.map((m) => h('div', { class: 'metric-card' },
      h('div', { class: 'metric-values' }, m.metrics.map((v) => h('span', { class: 'chip warn' }, h('b', {}, v)))),
      h('div', {}, h('div', {}, m.bullet), h('div', { class: 'row small', style: { 'margin-top': '4px' } },
        h('span', { class: 'muted' }, m.entry), m.master_lines.map((n) => lineChip(n)))))))
      : h('p', { class: 'muted' }, 'This build contains no metrics.'),
    h('h3', {}, 'STAR talking points'),
    b.star_prompts.map((s, i) => h('div', { class: 'star' },
      h('div', { class: 'star-head' }, h('b', { class: 'grow' }, `${i + 1}. ${s.requirement || 'Additional evidence'}`),
        s.category ? chip(s.category) : null),
      h('dl', {},
        h('dt', {}, 'Question'), h('dd', {}, s.likely_question),
        h('dt', {}, 'Situation'), h('dd', {}, s.situation),
        h('dt', {}, 'Task'), h('dd', {}, s.task),
        h('dt', {}, 'Action'), h('dd', {}, s.action),
        h('dt', {}, 'Result'), h('dd', {}, s.result),
        h('dt', {}, 'Evidence'), h('dd', {}, h('div', { class: 'row' }, s.evidence_lines.map((e) => lineChip(e.line, `L${e.line} ${e.tag}`))))))),
    h('p', { class: 'muted small' }, b.note));
}

/** Minimal, safe Markdown: headings, lists, paragraphs, bold, code and http(s) links. */
function renderMarkdown(text) {
  const root = h('div', { class: 'jd' });
  let list = null;
  let para = [];
  const flushPara = () => { if (para.length) { root.append(h('p', {}, inline(para.join(' ')))); para = []; } };
  for (const rawLine of String(text || '').split('\n')) {
    const line = rawLine.trimEnd();
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    const bullet = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (heading) {
      flushPara(); list = null;
      root.append(h(`h${Math.min(heading[1].length + 1, 6)}`, {}, inline(heading[2])));
    } else if (bullet) {
      flushPara();
      if (!list) { list = h('ul'); root.append(list); }
      list.append(h('li', {}, inline(bullet[1])));
    } else if (!line.trim()) {
      flushPara(); list = null;
    } else {
      list = null; para.push(line.trim());
    }
  }
  flushPara();
  return root;
}

function inline(text) {
  const out = [];
  const pattern = /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
  let last = 0;
  for (const m of text.matchAll(pattern)) {
    out.push(text.slice(last, m.index));
    if (m[1]) out.push(h('strong', {}, m[1]));
    else if (m[2]) out.push(h('code', {}, m[2]));
    else out.push(extLink(m[4], m[3]));
    last = m.index + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}

// ---------------------------------------------------------------------------
// Profile Studio: master profile and template editor
// ---------------------------------------------------------------------------
const TAGS = ['V', 'R', 'NO', 'GAP', 'OLD'];
const TAG_RE = /^(\s*(?:[-*]\s+)?)`?\[(V|R|OLD|NO|GAP)\]`?\s*/;

async function renderStudio(view, kind, jumpLine) {
  const source = await api(`/api/${kind}`);
  state.editor = { kind, text: source.text, sha: source.sha256, dirty: false };
  const textarea = h('textarea', { spellcheck: 'false', wrap: 'off', 'aria-label': source.file });
  textarea.value = source.text;
  const gutter = h('pre', { class: 'gutter', 'aria-hidden': 'true' });
  const highlight = h('div', { class: 'hl hidden' });
  const editor = h('div', { class: 'editor' }, gutter, textarea, highlight);
  const dirtyDot = h('span', { class: 'dirty-dot hidden', title: 'Unsaved changes' });
  const saveButton = h('button', { class: 'btn primary', type: 'button' }, 'Save');
  const revertButton = h('button', { class: 'btn', type: 'button' }, 'Revert');
  const side = h('div', { class: 'stack' });
  const lineHeight = 20;
  let highlighted = jumpLine;

  const syncGutter = () => {
    const count = textarea.value.split('\n').length;
    if (gutter.dataset.count !== String(count)) {
      gutter.textContent = Array.from({ length: count }, (_, i) => i + 1).join('\n');
      gutter.dataset.count = String(count);
    }
    gutter.scrollTop = textarea.scrollTop;
    if (highlighted) {
      highlight.classList.remove('hidden');
      highlight.style.setProperty('top', `${10 + (highlighted - 1) * lineHeight - textarea.scrollTop}px`);
    }
  };
  const jump = (n) => {
    const lines = textarea.value.split('\n');
    const line = Math.max(1, Math.min(n, lines.length));
    const start = lines.slice(0, line - 1).reduce((sum, l) => sum + l.length + 1, 0);
    textarea.focus();
    textarea.setSelectionRange(start, start + lines[line - 1].length);
    textarea.scrollTop = Math.max(0, (line - 1) * lineHeight - textarea.clientHeight / 3);
    highlighted = line;
    syncGutter();
  };
  const markDirty = () => {
    state.editor.dirty = textarea.value !== state.editor.text;
    dirtyDot.classList.toggle('hidden', !state.editor.dirty);
  };
  const validate = debounce(async () => {
    try {
      renderSide(side, kind, await api(`/api/${kind}/validate`, { method: 'POST', body: { text: textarea.value } }), jump);
    } catch (err) { toast(err.message, 'fail'); }
  }, 600);

  textarea.addEventListener('scroll', syncGutter);
  textarea.addEventListener('input', () => { markDirty(); syncGutter(); validate(); });
  textarea.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 's') { event.preventDefault(); save(); }
  });

  function setTag(tag) {
    const { selectionStart: start } = textarea;
    const value = textarea.value;
    const lineStart = value.lastIndexOf('\n', start - 1) + 1;
    let lineEnd = value.indexOf('\n', start);
    if (lineEnd === -1) lineEnd = value.length;
    const line = value.slice(lineStart, lineEnd);
    let next;
    if (TAG_RE.test(line)) next = line.replace(TAG_RE, (_m, lead) => (tag ? `${lead}\`[${tag}]\` ` : lead));
    else if (tag) next = line.replace(/^(\s*(?:[-*]\s+)?)/, (lead) => `${lead || '- '}\`[${tag}]\` `);
    else return;
    textarea.setRangeText(next, lineStart, lineEnd, 'end');
    textarea.dispatchEvent(new Event('input'));
  }

  async function save() {
    try {
      const saved = await api(`/api/${kind}`, { method: 'POST', body: { text: textarea.value, base_sha256: state.editor.sha } });
      state.editor = { kind, text: saved.text, sha: saved.sha256, dirty: false };
      markDirty();
      renderSide(side, kind, saved, jump);
      const changes = saved.changes;
      const summary = changes ? [changes.skills_added.length && `+${changes.skills_added.length} skills`,
        changes.skills_removed.length && `−${changes.skills_removed.length} skills`,
        changes.newly_no.length && `${changes.newly_no.length} newly [NO]`].filter(Boolean).join(', ') : '';
      toast(`Saved ${saved.file}${summary ? ` (${summary})` : ''}. A backup of the previous version is in .studio/backups/.`);
    } catch (err) {
      toast(err.message, 'fail');
    }
  }
  saveButton.addEventListener('click', save);
  revertButton.addEventListener('click', () => {
    if (state.editor.dirty && !confirm('Discard your unsaved edits?')) return;
    textarea.value = state.editor.text;
    markDirty(); syncGutter(); validate();
  });

  clear(view,
    reveal(h('div', { class: 'studio' },
      h('section', { class: 'panel' },
        h('div', { class: 'panel-head' },
          h('div', { class: 'segmented' },
            h('a', { href: '#/profile', class: kind === 'profile' ? 'active' : '' }, 'Master profile'),
            h('a', { href: '#/template', class: kind === 'template' ? 'active' : '' }, 'Template')),
          h('span', { class: 'mono muted' }, source.file), dirtyDot, h('span', { class: 'spacer' }),
          revertButton, saveButton),
        kind === 'profile' ? h('div', { class: 'tagbar' }, h('span', { class: 'muted small' }, 'Tag current line:'),
          TAGS.map((t) => h('button', { class: 'btn small', type: 'button', onclick: () => setTag(t) }, `[${t}]`)),
          h('button', { class: 'btn small ghost', type: 'button', onclick: () => setTag(null) }, 'Remove tag'),
          h('span', { class: 'spacer' }), h('span', { class: 'faint small' }, h('kbd', {}, '⌘'), h('kbd', {}, 'S'), ' or ', h('kbd', {}, 'Ctrl'), h('kbd', {}, 'S'), ' saves')) : null,
        editor),
      side)));
  renderSide(side, kind, source, jump);
  syncGutter();
  if (jumpLine) requestAnimationFrame(() => jump(jumpLine));
}

function renderSide(side, kind, check, jump) {
  const s = check.summary;
  const messages = [
    ...(check.errors || []).map((e) => h('div', { class: 'notice fail' }, e)),
    ...(check.warnings || []).slice(0, 6).map((w) => h('div', { class: 'notice warn small' }, w)),
  ];
  if (kind === 'template') {
    clear(side, h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', {}, 'Template structure'),
      check.ok ? chip('parses', 'pass') : chip('invalid', 'fail')),
    h('div', { class: 'panel-body stack' }, messages,
      s ? [h('div', { class: 'small muted' }, `Engine: ${s.engine} (${s.engine_reason})`),
        h('table', { class: 'data' }, h('tbody', {}, s.sections.map((sec) => h('tr', {},
          h('td', {}, h('b', {}, sec.title)), h('td', { class: 'muted' }, sec.kind),
          h('td', { class: 'muted nowrap' }, sec.entries ? `${sec.entries} entries, ${sec.bullets} bullets` : '')))))] : null,
      h('p', { class: 'muted small' }, 'The template sets formatting only. Its bullets are candidate wording and are verified against the master profile on every run.'))));
    return;
  }
  const changes = s?.changes;
  clear(side,
    h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'grow' }, 'Profile check'),
        check.ok ? chip('parses', 'pass') : chip('blocked', 'fail')),
      h('div', { class: 'panel-body stack' }, messages,
        s ? [
          h('div', { class: 'tag-counts' }, Object.entries(s.tags).map(([tag, n]) => h('div', {}, h('b', {}, n), h('span', {}, tag)))),
          h('div', { class: 'small muted' }, `${s.evidence_lines} evidence lines · ${s.skills.length} evidenced skills · ${s.no_skills.length} [NO] skills`),
          changes && Object.values(changes).some((v) => v.length) ? h('div', { class: 'stack small' },
            h('h3', {}, 'Unsaved changes'),
            changes.skills_added.length ? h('div', {}, h('b', {}, 'New evidence: '), changes.skills_added.join(', ')) : null,
            changes.skills_removed.length ? h('div', { class: 'notice warn' }, h('b', {}, 'Loses evidence: '), changes.skills_removed.join(', ')) : null,
            changes.newly_no.length ? h('div', { class: 'notice fail' }, h('b', {}, 'Newly [NO]: '), changes.newly_no.join(', ')) : null,
            changes.no_lifted.length ? h('div', { class: 'notice warn' }, h('b', {}, '[NO] lifted: '), changes.no_lifted.join(', ')) : null) : null,
        ] : null)),
    s ? h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', {}, 'Outline')),
      h('div', { class: 'panel-body outline' }, s.entries.map((e) => h('a', { href: '#', onclick: (ev) => { ev.preventDefault(); jump(e.line); } },
        h('span', { class: 'ln' }, e.line), h('span', { class: 'grow' }, e.title), h('span', { class: 'faint small' }, e.kind))))) : null,
    s?.no_skills.length ? h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', {}, 'Never claimed ([NO])')),
      h('div', { class: 'panel-body row' }, s.no_skills.map((n) => chip(n, 'fail')))) : null);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
window.addEventListener('hashchange', route);
window.addEventListener('beforeunload', (event) => {
  if (state.editor?.dirty) { event.preventDefault(); event.returnValue = ''; }
});
document.addEventListener('DOMContentLoaded', async () => {
  wireDialogs();
  try { await loadMeta(); } catch (err) { toast(`Server unreachable: ${err.message}`, 'fail'); }
  try { await restoreResume(); } catch { /* the board falls back to the master profile */ }
  if (!location.hash) location.hash = '#/jobs';
  else route();
});
