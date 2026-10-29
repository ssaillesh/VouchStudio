// The board on the hosted app: published jobs in Neon, each account's scores cached in
// Neon, and the profile those scores come from in R2. Scores are computed by api/engine.py
// (the same jobboard code as the local Studio) once per profile and published index.
import { createHash, randomUUID } from 'node:crypto';
import type { User } from './auth';
import { db, type Row } from './db';
import { callEngine } from './engine';
import { HttpError, isoSeconds, nowSeconds } from './http';
import { pdfText } from './pdf';
import * as r2 from './r2';

export const PAGE_SIZE_MAX = 200;
export const PROFILE_TYPES: Record<string, string> = {
  '.md': 'text/markdown; charset=utf-8', '.txt': 'text/plain; charset=utf-8', '.pdf': 'application/pdf',
};

export interface BoardMeta {
  key: string;
  sha256: string;
  generated: string;
  jobs: number;
  threshold: number;
  verdict_proceed: string;
  levels: string[];
  min_skill_requirements: number;
  max_profile_bytes: number;
}

export interface ProfileSummary {
  mode?: string;
  filename: string;
  evidence_lines?: number;
  skills?: string[];
  no_skills?: string[];
  warnings?: string[];
  is_master?: boolean;
  has_kit?: boolean;
  error?: string;
  source?: string;
}

interface EngineScore {
  profile: ProfileSummary;
  scores: Row[];
  index_sha256: string;
}

interface ProfileRow {
  filename: string;
  sha256: string;
  object_key: string | null;
  updated_at: number;
}

export async function boardMeta(): Promise<BoardMeta | null> {
  const rows = await db()`SELECT value FROM board WHERE key = 'index'`;
  return rows.length ? (rows[0].value as BoardMeta) : null;
}

export async function requireBoard(): Promise<BoardMeta> {
  const meta = await boardMeta();
  if (!meta) throw new HttpError(503, 'No jobs are published yet. Run `npm run publish:jobs`.');
  return meta;
}

export const extension = (filename: string) => (/\.[^./\\]+$/.exec(filename.toLowerCase())?.[0] ?? '');
export const safeFilename = (filename: string) =>
  (filename.split(/[\\/]/).pop() || 'profile').replace(/[^\w.\- ()]/g, '_').slice(0, 120);

async function profileRow(userId: number): Promise<ProfileRow | null> {
  const rows = await db()`SELECT filename, sha256, object_key, updated_at FROM profiles
                          WHERE user_id = ${userId}`;
  return rows.length ? (rows[0] as ProfileRow) : null;
}

/** The stored file: an R2 object, or bytes in the row when the local Studio saved it there. */
async function profileBytes(userId: number, row: ProfileRow): Promise<Uint8Array> {
  if (row.object_key) return r2.getBytes(row.object_key);
  const rows = await db()`SELECT encode(content, 'base64') AS b64 FROM profiles WHERE user_id = ${userId}`;
  return Buffer.from(rows[0]?.b64 ?? '', 'base64');
}

export async function profileText(filename: string, bytes: Uint8Array): Promise<string> {
  return extension(filename) === '.pdf' ? pdfText(bytes) : new TextDecoder().decode(bytes);
}

/** Score a candidate profile against the published index; a 422 means it cannot be scored. */
async function runEngine(filename: string, bytes: Uint8Array, meta: BoardMeta): Promise<EngineScore> {
  const payload: Record<string, unknown> = {
    action: 'score', filename,
    index: { url: await r2.presignGet(meta.key, { expires: 900 }), sha256: meta.sha256 },
  };
  if (extension(filename) === '.pdf') payload.text = await pdfText(bytes);
  else payload.content_base64 = Buffer.from(bytes).toString('base64');
  return callEngine<EngineScore>(payload);
}

