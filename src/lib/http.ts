import { NextResponse, type NextRequest } from 'next/server';

/** A refusal the API answers as `{detail}` with this status (the shape server.py uses). */
export class HttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

export function json(data: unknown, status = 200): NextResponse {
  return NextResponse.json(data, { status, headers: { 'Cache-Control': 'no-store' } });
}

type Handler<C> = (request: NextRequest, context: C) => Promise<Response>;

/** Wrap a route handler: HttpError becomes its JSON answer; anything else a generic 500. */
export function route<C = unknown>(handler: Handler<C>): Handler<C> {
  return async (request, context) => {
    try {
      return await handler(request, context);
    } catch (err) {
      if (err instanceof HttpError) return json({ detail: err.message }, err.status);
      console.error(`${request.method} ${request.nextUrl.pathname}:`,
        err instanceof Error ? `${err.name}: ${err.message}` : 'unknown error');
      return json({ detail: 'Something went wrong on our side. Try again.' }, 500);
    }
  };
}

export async function readJson<T = Record<string, unknown>>(request: Request, max = 64_000): Promise<T> {
  const text = await request.text();
  if (text.length > max) throw new HttpError(413, 'Request body too large');
  if (!text) return {} as T;
  try {
    const value = JSON.parse(text);
    if (value && typeof value === 'object' && !Array.isArray(value)) return value as T;
  } catch { /* answered below */ }
  throw new HttpError(400, 'The body must be a JSON object');
}

/** A trimmed string field, or a 422 naming the field. */
export function text(value: unknown, label: string, max: number, required = false): string {
  if (value != null && typeof value !== 'string') throw new HttpError(422, `${label} must be text`);
  const out = (value ?? '').trim();
  if (required && !out) throw new HttpError(422, `Enter ${label}.`);
  if (out.length > max) throw new HttpError(422, `Keep ${label} under ${max} characters.`);
  return out;
}

export function clientIp(request: NextRequest): string {
  return request.headers.get('x-real-ip')
    || request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() || 'unknown';
}

export const nowSeconds = (): number => Date.now() / 1000;

/** Epoch seconds as a local-free ISO string, like server.py's isoformat(timespec="seconds"). */
export const isoSeconds = (epoch: number): string => new Date(epoch * 1000).toISOString().slice(0, 19);
