import type { NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { buildView } from '@/lib/board';
import { db } from '@/lib/db';
import { json, route } from '@/lib/http';

export const GET = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  const rows = await db()`
    SELECT job_id, title, company, status, summary, created_at,
           (pdf IS NOT NULL OR pdf_key IS NOT NULL) AS has_pdf, (report IS NOT NULL) AS has_report
    FROM builds WHERE user_id = ${user.id} ORDER BY created_at DESC`;
  return json({ builds: rows.map(buildView) });
});
