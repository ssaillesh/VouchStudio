'use client';

// The public pages' live bits (port of static/site.js): board counts on the home page, the
// calls to action for a signed-in visitor, and the sign-up / log-in form.
import { useEffect, useState } from 'react';
import { api, ApiError } from './ui';

let mePromise: Promise<{ user: unknown } | null> | null = null;
function useSignedIn(): boolean {
  const [signedIn, setSignedIn] = useState(false);
  useEffect(() => {
    mePromise ??= fetch('/api/me', { credentials: 'same-origin' }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    mePromise.then((me) => setSignedIn(Boolean(me?.user)));
  }, []);
  return signedIn;
}

export function SiteActions() {
  const signedIn = useSignedIn();
  if (signedIn) return <nav className="site-actions" aria-label="Account"><a className="btn primary" href="/app">Open your board</a></nav>;
  return (
    <nav className="site-actions" aria-label="Account">
      <a className="btn ghost" href="/login">Log in</a>
      <a className="btn primary" href="/signup">Create account</a>
    </nav>
  );
}

export function HeroActions() {
  const signedIn = useSignedIn();
  return (
    <div className="hero-actions">
      <a className="btn primary large" href={signedIn ? '/app' : '/signup'}>
        {signedIn ? 'Open your board' : 'Create a free account'}</a>
      {signedIn ? null : <a className="btn large" href="/login">Log in</a>}
    </div>
  );
}

export function ClosingCta() {
  const signedIn = useSignedIn();
  return <a className="btn invert large" href={signedIn ? '/app' : '/signup'}>
    {signedIn ? 'Open your board' : 'Create a free account'}</a>;
}

export function HeroStats() {
  const [text, setText] = useState('Open roles across the US and Canada');
  useEffect(() => {
    fetch('/api/stats').then((r) => (r.ok ? r.json() : null)).then((stats) => {
      if (stats?.jobs) {
        setText(`${stats.jobs.toLocaleString()} open roles · ${stats.companies.toLocaleString()} companies · US and Canada`);
      }
    }).catch(() => { /* the static line still reads fine */ });
  }, []);
  return <p className="hero-stats" aria-live="polite">{text}</p>;
}

export function AuthForm({ mode }: { mode: 'login' | 'signup' }) {
  const login = mode === 'login';
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return (
    <form noValidate onSubmit={async (event) => {
      event.preventDefault();
      setError('');
      const form = new FormData(event.currentTarget);
      const email = String(form.get('email') || '').trim();
      const password = String(form.get('password') || '');
      if (!email || !password) { setError('Enter your email and password.'); return; }
      if (!login && password.length < 8) { setError('Use a password of at least 8 characters.'); return; }
      setBusy(true);
      try {
        if (login) {
          await api('/api/auth/login', { method: 'POST', body: { email, password } });
          window.location.href = '/app';
        } else {
          await api('/api/auth/signup', { method: 'POST',
            body: { email, password, name: String(form.get('name') || '').trim() } });
          window.location.href = '/app/me';  // first stop: upload a profile
        }
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Something went wrong. Try again.');
        setBusy(false);
      }
    }}>
      {login ? null : (
        <label className="field"><span>Name <span className="faint">(optional)</span></span>
          <input type="text" name="name" autoComplete="name" maxLength={80} />
        </label>
      )}
      <label className="field">Email
        <input type="email" name="email" autoComplete="email" required maxLength={254} autoFocus />
      </label>
      <label className="field">Password
        <input type="password" name="password" autoComplete={login ? 'current-password' : 'new-password'}
          required minLength={8} maxLength={256} />
        {login ? null : <span className="hint">At least 8 characters.</span>}
      </label>
      <p className="form-error" role="alert">{error}</p>
      <button className="btn primary large block" type="submit" disabled={busy}>
        {login ? 'Log in' : 'Create account'}</button>
    </form>
  );
}