async function storeScores(userId: number, profileSha: string, meta: BoardMeta,
  summary: ProfileSummary, scores: Row[]): Promise<void> {
  const chunks: Row[][] = [];
  for (let i = 0; i < scores.length; i += 500) chunks.push(scores.slice(i, i + 500));
  await db().transaction((sql) => [
    sql`DELETE FROM job_scores WHERE user_id = ${userId}`,
    ...chunks.map((chunk) => sql`
      INSERT INTO job_scores (user_id, job_id, score, rank, verdict, insufficient, requirements,
                              skill_requirements, matched, gaps, recognized)
      SELECT ${userId}, x.id, x.score, x.rank, x.verdict, x.insufficient, x.requirements,
             x.skill_requirements, COALESCE(x.matched, '[]'), COALESCE(x.gaps, '[]'),
             COALESCE(x.recognized, '[]')
      FROM jsonb_to_recordset(${JSON.stringify(chunk)}::jsonb) AS x(
        id text, score int, rank int, verdict text, insufficient boolean, requirements int,
        skill_requirements int, matched jsonb, gaps jsonb, recognized jsonb)
      JOIN jobs ON jobs.id = x.id`),
    sql`INSERT INTO score_runs (user_id, profile_sha256, index_sha256, summary, scored_at)
        VALUES (${userId}, ${profileSha}, ${meta.sha256}, ${JSON.stringify(summary)}::jsonb, now())
        ON CONFLICT (user_id) DO UPDATE SET profile_sha256 = excluded.profile_sha256,
          index_sha256 = excluded.index_sha256, summary = excluded.summary, scored_at = now()`,
  ]);
}

/**
 * The profile summary behind this account's board, rescoring first when the profile or the
 * published index changed since the last run. Null when there is no profile (or no board).
 */
export async function ensureScores(user: User): Promise<ProfileSummary | null> {
  const [meta, row] = await Promise.all([boardMeta(), profileRow(user.id)]);
  if (!meta || !row) return null;
  const runs = await db()`SELECT profile_sha256, index_sha256, summary FROM score_runs
                          WHERE user_id = ${user.id}`;
  const run = runs[0];
  if (run && run.profile_sha256 === row.sha256 && run.index_sha256 === meta.sha256) {
    return { ...(run.summary as ProfileSummary), source: 'account' };
  }
  let summary: ProfileSummary;
  let scores: Row[] = [];
  try {
    const result = await runEngine(row.filename, await profileBytes(user.id, row), meta);
    summary = result.profile;
    scores = result.scores;
  } catch (err) {
    if (!(err instanceof HttpError) || err.status !== 422) throw err;
    summary = { error: err.message, filename: row.filename };
  }
  await storeScores(user.id, row.sha256, meta, summary, scores);
  return { ...summary, source: 'account' };
}

/**
 * Save an uploaded profile once the engine can score it (as server.py refuses what it
 * cannot read). `key` is the R2 object the browser uploaded, or null to store `bytes` here.
 */
export async function saveProfile(user: User, filename: string, bytes: Uint8Array,
  key: string | null): Promise<void> {
  const meta = await requireBoard();
  const name = safeFilename(filename);
  const ext = extension(name);
  if (!PROFILE_TYPES[ext]) throw new HttpError(422, 'Upload a .md, .txt or .pdf file');
  if (bytes.length > meta.max_profile_bytes) throw new HttpError(413, 'Profiles are limited to 2 MB');
  const sha256 = createHash('sha256').update(bytes).digest('hex');
  const objectKey = key ?? `users/${user.id}/profile/${sha256}${ext}`;
  let result: EngineScore;
  try {
    result = await runEngine(name, bytes, meta);
  } catch (err) {
    if (key) await r2.remove(key).catch(() => undefined);
    throw err;
  }
  if (!key) await r2.put(objectKey, bytes, PROFILE_TYPES[ext]);
  const old = await profileRow(user.id);
  await db()`
    INSERT INTO profiles (user_id, filename, content, sha256, updated_at, object_key)
    VALUES (${user.id}, ${name}, decode('', 'hex'), ${sha256}, ${nowSeconds()}, ${objectKey})
    ON CONFLICT (user_id) DO UPDATE SET filename = excluded.filename, content = excluded.content,
      sha256 = excluded.sha256, updated_at = excluded.updated_at, object_key = excluded.object_key`;
  if (old?.object_key && old.object_key !== objectKey) await r2.remove(old.object_key).catch(() => undefined);
  await storeScores(user.id, sha256, meta, result.profile, result.scores);
}

export async function deleteProfile(user: User): Promise<void> {
  const old = await profileRow(user.id);
  await db().transaction((sql) => [
    sql`DELETE FROM profiles WHERE user_id = ${user.id}`,
    sql`DELETE FROM score_runs WHERE user_id = ${user.id}`,
    sql`DELETE FROM job_scores WHERE user_id = ${user.id}`,
  ]);
  if (old?.object_key) await r2.remove(old.object_key).catch(() => undefined);
}

