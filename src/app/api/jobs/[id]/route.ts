import type { NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { ensureScores, myBuild, myScore, publicJob } from '@/lib/board';
import { db } from '@/lib/db';
import { HttpError, json, route } from '@/lib/http';

export const maxDuration = 60;

export const GET = route(async (request: NextRequest, { params }: { params: Promise<{ id: string }> }) => {
  const user = await requireUser(request);
  const { id } = await params;
  const rows = await db()`SELECT * FROM jobs WHERE id = ${id}`;
  if (!rows.length) throw new HttpError(404, 'Unknown job');
  const profile = await ensureScores(user);
  return json({ ...publicJob(rows[0]), text: rows[0].description, profile,
    my_score: await myScore(user, id), my_build: await myBuild(user.id, id) });
});
