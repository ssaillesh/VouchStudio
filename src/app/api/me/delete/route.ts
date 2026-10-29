import type { NextRequest } from 'next/server';
import { authenticate, clearSessionCookie, requireUser } from '@/lib/auth';
import { db } from '@/lib/db';
import { HttpError, json, readJson, route } from '@/lib/http';
import { removePrefix } from '@/lib/r2';

/** Delete the account: its R2 folder (profile, PDFs, review media), then every row. */
export const POST = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  const body = await readJson(request);
  if (!(await authenticate(user.email, body.password))) throw new HttpError(403, 'That password is not correct.');
  await removePrefix(`users/${user.id}/`);
  await db()`DELETE FROM users WHERE id = ${user.id}`;  // sessions, scores, reviews cascade
  const response = json({ deleted: true });
  clearSessionCookie(response);
  return response;
});
