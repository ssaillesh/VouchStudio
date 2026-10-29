import { NextResponse, type NextRequest } from 'next/server';

const SESSION_COOKIE = 'vouch_session';
const R2_HOSTS = 'https://*.r2.cloudflarestorage.com';
const MAX_BODY_BYTES = 4_000_000;

function refuse(detail: string, status = 403): NextResponse {
  return NextResponse.json({ detail }, { status, headers: { 'Cache-Control': 'no-store' } });
}

/**
 * API: state-changing requests must carry X-Vouch-Studio and, when the browser sends an
 * Origin, come from this site. A cross-site page cannot add the header without a CORS
 * preflight this app never grants (the same guard as server.py).
 * Pages: a per-request nonce CSP; signed-out visitors to /app go to /login.
 */
export function proxy(request: NextRequest): NextResponse {
  const { pathname } = request.nextUrl;
  if (pathname.startsWith('/api/')) {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      if (request.headers.get('x-vouch-studio') !== '1') return refuse('Missing X-Vouch-Studio header');
      const origin = request.headers.get('origin');
      if (origin && origin !== request.nextUrl.origin) return refuse('Cross-origin requests are refused');
      const length = Number(request.headers.get('content-length') || 0);
      if (length > MAX_BODY_BYTES) return refuse('Request body too large', 413);
    }
    return NextResponse.next();
  }
  if ((pathname === '/app' || pathname.startsWith('/app/')) && !request.cookies.has(SESSION_COOKIE)) {
    return NextResponse.redirect(new URL('/login', request.url), 303);
  }
  const nonce = Buffer.from(crypto.randomUUID()).toString('base64');
  const dev = process.env.NODE_ENV !== 'production';
  const csp = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ''}`,
    "style-src 'self' 'unsafe-inline'",
    `img-src 'self' data: blob: ${R2_HOSTS}`,
    `media-src 'self' blob: ${R2_HOSTS}`,
    `frame-src 'self' ${R2_HOSTS}`,
    `connect-src 'self' ${R2_HOSTS}${dev ? ' ws:' : ''}`,
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
  ].join('; ');
  const headers = new Headers(request.headers);
  headers.set('x-nonce', nonce);
  headers.set('Content-Security-Policy', csp);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set('Content-Security-Policy', csp);
  return response;
}

export const config = {
  // api/engine is the Python function (server-to-server, its own bearer secret).
  matcher: ['/((?!api/engine|_next/static|_next/image|static/|favicon.ico).*)'],
};
