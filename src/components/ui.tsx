'use client';

// Shared pieces of the hosted UI, ported from static/studio.js with the same classes so
// static/studio.css styles both. React escapes every text node, and links render only for
// http(s) URLs, so job descriptions and reviews can never inject markup.
import { createElement, useEffect, useRef, type CSSProperties, type ReactNode } from 'react';

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

/** JSON API call; state-changing requests carry X-Vouch-Studio, which the server requires. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export async function api<T = any>(path: string, { method = 'GET', body }: { method?: string; body?: unknown } = {}): Promise<T> {
  const options: RequestInit = { method, headers: {}, credentials: 'same-origin' };
  if (method !== 'GET') {
    options.headers = { 'X-Vouch-Studio': '1', 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body ?? {});
  }
  const res = await fetch(path, options);
  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    window.location.href = '/login';
    throw new ApiError(401, 'Log in to continue');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, typeof data.detail === 'string' ? data.detail : res.statusText);
  return data as T;
}

export function MarkSvg({ className = 'mark' }: { className?: string }) {
  return (
    <svg className={className} viewBox="2 12 60 43" aria-hidden="true" focusable="false">
      <path d="M2 12H20L31 34L34.2 29.6L42.2 40.2L35.4 51.6C33.4 55.2 28.6 55.2 26.6 51.6Z" />
      <path d="M36.6 26.4L46.4 12H62L44.6 37.4Z" />
    </svg>
  );
}

export function Brand({ small = false }: { small?: boolean }) {
  return (
    <a className={`brand${small ? ' small' : ''}`} href="/" aria-label="VouchStudio home">
      <MarkSvg /><span className="wordmark">VouchStudio</span>
    </a>
  );
}

const ICON_PATHS: Record<string, string[]> = {
  external: ['M8 16 16 8', 'M9.5 8H16v6.5'],
  check: ['M5 12.5 9.5 17 19 7.5'],
  cross: ['M7 7l10 10', 'M17 7 7 17'],
  arrow: ['M5 12h14', 'M13 6l6 6-6 6'],
  star: ['M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z'],
};

export function Icon({ name, size = 13 }: { name: keyof typeof ICON_PATHS; size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={2.25}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="icon">
      {ICON_PATHS[name].map((d) => <path key={d} d={d} />)}
    </svg>
  );
}

export function Chip({ kind = '', title, children }: { kind?: string; title?: string; children: ReactNode }) {
  return <span className={`chip ${kind}`} title={title}>{children}</span>;
}

export function regionKind(region: string): string {
  return region.includes('US') ? 'accent tag' : region.includes('Canada') ? 'warn tag' : 'tag';
}

export function safeUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw.includes('://') ? raw : `https://${raw}`);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

export function ExtLink({ href, children }: { href: string | null | undefined; children?: ReactNode }) {
  const url = safeUrl(href);
  if (!url) return <>{children ?? href}</>;
  return <a href={url} target="_blank" rel="noopener noreferrer">{children ?? href}</a>;
}

let observer: IntersectionObserver | null = null;
function revealObserver(): IntersectionObserver | null {
  if (typeof window === 'undefined' || !('IntersectionObserver' in window)) return null;
  observer ??= new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        entry.target.classList.add('in');
        observer?.unobserve(entry.target);
      }
    }
  }, { rootMargin: '0px 0px -40px 0px' });
  return observer;
}

/** Fade a block in as it enters the viewport; `index` staggers siblings (80 ms each). */
export function Reveal({ as = 'div', index = 0, className = '', children, ...rest }: {
  as?: string; index?: number; className?: string; children?: ReactNode;
  [attr: string]: unknown;
}) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = revealObserver();
    if (io) io.observe(el); else el.classList.add('in');
    return () => io?.unobserve(el);
  }, []);
  return createElement(as, { ref, className: `reveal ${className}`.trim(),
    style: { '--index': String(Math.min(index, 8)) } as CSSProperties, ...rest }, children);
}

