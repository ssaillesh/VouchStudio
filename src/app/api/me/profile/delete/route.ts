import type { NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { deleteProfile, myProfile } from '@/lib/board';
import { json, route } from '@/lib/http';

export const POST = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  await deleteProfile(user);
  return json(await myProfile(user));
});
