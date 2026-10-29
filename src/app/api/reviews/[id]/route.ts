import type { NextRequest } from 'next/server';
import { requireAdmin, requireUser } from '@/lib/auth';
import { db } from '@/lib/db';
import { HttpError, json, readJson, route } from '@/lib/http';
import { deleteReview } from '@/lib/reviews';

type Context = { params: Promise<{ id: string }> };

async function reviewId(context: Context): Promise<number> {
  const id = Number((await context.params).id);
  if (!Number.isInteger(id) || id <= 0) throw new HttpError(404, 'No such review.');
  return id;
}

/** The author or an admin deletes a review and its attachments. */
export const DELETE = route(async (request: NextRequest, context: Context) => {
  await deleteReview(await requireUser(request), await reviewId(context));
  return json({ deleted: true });
});

/** Admins hide a review from everyone but its author, or publish it again. */
export const PATCH = route(async (request: NextRequest, context: Context) => {
  await requireAdmin(request);
  const { status } = await readJson(request);
  if (status !== 'published' && status !== 'hidden') throw new HttpError(422, 'status is published or hidden.');
  const rows = await db()`UPDATE reviews SET status = ${status}, updated_at = now()
                          WHERE id = ${await reviewId(context)} RETURNING id`;
  if (!rows.length) throw new HttpError(404, 'No such review.');
  return json({ id: rows[0].id, status });
});
