import type { NextRequest } from 'next/server';
import { adminOverview } from '@/lib/accounts';
import { requireAdmin } from '@/lib/auth';
import { json, route } from '@/lib/http';

export const GET = route(async (request: NextRequest) => {
  await requireAdmin(request);
  return json(await adminOverview());
});
