import { NextResponse, type NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { db } from '@/lib/db';
import { HttpError, route } from '@/lib/http';

export const GET = route(async (request: NextRequest, { params }: { params: Promise<{ jobId: string }> }) => {
  const user = await requireUser(request);
  const { jobId } = await params;
  const rows = await db()`SELECT report FROM builds WHERE user_id = ${user.id} AND job_id = ${jobId}`;
  if (!rows[0]?.report) throw new HttpError(404, 'No report for this job yet');
  return new NextResponse(rows[0].report, { headers: { 'Content-Type': 'text/markdown; charset=utf-8',
    'Cache-Control': 'no-store' } });
});
