import type { NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { buildNeeds, myBuild } from '@/lib/board';
import { db } from '@/lib/db';
import { HttpError, json, nowSeconds, readJson, route } from '@/lib/http';

export const maxDuration = 60;

const MESSAGES: Record<string, string> = {
  profile: 'Upload your master profile first.',
  master: 'Building a resume needs a tagged master profile ([V] and [NO] lines), not a plain resume.',
  kit: 'Your master profile needs a resume kit (section 9) with your roles, projects and bullet bank.',
  header: 'Add your resume header (at least your name) first.',
};

/**
 * Queue a tailored resume. Vercel has no LaTeX, so the build waits as 'queued' until the
 * owner's machine runs `./run_ui.sh --process-builds`; an earlier PDF stays until then.
 */
export const POST = route(async (request: NextRequest, { params }: { params: Promise<{ jobId: string }> }) => {
  const user = await requireUser(request);
  const { jobId } = await params;
  if (!user.is_pro) {
    throw new HttpError(403, 'Tailored resumes are a Pro feature. Request Pro on your profile page.');
  }
  const needs = await buildNeeds(user);
  if (needs.missing.length) throw new HttpError(422, MESSAGES[needs.missing[0]]);
  const jobs = await db()`SELECT title, company FROM jobs WHERE id = ${jobId}`;
  if (!jobs.length) throw new HttpError(404, 'Unknown job');
  const body = await readJson(request);
  const summary = JSON.stringify({ outcome: 'queued', override_low_fit: body.override_low_fit === true });
  await db()`
    INSERT INTO builds (user_id, job_id, title, company, status, summary, created_at)
    VALUES (${user.id}, ${jobId}, ${jobs[0].title}, ${jobs[0].company}, 'queued', ${summary}, ${nowSeconds()})
    ON CONFLICT (user_id, job_id) DO UPDATE SET status = 'queued', summary = excluded.summary,
      title = excluded.title, company = excluded.company, created_at = excluded.created_at`;
  return json({ build: await myBuild(user.id, jobId) });
});
