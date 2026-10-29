import type { NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { boardMeta } from '@/lib/board';
import { db } from '@/lib/db';
import { json, route } from '@/lib/http';

export const GET = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  const meta = await boardMeta();
  const out: Record<string, unknown> = { jobs: meta?.jobs ?? 0, index_generated: meta?.generated ?? null,
    threshold: meta?.threshold ?? 65, levels: meta?.levels ?? [],
    min_skill_requirements: meta?.min_skill_requirements ?? 3, user };
  if (user.is_admin) {
    const [row] = await db()`SELECT count(*)::int AS n FROM pro_requests WHERE status = 'pending'`;
    out.pending_requests = row.n;
  }
  return json(out);
});
