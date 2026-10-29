import type { NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { myProfile, saveProfile } from '@/lib/board';
import { HttpError, json, readJson, route, text } from '@/lib/http';
import * as r2 from '@/lib/r2';

export const maxDuration = 60;

export const GET = route(async (request: NextRequest) => json(await myProfile(await requireUser(request))));

/**
 * Save the profile that scores this account's board, then score every job against it.
 * Either `{filename, upload_key}` after the browser PUT the file to R2 (POST /api/uploads),
 * or `{filename, text}` for pasted or edited text (stored to R2 from here).
 */
export const POST = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  const body = await readJson(request, 2_200_000);
  const filename = text(body.filename, 'a file name', 200, true);
  if (typeof body.upload_key === 'string') {
    const key = body.upload_key;
    if (!key.startsWith(`users/${user.id}/profile/`) || key.includes('..')) {
      throw new HttpError(403, 'That upload belongs to another account.');
    }
    const found = await r2.head(key);
    if (!found) throw new HttpError(422, 'The upload did not finish; try again.');
    await saveProfile(user, filename, await r2.getBytes(key), key);
  } else if (typeof body.text === 'string') {
    if (!body.text.trim()) throw new HttpError(422, 'Paste your resume text first.');
    await saveProfile(user, filename, new TextEncoder().encode(body.text), null);
  } else {
    throw new HttpError(400, 'Send upload_key or text.');
  }
  return json(await myProfile(user));
});
