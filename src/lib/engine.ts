// Calls api/engine.py, the Python scorer deployed as a Vercel Function beside this app.
// The URL never comes from the request (a forged Host header must not receive profiles or
// the secret): ENGINE_URL when set (local dev), else this deployment's own Vercel URL.
import { HttpError } from './http';

const TIMEOUT_MS = 55_000;

function engineUrl(): string {
  if (process.env.ENGINE_URL) return process.env.ENGINE_URL;
  const host = process.env.VERCEL_ENV === 'production'
    ? process.env.VERCEL_PROJECT_PRODUCTION_URL : process.env.VERCEL_URL;
  if (!host) throw new HttpError(503, 'The engine is not configured (ENGINE_URL).');
  return `https://${host}/api/engine`;
}

export async function callEngine<T>(payload: Record<string, unknown>): Promise<T> {
  const secret = process.env.ENGINE_SECRET;
  if (!secret) throw new HttpError(503, 'The engine is not configured (ENGINE_SECRET).');
  const headers: Record<string, string> = { 'Content-Type': 'application/json',
    Authorization: `Bearer ${secret}` };
  if (process.env.VERCEL_AUTOMATION_BYPASS_SECRET) {  // preview deployments behind Vercel login
    headers['x-vercel-protection-bypass'] = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  }
  let res: Response;
  try {
    res = await fetch(engineUrl(), { method: 'POST', headers, body: JSON.stringify(payload),
      cache: 'no-store', signal: AbortSignal.timeout(TIMEOUT_MS) });
  } catch {
    throw new HttpError(502, 'The scoring engine could not be reached. Try again in a minute.');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : `Engine error ${res.status}`;
    throw new HttpError(res.status === 422 ? 422 : 502, detail);
  }
  return data as T;
}
