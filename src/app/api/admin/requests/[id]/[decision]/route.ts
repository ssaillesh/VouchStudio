import type { NextRequest } from 'next/server';
import { adminOverview, decideRequest } from '@/lib/accounts';
import { requireAdmin } from '@/lib/auth';
import { HttpError, json, route } from '@/lib/http';

export const POST = route(async (request: NextRequest,
  { params }: { params: Promise<{ id: string; decision: string }> }) => {
  const admin = await requireAdmin(request);
  const { id, decision } = await params;
  if (decision !== 'approve' && decision !== 'decline') throw new HttpError(404, 'Unknown decision');
  await decideRequest(Number(id), decision === 'approve', admin);
  return json(await adminOverview());
});
