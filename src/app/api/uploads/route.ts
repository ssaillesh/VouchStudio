import type { NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { PROFILE_TYPES, extension, profileUploadKey } from '@/lib/board';
import { HttpError, json, readJson, route, text } from '@/lib/http';
import { presignPut } from '@/lib/r2';
import { createMediaUpload } from '@/lib/reviews';

const MAX_PROFILE_BYTES = 2_000_000;

/**
 * A presigned PUT URL for one file, straight to R2. `purpose` is "profile" (a resume or
 * master profile) or "review" (an image, PDF or video for a review). The type and size
 * are signed into the URL and checked again before the file is used.
 */
export const POST = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  const body = await readJson(request);
  const filename = text(body.filename, 'a file name', 200, true);
  const size = Number(body.size);
  if (body.purpose === 'profile') {
    if (!Number.isInteger(size) || size <= 0) throw new HttpError(422, 'The file is empty.');
    if (size > MAX_PROFILE_BYTES) throw new HttpError(413, 'That file is larger than 2 MB.');
    const key = profileUploadKey(user, filename);
    const contentType = PROFILE_TYPES[extension(filename)];
    return json({ key, url: await presignPut(key, contentType, size), headers: { 'Content-Type': contentType } });
  }
  if (body.purpose === 'review') {
    return json(await createMediaUpload(user, filename, String(body.content_type || ''), size));
  }
  throw new HttpError(400, 'purpose is "profile" or "review".');
});
