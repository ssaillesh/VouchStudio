import type { NextRequest } from 'next/server';
import { adminOverview, setPlan } from '@/lib/accounts';
import { requireAdmin } from '@/lib/auth';
import { json, readJson, route } from '@/lib/http';

export const POST = route(async (request: NextRequest, { params }: { params: Promise<{ id: string }> }) => {
  await requireAdmin(request);
  await setPlan(Number((await params).id), (await readJson(request)).plan);
  return json(await adminOverview());
});
