import type { NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { json, readJson, route } from '@/lib/http';
import { createReview, listReviews } from '@/lib/reviews';

export const GET = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  const p = request.nextUrl.searchParams;
  return json({ reviews: await listReviews(user, { company: p.get('company') ?? undefined,
    jobId: p.get('job_id') ?? undefined, mine: p.get('mine') === '1',
    offset: Number(p.get('offset')) || 0 }) });
});

export const POST = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  return json({ review: await createReview(user, await readJson(request)) }, 201);
});
