import type { NextRequest } from 'next/server';
import { requireUser, type User } from '@/lib/auth';
import { buildNeeds, resumeHeader } from '@/lib/board';
import { db } from '@/lib/db';
import { callEngine } from '@/lib/engine';
import { json, nowSeconds, readJson, route } from '@/lib/http';

export const maxDuration = 60;

async function view(user: User) {
  return { header: await resumeHeader(user.id), build: await buildNeeds(user) };
}

export const GET = route(async (request: NextRequest) => json(await view(await requireUser(request))));

/** Validated by user_template.clean_header in the engine, the rules the local build uses. */
export const POST = route(async (request: NextRequest) => {
  const user = await requireUser(request);
  const { header } = await callEngine<{ header: Record<string, unknown> }>(
    { action: 'header', header: await readJson(request) });
  await db()`INSERT INTO resume_headers (user_id, header, updated_at)
             VALUES (${user.id}, ${JSON.stringify(header)}, ${nowSeconds()})
             ON CONFLICT (user_id) DO UPDATE SET header = excluded.header, updated_at = excluded.updated_at`;
  return json(await view(user));
});
