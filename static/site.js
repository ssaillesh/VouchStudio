/* Vouch public pages: the home page and the sign-up / log-in form.
 *
 * Nodes are filled with textContent only. State-changing requests carry
 * X-Vouch-Studio, which the server requires and a cross-site page cannot send.
 */
'use strict';

const $ = (id) => document.getElementById(id);

function revealAll() {
  const nodes = document.querySelectorAll('.reveal');
  const groups = new Map();
  for (const node of nodes) {  // stagger siblings that share a parent
    const n = groups.get(node.parentNode) || 0;
    node.style.setProperty('--index', String(Math.min(n, 8)));
    groups.set(node.parentNode, n + 1);
  }
  if (!('IntersectionObserver' in window)) { nodes.forEach((n) => n.classList.add('in')); return; }
  const io = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) { entry.target.classList.add('in'); io.unobserve(entry.target); }
    }
  }, { rootMargin: '0px 0px -40px 0px' });
  nodes.forEach((n) => io.observe(n));
}

async function getJson(path) {
  const res = await fetch(path, { credentials: 'same-origin' });
  return res.ok ? res.json() : null;
}

async function postJson(path, body) {
  const res = await fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'X-Vouch-Studio': '1', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let detail = data.detail || 'Something went wrong. Try again.';
    if (Array.isArray(detail)) detail = 'Check the fields and try again.';
    throw new Error(detail);
  }
  return data;
}

// ---------------------------------------------------------------------------
// Home
// ---------------------------------------------------------------------------
async function initHome() {
  const [me, stats] = await Promise.all([getJson('/api/me'), getJson('/api/stats')]);
  if (stats) {
    $('hero-stats').textContent = `${stats.jobs.toLocaleString()} open roles · `
      + `${stats.companies.toLocaleString()} companies · US and Canada`;
  }
  if (me && me.user) {
    const open = (el, text) => { el.href = '/app'; el.textContent = text; };
    const actions = $('site-actions');
    const link = document.createElement('a');
    link.className = 'btn primary';
    link.href = '/app';
    link.textContent = 'Open your board';
    actions.replaceChildren(link);
    open($('hero-primary'), 'Open your board');
    $('hero-secondary').hidden = true;
    open($('closing-cta'), 'Open your board');
  }
}

// ---------------------------------------------------------------------------
// Sign up and log in (one page; the path picks the mode)
// ---------------------------------------------------------------------------
function initAuth() {
  const login = location.pathname.startsWith('/login');
  if (login) {
    document.title = 'VouchStudio: log in';
    $('auth-eyebrow').textContent = 'Log in';
    $('auth-title').textContent = 'Welcome back.';
    $('auth-lede').textContent = 'Log in to see the board scored against your profile.';
    $('name-field').hidden = true;
    $('password-hint').hidden = true;
    $('auth-password').autocomplete = 'current-password';
    $('auth-submit').textContent = 'Log in';
    const sw = $('auth-switch');
    sw.replaceChildren('New to VouchStudio? ');
    const a = document.createElement('a');
    a.href = '/signup';
    a.textContent = 'Create an account';
    sw.append(a);
  }
  const form = $('auth-form');
  const error = $('auth-error');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    error.textContent = '';
    const email = $('auth-email').value.trim();
    const password = $('auth-password').value;
    if (!email || !password) { error.textContent = 'Enter your email and password.'; return; }
    if (!login && password.length < 8) { error.textContent = 'Use a password of at least 8 characters.'; return; }
    const button = $('auth-submit');
    button.disabled = true;
    try {
      if (login) {
        await postJson('/api/auth/login', { email, password });
        location.href = '/app';
      } else {
        await postJson('/api/auth/signup', { email, password, name: $('auth-name').value.trim() });
        location.href = '/app#/me';  // first stop: upload a profile
      }
    } catch (err) {
      error.textContent = err.message;
      button.disabled = false;
    }
  });
  $('auth-email').focus();
}

document.addEventListener('DOMContentLoaded', () => {
  revealAll();
  const page = document.body.dataset.page;
  if (page === 'home') initHome().catch(() => { /* the static page still reads fine */ });
  else if (page === 'auth') initAuth();
});
