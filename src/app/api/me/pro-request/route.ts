import type { NextRequest } from 'next/server';
import { requestPro } from '@/lib/accounts';
import { requireUser } from '@/lib/auth';
import { json, readJson, route } from '@/lib/http';

export const POST = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  return json({ pro_request: await requestPro(user, (await readJson(request)).note) });
});
