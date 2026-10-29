import type { NextRequest } from 'next/server';
import { latestRequest } from '@/lib/accounts';
import { requireUser } from '@/lib/auth';
import { json, route } from '@/lib/http';

export const GET = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  return json({ user, pro_request: await latestRequest(user.id) });
});
