'use client';

import { usePathname } from 'next/navigation';
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';
import { api, Brand, Chip } from './ui';

export interface User {
  id: number; email: string; name: string; is_admin: boolean; plan: string; is_pro: boolean;
}
export interface ProRequest { id: number; status: string; created_at: number; note: string }
export interface Meta {
  jobs: number; index_generated: string | null; threshold: number; levels: string[];
  min_skill_requirements: number; pending_requests?: number;
}

interface Session {
  user: User;
  proRequest: ProRequest | null;
  setProRequest: (r: ProRequest | null) => void;
  meta: Meta;
  toast: (message: string, kind?: string) => void;
}

const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error('useSession outside the app shell');
  return session;
}

const VIEWS: [RegExp, string][] = [[/^\/app\/jobs\//, 'jobview'], [/^\/app\/me/, 'me'],
  [/^\/app\/admin/, 'admin'], [/^\/app\/reviews/, 'reviews'], [/^\/app\/?$/, 'jobs']];

/** The signed-in frame: top bar, navigation, user menu and toasts around every /app page. */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const [proRequest, setProRequest] = useState<ProRequest | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [toasts, setToasts] = useState<{ id: number; message: string; kind: string }[]>([]);

  const toast = useCallback((message: string, kind = '') => {
    const id = Date.now() + Math.random();
    setToasts((all) => [...all, { id, message, kind }]);
    setTimeout(() => setToasts((all) => all.filter((t) => t.id !== id)), kind === 'fail' ? 7000 : 4000);
  }, []);

  useEffect(() => {
    try { localStorage.removeItem('vouch.resume.v1'); } catch { /* storage unavailable */ }
    Promise.all([api('/api/me'), api('/api/meta')]).then(([me, m]) => {
      setUser(me.user);
      setProRequest(me.pro_request);
      setMeta(m);
    }).catch(() => { /* api() has already sent a signed-out visitor to /login */ });
  }, []);

  useEffect(() => {
    document.body.dataset.view = VIEWS.find(([re]) => re.test(pathname))?.[1] ?? 'jobs';
    window.scrollTo(0, 0);
  }, [pathname]);

  const nav: [string, string, boolean][] = [['/app', 'Jobs', pathname === '/app' || pathname.startsWith('/app/jobs')],
    ['/app/me', 'Your profile', pathname.startsWith('/app/me')],
    ['/app/reviews', 'Reviews', pathname.startsWith('/app/reviews')]];
  if (user?.is_admin) nav.push(['/app/admin', 'Admin', pathname.startsWith('/app/admin')]);

  return (
    <>
      <header className="topbar">
        <Brand />
        <nav className="nav" aria-label="Main">
          {nav.map(([href, label, active]) => (
            <a key={href} href={href} className={active ? 'active' : undefined}>{label}
              {href === '/app/admin' && meta?.pending_requests ? <span className="badge">{meta.pending_requests}</span> : null}
            </a>
          ))}
        </nav>
        <div className="status">
          {user ? (
            <span className="user-menu">
              <span className="who" title={user.email}>{user.name || user.email}</span>
              {user.is_admin ? <Chip kind="tag accent">Admin</Chip>
                : <Chip kind={`tag ${user.is_pro ? 'pass' : ''}`}>{user.is_pro ? 'Pro' : 'Basic'}</Chip>}
              <button className="btn small" type="button" onClick={async () => {
                try { await api('/api/auth/logout', { method: 'POST' }); } finally { window.location.href = '/'; }
              }}>Log out</button>
            </span>
          ) : null}
        </div>
      </header>
      <main id="view" aria-live="polite">
        {user && meta ? (
          <SessionContext.Provider value={{ user, proRequest, setProRequest, meta, toast }}>
            {children}
          </SessionContext.Provider>
        ) : <div className="panel empty">Loading…</div>}
      </main>
      <div className="toasts" aria-live="assertive">
        {toasts.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.message}</div>)}
      </div>
    </>
  );
}