/** Every static `.reveal` on a server-rendered page (the home and auth pages). */
export function RevealAll() {
  useEffect(() => {
    const groups = new Map<Node, number>();
    const io = revealObserver();
    document.querySelectorAll<HTMLElement>('.reveal:not(.in)').forEach((node) => {
      const n = groups.get(node.parentNode as Node) ?? 0;
      node.style.setProperty('--index', String(Math.min(n, 8)));
      groups.set(node.parentNode as Node, n + 1);
      if (io) io.observe(node); else node.classList.add('in');
    });
  }, []);
  return null;
}

export function EmptyState({ title, text }: { title: string; text: ReactNode }) {
  return <div className="panel empty"><strong>{title}</strong>{text}</div>;
}

export interface ScoredJob {
  score: number | null;
  insufficient?: boolean;
  complete?: boolean;
  requirement_count?: number | null;
}

export function ScoreBadge({ job, threshold, inline = false }: { job: ScoredJob; threshold: number; inline?: boolean }) {
  const cls = inline ? ' inline' : '';
  if (job.insufficient) {
    return <span className={`score thin${cls}`} title="Fewer than 3 recognized skill requirements">Too little detail</span>;
  }
  if (job.score == null) {
    return <span className={`score thin${cls}`} title="No full description to score">
      {job.complete === false ? 'Listing' : 'Not scored'}</span>;
  }
  const kind = job.score >= threshold ? 'proceed' : job.score >= 40 ? 'mid' : '';
  const n = job.requirement_count;
  return (
    <span className={`score ${kind}${cls}`}>{job.score}
      {!inline && n != null ? <small>{n} req{n === 1 ? '' : 's'}</small> : null}
    </span>
  );
}

export const GAP_LABEL: Record<string, string> = { explicitly_no: 'no', not_evidenced: 'missing', ambiguous: 'unclear' };
export const statusLabel = (s: string | null | undefined) => String(s || '').replace(/_/g, ' ');

/** A fetched posting without its ingestion header (title and labelled metadata lines). */
export function postingBody(text: string): string {
  const match = /^##\s+Description\s*$/m.exec(text || '');
  return match ? text.slice(match.index + match[0].length) : text;
}

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
  let last = 0;
  for (const m of text.matchAll(pattern)) {
    out.push(text.slice(last, m.index));
    if (m[1]) out.push(<strong key={m.index}>{m[1]}</strong>);
    else if (m[2]) out.push(<code key={m.index}>{m[2]}</code>);
    else out.push(<ExtLink key={m.index} href={m[4]}>{m[3]}</ExtLink>);
    last = (m.index ?? 0) + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}

/** Minimal, safe Markdown: headings, lists, paragraphs, bold, code and http(s) links. */
export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: string[] | null = null;
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) blocks.push(<p key={blocks.length}>{inline(para.join(' '))}</p>);
    para = [];
  };
  const flushList = () => {
    if (list) blocks.push(<ul key={blocks.length}>{list.map((item, i) => <li key={i}>{inline(item)}</li>)}</ul>);
    list = null;
  };
  for (const rawLine of String(text || '').split('\n')) {
    const line = rawLine.trimEnd();
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    const bullet = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (heading) {
      flushPara(); flushList();
      blocks.push(createElement(`h${Math.min(heading[1].length + 1, 6)}`, { key: blocks.length }, inline(heading[2])));
    } else if (bullet) {
      flushPara();
      (list ??= []).push(bullet[1]);
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      flushList();
      para.push(line.trim());
    }
  }
  flushPara(); flushList();
  return <div className="jd">{blocks}</div>;
}

export function useDebounced<A extends unknown[]>(fn: (...args: A) => void, ms: number): (...args: A) => void {
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const latest = useRef(fn);
  latest.current = fn;
  return (...args: A) => {
    clearTimeout(timer.current);
    timer.current = setTimeout(() => latest.current(...args), ms);
  };
}

/** PUT a file to a presigned R2 URL, reporting progress (fetch cannot report uploads). */
export function uploadToR2(url: string, file: Blob, headers: Record<string, string>,
  onProgress?: (fraction: number) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress?.(e.loaded / e.total); };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve()
      : reject(new Error(`The upload failed (HTTP ${xhr.status}).`)));
    xhr.onerror = () => reject(new Error('The upload failed. Check your connection and try again.'));
    xhr.send(file);
  });
}