/** GET /api/me/profile: the stored file (as text when editable) and what it scores. */
export async function myProfile(user: User): Promise<Row> {
  const meta = await boardMeta();
  const summary = await ensureScores(user);
  const counts = (await db()`
    SELECT count(*) FILTER (WHERE NOT insufficient)::int AS scored,
           count(*) FILTER (WHERE NOT insufficient AND score >= ${meta?.threshold ?? 65})::int AS proceed,
           count(*) FILTER (WHERE insufficient)::int AS thin
    FROM job_scores WHERE user_id = ${user.id}`)[0];
  const base = { summary, threshold: meta?.threshold ?? 65, ...counts };
  const row = await profileRow(user.id);
  if (!row) return { stored: null, ...base };
  let text: string | null = null;
  let error: string | null = null;
  try {
    text = await profileText(row.filename, await profileBytes(user.id, row));
  } catch (err) {
    error = err instanceof HttpError ? err.message : 'The file could not be read';
  }
  return { stored: { filename: row.filename, text, error, sha256: row.sha256,
    editable: ['.md', '.txt'].includes(extension(row.filename)), updated: isoSeconds(row.updated_at) },
  ...base };
}

/** A profile upload's object key: under this account's folder, unguessable, kept on success. */
export function profileUploadKey(user: User, filename: string): string {
  const ext = extension(filename);
  if (!PROFILE_TYPES[ext]) throw new HttpError(422, 'Upload a .md, .txt or .pdf file');
  return `users/${user.id}/profile/${randomUUID()}${ext}`;
}

// ---------------------------------------------------------------------------
// The board listing (GET /api/jobs), the same filters, sorts and facets as server.py
// ---------------------------------------------------------------------------
export interface JobQuery {
  q: string; region: string; level: string; source: string; verdict: string;
  min_score: number | null; sort: string; offset: number; limit: number;
}

const SORTS: Record<string, string> = {
  match: 's.rank DESC NULLS LAST, s.score DESC NULLS LAST, j.id',
  score: 's.score DESC NULLS LAST, j.id',
  recent: 'j.posted DESC NULLS LAST, j.id',
  company: 'lower(j.company), lower(j.title), j.id',
};
const VERDICT_KEY = `CASE WHEN s.verdict = $2 THEN 'proceed' WHEN s.verdict IS NOT NULL THEN 'low_fit'
  WHEN s.insufficient THEN 'thin' WHEN NOT j.complete THEN 'listing' ELSE 'unscored' END`;
const FROM = 'FROM jobs j LEFT JOIN job_scores s ON s.job_id = j.id AND s.user_id = $1';

export function publicJob(row: Row): Row {
  return { id: row.id, title: row.title, company: row.company, source: row.source,
    location: row.location, region: row.region, url: row.url, posted: row.posted,
    complete: row.complete, level: row.level, domain: row.domain,
    skill_requirements: row.skill_requirements, enough_detail: row.enough_detail };
}

