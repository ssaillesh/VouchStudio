(() => {
  'use strict';
  const PAGE = 40;
  const STORE_KEY = 'jobauto.profile.v1';
  const MAX_REMEMBER_CHARS = 2_800_000;
  const $ = (id) => document.getElementById(id);
  const state = {
    jobs: [], scores: new Map(), profile: null, threshold: 65, shown: PAGE,
    q: '', sort: 'best', status: 'all', region: 'all', levels: new Set(), levelNames: [],
  };

  const store = {
    get() { try { return JSON.parse(localStorage.getItem(STORE_KEY)); } catch (e) { return null; } },
    set(value) { try { localStorage.setItem(STORE_KEY, JSON.stringify(value)); return true; } catch (e) { return false; } },
    clear() { try { localStorage.removeItem(STORE_KEY); } catch (e) { /* storage unavailable */ } },
  };

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  // Scroll-entry reveal
  const io = 'IntersectionObserver' in window ? new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) { entry.target.classList.add('in'); io.unobserve(entry.target); }
    }
  }, { rootMargin: '0px 0px -40px 0px' }) : null;
  function reveal(node, index) {
    node.classList.add('reveal');
    node.style.setProperty('--index', String(Math.min(index, 8)));
    if (io) io.observe(node); else node.classList.add('in');
  }
  document.querySelectorAll('.reveal').forEach((node, i) => reveal(node, i % 6));

  // Tabs
  function showTab(name) {
    for (const tab of document.querySelectorAll('.tab')) {
      const on = tab.id === 'tab-' + name;
      tab.setAttribute('aria-selected', String(on));
      $(tab.getAttribute('aria-controls')).hidden = !on;
    }
    document.querySelectorAll('#panel-' + name + ' .reveal').forEach((n) => n.classList.add('in'));
  }
  document.querySelectorAll('.tab').forEach((tab) =>
    tab.addEventListener('click', () => showTab(tab.id.replace('tab-', ''))));
  document.querySelectorAll('[data-goto]').forEach((btn) =>
    btn.addEventListener('click', () => { showTab(btn.dataset.goto); window.scrollTo({ top: 0, behavior: 'smooth' }); }));

  // Board data
  async function load() {
    const [jobs, meta] = await Promise.all([
      fetch('/api/jobs').then((r) => r.json()),
      fetch('/api/meta').then((r) => r.json()),
    ]);
    state.jobs = jobs;
    state.threshold = meta.threshold;
    state.levelNames = meta.levels || [];
    state.minSkills = meta.min_skill_requirements || 3;
    buildLevelChips();
    $('footer-index').textContent = `Index built ${String(meta.generated || '').replace('T', ' ')} · ${meta.jobs} jobs`;
    renderStats();
    renderList();
    const saved = store.get();
    if (saved && saved.filename && saved.content_base64) {
      $('remember').checked = true;
      scoreUpload(saved.filename, saved.content_base64, false);
    }
  }

  function renderStats() {
    const jobs = state.jobs;
    $('stat-jobs').textContent = jobs.length.toLocaleString();
    $('stat-scorable').textContent = jobs.filter((j) => j.enough_detail).length.toLocaleString();
    $('stat-scorable-label').textContent = `With enough detail to score (at least ${state.minSkills || 3} recognized skills)`;
    $('stat-us').textContent = jobs.filter((j) => j.region.includes('US')).length.toLocaleString();
    $('stat-ca').textContent = jobs.filter((j) => j.region.includes('Canada')).length.toLocaleString();
    const levels = {};
    for (const job of jobs) levels[job.level] = (levels[job.level] || 0) + 1;
    const list = $('stat-levels');
    list.replaceChildren();
    for (const name of state.levelNames) {
      const li = el('li');
      li.append(el('span', null, name), el('span', null, String(levels[name] || 0)));
      list.append(li);
    }
    if (state.profile) {
      $('stat-fit').textContent = fitCount().toLocaleString();
      $('stat-fit-label').textContent = `Jobs scoring ${state.threshold} or more for your profile`;
    } else {
      $('stat-fit').textContent = '—';
      $('stat-fit-label').textContent = 'Upload a resume to see how many you fit';
    }
  }

  function ranked(s) { return Boolean(s && !s.insufficient); }
  function fitCount() { return [...state.scores.values()].filter((s) => ranked(s) && s.score >= state.threshold).length; }

  function filtered() {
    const q = state.q;
    const out = state.jobs.filter((job) => {
      const s = state.scores.get(job.id);
      if (state.status === 'proceed' && !(ranked(s) && s.score >= state.threshold)) return false;
      if (state.status === 'scored' && !ranked(s)) return false;
      if (state.status === 'thin' && !(job.complete && !job.enough_detail)) return false;
      if (state.status === 'listing' && job.complete) return false;
      if (state.region !== 'all' && !job.region.includes(state.region)) return false;
      if (state.levels.size && !state.levels.has(job.level)) return false;
      if (q && !(job.title + ' ' + job.company + ' ' + job.location).toLowerCase().includes(q)) return false;
      return true;
    });
    const score = (job) => { const s = state.scores.get(job.id); return ranked(s) ? s : null; };
    const byNewest = (a, b) => String(b.posted || '').localeCompare(String(a.posted || ''));
    const sorters = {
      best: (a, b) => ((score(b) ? score(b).rank : -1) - (score(a) ? score(a).rank : -1))
        || ((score(b) ? score(b).score : -1) - (score(a) ? score(a).score : -1)) || byNewest(a, b),
      score: (a, b) => ((score(b) ? score(b).score : -1) - (score(a) ? score(a).score : -1)) || byNewest(a, b),
      newest: byNewest,
      company: (a, b) => a.company.localeCompare(b.company) || a.title.localeCompare(b.title),
    };
    const sort = state.profile || state.sort === 'company' ? state.sort : 'newest';
    return out.sort(sorters[sort]);
  }

  function regionTag(region) {
    const cls = region.includes('US') ? 'tag tag-blue' : region === 'Canada' ? 'tag tag-yellow' : 'tag';
    return el('span', cls, region);
  }

  const GAP_LABEL = { explicitly_no: 'no', not_evidenced: 'missing', ambiguous: 'unclear' };

  function jobRow(job, index) {
    const raw = state.scores.get(job.id);
    const s = ranked(raw) ? raw : null;
    const thin = job.complete && !job.enough_detail;
    const row = el('article', 'job');
    const score = el('div', 'score' + (s && s.score >= state.threshold ? ' pass' : ''),
                     s ? String(s.score) : '—');
    score.append(el('small', null, s ? `${s.requirements} req${s.requirements === 1 ? '' : 's'}`
                                     : thin ? 'too little detail' : job.complete ? 'not scored' : 'listing'));
    const main = el('div', 'job-main');
    const head = el('div', 'job-head');
    head.append(el('h3', null, job.title));
    const tags = el('div', 'tags');
    if (s) tags.append(el('span', s.score >= state.threshold ? 'tag tag-green' : 'tag tag-red',
                          s.score >= state.threshold ? `${state.threshold}+` : `Below ${state.threshold}`));
    else if (thin) tags.append(el('span', 'tag', 'Not enough detail'));
    else if (!job.complete) tags.append(el('span', 'tag', 'Listing only'));
    tags.append(regionTag(job.region), el('span', 'tag', job.level), el('span', 'tag', job.source));
    head.append(tags);
    const meta = [job.company, job.location, job.posted ? 'posted ' + job.posted : null].filter(Boolean);
    main.append(head, el('p', 'job-meta', meta.join(' · ')));
    if (s) {
      const detail = el('div', 'detail');
      const left = el('div'); left.append(el('h4', null, 'Evidence matched'));
      const matched = el('div', 'chips');
      s.matched.slice(0, 8).forEach((m) => matched.append(el('span', 'chip match', m)));
      if (s.matched.length > 8) matched.append(el('span', 'chip', `+${s.matched.length - 8}`));
      if (!s.matched.length) matched.append(el('span', 'muted', 'None'));
      left.append(matched);
      const right = el('div'); right.append(el('h4', null, 'Gaps'));
      const gaps = el('div', 'chips');
      s.gaps.slice(0, 6).forEach(([name, status]) => {
        const chip = el('span', status === 'explicitly_no' ? 'chip no' : 'chip', name);
        chip.append(el('em', null, GAP_LABEL[status] || status));
        gaps.append(chip);
      });
      if (s.gaps.length > 6) gaps.append(el('span', 'chip', `+${s.gaps.length - 6}`));
      if (!s.gaps.length) gaps.append(el('span', 'muted', 'None'));
      right.append(gaps);
      detail.append(left, right);
      main.append(detail);
    } else if (thin) {
      const found = raw && raw.recognized && raw.recognized.length ? ' (' + raw.recognized.join(', ') + ')' : '';
      main.append(el('p', 'muted small', `Not enough detail to score: the posting has ${job.skill_requirements} recognized skill requirement${job.skill_requirements === 1 ? '' : 's'}${found}, and a score needs at least ${state.minSkills || 3}. Read the posting to judge fit.`));
    } else if (!job.complete) {
      main.append(el('p', 'muted small', 'This source lists the job without a description, so it cannot be scored. Open the posting to read it.'));
    }
    const foot = el('div', 'job-foot');
    if (job.url && /^https?:\/\//i.test(job.url)) {
      const link = el('a', 'link', 'Posting ↗');
      link.href = job.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      foot.append(link);
    }
    main.append(foot);
    row.append(score, main);
    reveal(row, index);
    return row;
  }

  function renderList() {
    const items = filtered();
    const container = $('jobs');
    container.replaceChildren();
    const page = items.slice(0, state.shown);
    page.forEach((job, i) => container.append(jobRow(job, i % PAGE)));
    if (!items.length) container.append(el('p', 'empty', 'No jobs match these filters.'));
    $('count').textContent = `${Math.min(state.shown, items.length).toLocaleString()} of ${items.length.toLocaleString()} shown`;
    $('more').hidden = items.length <= state.shown;
  }

  // Controls
  let searchTimer = null;
  $('search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.q = e.target.value.trim().toLowerCase(); state.shown = PAGE; renderList(); }, 150);
  });
  function buildLevelChips() {
    const counts = {};
    for (const job of state.jobs) counts[job.level] = (counts[job.level] || 0) + 1;
    const box = $('level-chips');
    box.replaceChildren();
    for (const name of state.levelNames) {
      const chip = el('button', 'level-chip');
      chip.type = 'button';
      chip.dataset.level = name;
      chip.setAttribute('aria-pressed', String(state.levels.has(name)));
      chip.append(el('span', null, name), el('span', 'n', String(counts[name] || 0)));
      chip.addEventListener('click', () => {
        if (state.levels.has(name)) state.levels.delete(name); else state.levels.add(name);
        chip.setAttribute('aria-pressed', String(state.levels.has(name)));
        $('level-clear').hidden = !state.levels.size;
        state.shown = PAGE;
        renderList();
      });
      box.append(chip);
    }
  }
  $('level-clear').addEventListener('click', () => {
    state.levels.clear();
    document.querySelectorAll('.level-chip').forEach((c) => c.setAttribute('aria-pressed', 'false'));
    $('level-clear').hidden = true;
    state.shown = PAGE;
    renderList();
  });
  for (const id of ['sort', 'status', 'region']) {
    $(id).addEventListener('change', (e) => { state[id] = e.target.value; state.shown = PAGE; renderList(); });
  }
  $('more').addEventListener('click', () => { state.shown += PAGE; renderList(); });

  // Uploads
  function bufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  }
  function setStatus(text, isError) {
    const node = $('upload-status');
    node.textContent = text;
    node.classList.toggle('error', Boolean(isError));
  }

  async function scoreUpload(filename, contentBase64, fromUser) {
    setStatus('Scoring every job against ' + filename + '…');
    let response, data;
    try {
      response = await fetch('/api/score', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename, content_base64: contentBase64 }),
      });
      data = await response.json();
    } catch (e) {
      setStatus('Could not reach the app. Is python3 app.py still running?', true);
      return;
    }
    if (!response.ok) { setStatus(data.error || 'That file could not be scored.', true); return; }
    state.profile = data.profile;
    state.threshold = data.threshold;
    state.scores = new Map(data.scores.map((s) => [s.id, s]));
    if ($('remember').checked && fromUser) {
      const ok = contentBase64.length < MAX_REMEMBER_CHARS && store.set({ filename, content_base64: contentBase64 });
      if (!ok) setStatus('Scored, but this browser could not remember the file.');
    }
    const scoredCount = data.scores.filter(ranked).length;
    const thinCount = state.jobs.filter((j) => j.complete && !j.enough_detail).length;
    setStatus(`Scored ${scoredCount.toLocaleString()} jobs; ${fitCount().toLocaleString()} at or above ${state.threshold}. ${thinCount.toLocaleString()} had too little detail to score.`);
    renderProfile();
    renderStats();
    state.shown = PAGE;
    renderList();
  }

  function renderProfile() {
    const p = state.profile;
    $('profile-card').hidden = !p;
    if (!p) {
      $('banner-text').textContent = 'Upload your resume to see how you fit each job.';
      $('banner-action').textContent = 'Upload resume';
      return;
    }
    const mode = $('profile-mode');
    mode.textContent = p.mode === 'master' ? 'Master profile' : 'Plain resume';
    mode.className = 'tag ' + (p.mode === 'master' ? 'tag-green' : 'tag-yellow');
    $('profile-file').textContent = p.filename;
    $('profile-evidence').textContent = `${p.evidence_lines} evidence lines · ${p.skills.length} recognized skills`;
    const warning = $('profile-warning');
    warning.hidden = !p.warnings.length;
    warning.textContent = p.warnings.join(' ');
    const skills = $('profile-skills');
    skills.replaceChildren(...(p.skills.length ? p.skills.map((s) => el('span', 'chip match', s))
                                               : [el('span', 'muted', 'No recognized skills')]));
    $('profile-no-wrap').hidden = !p.no_skills.length;
    $('profile-no').replaceChildren(...p.no_skills.map((s) => el('span', 'chip no', s)));
    const scoredCount = [...state.scores.values()].filter(ranked).length;
    $('banner-text').textContent = `Scored against ${p.filename}: ${scoredCount.toLocaleString()} jobs, ${fitCount().toLocaleString()} at or above ${state.threshold}.`;
    $('banner-action').textContent = 'Change resume';
  }

  async function handleFile(file) {
    if (!file) return;
    if (file.size > 2_000_000) { setStatus('That file is larger than 2 MB.', true); return; }
    const buffer = await file.arrayBuffer();
    scoreUpload(file.name, bufferToBase64(buffer), true);
  }
  $('file').addEventListener('change', (e) => handleFile(e.target.files[0]));
  const drop = $('drop');
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => handleFile(e.dataTransfer.files[0]));
  $('score-paste').addEventListener('click', () => {
    const text = $('paste').value.trim();
    if (!text) { setStatus('Paste your resume text first.', true); return; }
    scoreUpload('pasted-resume.txt', bufferToBase64(new TextEncoder().encode(text)), true);
  });
  $('forget').addEventListener('click', () => {
    store.clear();
    state.profile = null;
    state.scores = new Map();
    $('file').value = '';
    $('paste').value = '';
    renderProfile(); renderStats(); renderList();
    setStatus('Removed from this browser.');
  });

  load().catch(() => { $('footer-index').textContent = 'Could not load the board. Is python3 app.py running?'; });
})();
