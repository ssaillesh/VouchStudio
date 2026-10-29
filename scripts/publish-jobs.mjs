// Publish the local job board to the hosted app.
//
//   npm run publish:jobs               # refresh outputs/job_index.json, then publish
//   npm run publish:jobs -- --dry-run  # show what would change, write nothing
//
// 1. `python3 jobboard.py index` refreshes the local index (requirements are extracted here,
//    with the same engine as the CLI, so Vercel never parses a posting).
// 2. The requirements-only index goes to R2 as board/index-<sha12>.json (content-addressed,
//    so a published version never changes underneath api/engine.py).
// 3. Each posting's metadata and description is upserted into Neon `jobs`; postings no
//    longer in target_jobs/ are deleted (their scores go with them).
// 4. board.index points at the new object last, so readers never see a half-published board.
//    Every account's scores are then stale and are recomputed on its next board load.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PutObjectCommand, S3Client } from '@aws-sdk/client-s3';
import { neon } from '@neondatabase/serverless';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const dryRun = process.argv.includes('--dry-run');
const CHUNK = 200;

function need(name) {
  const value = process.env[name];
  if (!value) {
    console.error(`${name} is not set (see .env.example).`);
    process.exit(1);
  }
  return value;
}

const python = process.env.PYTHON || 'python3';
execFileSync(python, ['jobboard.py', 'index'], { cwd: root, stdio: 'inherit' });
const constants = JSON.parse(execFileSync(python, ['-c',
  'import json, sys; sys.path.insert(0, "api"); import engine; print(json.dumps(engine.constants()))'],
{ cwd: root, encoding: 'utf8' }));

const index = JSON.parse(readFileSync(join(root, 'outputs', 'job_index.json'), 'utf8'));
const slim = JSON.stringify({ version: index.version, generated: index.generated,
  jobs: index.jobs.map((job) => ({ id: job.id, requirements: job.requirements })) });
const sha256 = createHash('sha256').update(slim).digest('hex');
const key = `board/index-${sha256.slice(0, 12)}.json`;
const skillKinds = new Set(['skill', 'skill_category', 'skill_group', 'language']);
const rows = index.jobs.map((job) => {
  const skills = (job.requirements || []).filter((r) => skillKinds.has(r.kind)).length;
  return {
    id: job.id, title: job.title, company: job.company || '', source: job.source || 'Manual',
    location: job.location || 'Not specified', region: job.region || '', url: job.url || null,
    posted: job.posted || null, complete: Boolean(job.complete), level: job.level,
    domain: job.domain || null, skill_requirements: skills,
    enough_detail: skills >= constants.min_skill_requirements,
    description: readFileSync(join(root, 'target_jobs', job.file), 'utf8'), sha256: job.sha256,
  };
});

console.log(`${rows.length} jobs, index ${key} (${(slim.length / 1e6).toFixed(1)} MB)`);
if (dryRun) {
  console.log('Dry run: nothing written.');
  process.exit(0);
}

const r2 = new S3Client({
  region: 'auto',
  endpoint: process.env.R2_ENDPOINT || `https://${need('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com`,
  forcePathStyle: true, requestChecksumCalculation: 'WHEN_REQUIRED',
  responseChecksumValidation: 'WHEN_REQUIRED',
  credentials: { accessKeyId: need('R2_ACCESS_KEY_ID'), secretAccessKey: need('R2_SECRET_ACCESS_KEY') },
});
await r2.send(new PutObjectCommand({ Bucket: need('R2_BUCKET'), Key: key, Body: slim,
  ContentType: 'application/json' }));

const sql = neon(need('DATABASE_URL'));
for (let i = 0; i < rows.length; i += CHUNK) {
  await sql`
    INSERT INTO jobs (id, title, company, source, location, region, url, posted, complete, level,
                      domain, skill_requirements, enough_detail, description, sha256, published_at)
    SELECT x.id, x.title, x.company, x.source, x.location, x.region, x.url, x.posted, x.complete,
           x.level, x.domain, x.skill_requirements, x.enough_detail, x.description, x.sha256, now()
    FROM jsonb_to_recordset(${JSON.stringify(rows.slice(i, i + CHUNK))}::jsonb) AS x(
      id text, title text, company text, source text, location text, region text, url text,
      posted text, complete boolean, level text, domain text, skill_requirements int,
      enough_detail boolean, description text, sha256 text)
    ON CONFLICT (id) DO UPDATE SET title = excluded.title, company = excluded.company,
      source = excluded.source, location = excluded.location, region = excluded.region,
      url = excluded.url, posted = excluded.posted, complete = excluded.complete,
      level = excluded.level, domain = excluded.domain,
      skill_requirements = excluded.skill_requirements, enough_detail = excluded.enough_detail,
      description = excluded.description, sha256 = excluded.sha256,
      published_at = excluded.published_at
    WHERE jobs.sha256 IS DISTINCT FROM excluded.sha256 OR jobs.level IS DISTINCT FROM excluded.level`;
  process.stdout.write(`\rUpserted ${Math.min(i + CHUNK, rows.length)} / ${rows.length}`);
}
process.stdout.write('\n');
const removed = await sql`DELETE FROM jobs WHERE NOT (id = ANY(${rows.map((r) => r.id)}::text[]))
                          RETURNING id`;
await sql`INSERT INTO board (key, value) VALUES ('index', ${JSON.stringify({
  ...constants, key, sha256, generated: index.generated, jobs: rows.length })}::jsonb)
  ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()`;
console.log(`Published ${rows.length} jobs (${removed.length} removed). Index: ${key}`);
