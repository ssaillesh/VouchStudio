import { timingSafeEqual } from 'node:crypto';
import type { NextRequest } from 'next/server';
import { db } from '@/lib/db';
import { HttpError, json, nowSeconds, route } from '@/lib/http';
import { remove } from '@/lib/r2';

export const maxDuration = 60;

/**
 * Daily (vercel.json "crons"): delete review attachments uploaded but never posted, expired
 * sessions and old throttle entries. Vercel sends `Authorization: Bearer $CRON_SECRET`.
 */
export const GET = route(async (request: NextRequest) => {
  const secret = process.env.CRON_SECRET;
  const given = Buffer.from(request.headers.get('authorization') ?? '');
  const expected = Buffer.from(`Bearer ${secret}`);
  if (!secret || given.length !== expected.length || !timingSafeEqual(given, expected)) {
    throw new HttpError(401, 'Unauthorized');
  }
  const stale = await db()`SELECT id, object_key FROM review_media
                           WHERE status = 'pending' AND created_at < now() - interval '1 day' LIMIT 500`;
  for (const media of stale) {
    await remove(media.object_key);
    await db()`DELETE FROM review_media WHERE id = ${media.id}`;
  }
  const now = nowSeconds();
  const sessions = await db()`DELETE FROM sessions WHERE expires_at < ${now} RETURNING token_hash`;
  await db()`DELETE FROM auth_attempts WHERE at < ${now - 24 * 3600}`;
  return json({ media_removed: stale.length, sessions_removed: sessions.length });
});
