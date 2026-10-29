import type { NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { listJobs } from '@/lib/board';
import { HttpError, json, route } from '@/lib/http';

export const maxDuration = 60;

function int(value: string | null, fallback: number, min: number, max: number): number {
  if (value == null || value === '') return fallback;
  const n = Number(value);
  if (!Number.isInteger(n) || n < min || n > max) throw new HttpError(422, `Out of range: ${value}`);
  return n;
}

export const GET = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  const p = request.nextUrl.searchParams;
  const q = p.get('q') ?? '';
  if (q.length > 200) throw new HttpError(422, 'Search is limited to 200 characters');
  return json(await listJobs(user, {
    q, region: p.get('region') ?? '', level: p.get('level') ?? '', source: p.get('source') ?? '',
    verdict: p.get('verdict') ?? '', sort: p.get('sort') ?? 'match',
    min_score: p.get('min_score') ? int(p.get('min_score'), 0, 0, 100) : null,
    offset: int(p.get('offset'), 0, 0, 1_000_000), limit: int(p.get('limit'), 50, 1, 200),
  }));
});