export async function listJobs(user: User, query: JobQuery): Promise<Row> {
  const meta = await requireBoard();
  const profile = await ensureScores(user);
  const params: unknown[] = [user.id, meta.verdict_proceed];
  const where: string[] = [];
  const add = (value: unknown) => { params.push(value); return `$${params.length}`; };
  if (query.q.trim()) {
    const like = `%${query.q.trim().replace(/[\\%_]/g, (c) => `\\${c}`)}%`;
    where.push(`(j.title || ' ' || j.company || ' ' || j.location || ' ' || j.id) ILIKE ${add(like)}`);
  }
  if (query.region) where.push(`j.region = ${add(query.region)}`);
  const levels = query.level.split(',').filter(Boolean);
  if (levels.length) where.push(`j.level = ANY(${add(levels)}::text[])`);
  if (query.source) where.push(`j.source = ${add(query.source)}`);
  if (query.verdict) where.push(`(${VERDICT_KEY}) = ${add(query.verdict)}`);
  if (query.min_score != null) where.push(`s.score >= ${add(query.min_score)}`);
  const filter = where.length ? `WHERE ${where.join(' AND ')}` : '';
  const limit = Math.max(1, Math.min(query.limit, PAGE_SIZE_MAX));
  const sql = db();
  const [rows, count, facets, totals] = await Promise.all([
    sql.query(`SELECT j.id, j.title, j.company, j.source, j.location, j.region, j.url, j.posted,
        j.complete, j.level, j.domain, j.skill_requirements, j.enough_detail, s.score, s.rank,
        s.verdict, COALESCE(s.insufficient, false) AS insufficient, s.matched, s.gaps,
        s.requirements AS requirement_count, ${VERDICT_KEY} AS verdict_key
      ${FROM} ${filter} ORDER BY ${SORTS[query.sort] ?? SORTS.match}
      LIMIT ${add(limit)} OFFSET ${add(query.offset)}`, params),
    // Every parameter must appear in the text, so the verdict key ($2) is always selected.
    sql.query(`SELECT count(*)::int AS n FROM (SELECT ${VERDICT_KEY} AS verdict_key ${FROM} ${filter}) t`,
      params.slice(0, params.length - 2)),
    sql.query(`SELECT 'region' AS facet, j.region AS value, count(*)::int AS n ${FROM} GROUP BY 2
      UNION ALL SELECT 'level', j.level, count(*)::int ${FROM} GROUP BY 2
      UNION ALL SELECT 'source', j.source, count(*)::int ${FROM} GROUP BY 2
      UNION ALL SELECT 'verdict', ${VERDICT_KEY}, count(*)::int ${FROM} GROUP BY 2`,
    [user.id, meta.verdict_proceed]),
    sql`SELECT count(*)::int AS total, count(*) FILTER (WHERE enough_detail)::int AS scorable FROM jobs`,
  ]);
  const facetMap: Record<string, Record<string, number>> = { region: {}, level: {}, source: {}, verdict: {} };
  for (const f of facets) facetMap[f.facet][f.value ?? ''] = f.n;
  for (const key of Object.keys(facetMap)) {
    facetMap[key] = Object.fromEntries(Object.entries(facetMap[key]).sort(([a], [b]) => a.localeCompare(b)));
  }
  return {
    total: totals[0].total, matching: count[0].n, offset: query.offset, scorable: totals[0].scorable,
    limit, facets: facetMap, profile, index_generated: meta.generated,
    jobs: rows.map((row) => {
      const scored = row.score != null || row.insufficient;
      return { ...publicJob(row), score: row.score, rank: row.rank, verdict: row.verdict,
        insufficient: row.insufficient, verdict_key: row.verdict_key,
        matched: scored ? (row.matched ?? []).slice(0, 8) : [],
        gaps: scored ? (row.gaps ?? []).slice(0, 6) : [],
        requirement_count: row.requirement_count };
    }),
  };
}

export async function myScore(user: User, jobId: string): Promise<Row | null> {
  const rows = await db()`SELECT * FROM job_scores WHERE user_id = ${user.id} AND job_id = ${jobId}`;
  if (!rows.length) return null;
  const s = rows[0];
  return { id: jobId, score: s.score, rank: s.rank, verdict: s.verdict, insufficient: s.insufficient,
    matched: s.matched, gaps: s.gaps, requirements: s.requirements,
    skill_requirements: s.skill_requirements, recognized: s.recognized };
}

// ---------------------------------------------------------------------------
// Pro builds: queued here, run by `server.py --process-builds` where LaTeX is installed
// ---------------------------------------------------------------------------
export async function buildNeeds(user: User): Promise<{ pro: boolean; missing: string[]; ready: boolean }> {
  const missing: string[] = [];
  const summary = await ensureScores(user);
  if (!summary || summary.error) missing.push('profile');
  else if (!summary.is_master) missing.push('master');
  else if (!summary.has_kit) missing.push('kit');
  const header = await resumeHeader(user.id);
  if (!header?.name) missing.push('header');
  return { pro: user.is_pro, missing, ready: user.is_pro && !missing.length };
}

export async function resumeHeader(userId: number): Promise<Row | null> {
  const rows = await db()`SELECT header FROM resume_headers WHERE user_id = ${userId}`;
  return rows.length ? JSON.parse(rows[0].header) : null;
}

export function buildView(row: Row): Row {
  const summary = JSON.parse(row.summary || '{}');
  return { job_id: row.job_id, title: row.title, company: row.company, status: row.status, summary,
    has_pdf: Boolean(row.has_pdf), has_report: Boolean(row.has_report), created: isoSeconds(row.created_at) };
}

export async function myBuild(userId: number, jobId: string): Promise<Row | null> {
  const rows = await db()`
    SELECT job_id, title, company, status, summary, created_at,
           (pdf IS NOT NULL OR pdf_key IS NOT NULL) AS has_pdf, (report IS NOT NULL) AS has_report
    FROM builds WHERE user_id = ${userId} AND job_id = ${jobId}`;
  return rows.length ? buildView(rows[0]) : null;
}
