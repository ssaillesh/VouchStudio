// Reviews: text and links in Neon, attachments (images, PDFs, videos) in R2.
//
// An attachment is uploaded before its review exists: POST /api/uploads records a 'pending'
// review_media row and returns a presigned PUT URL; the browser uploads to R2; POST
// /api/reviews then checks each object (it exists, its size and type are what was signed)
// and attaches it. Pending media nobody attached is removed by the daily cleanup cron.
import { randomUUID } from 'node:crypto';
import type { User } from './auth';
import { db, type Row } from './db';
import { HttpError, text } from './http';
import * as r2 from './r2';

export const MEDIA_TYPES: Record<string, { kind: 'image' | 'pdf' | 'video'; ext: string; max: number }> = {
  'image/jpeg': { kind: 'image', ext: '.jpg', max: 10_000_000 },
  'image/png': { kind: 'image', ext: '.png', max: 10_000_000 },
  'image/webp': { kind: 'image', ext: '.webp', max: 10_000_000 },
  'image/gif': { kind: 'image', ext: '.gif', max: 10_000_000 },
  'application/pdf': { kind: 'pdf', ext: '.pdf', max: 10_000_000 },
  'video/mp4': { kind: 'video', ext: '.mp4', max: 200_000_000 },
  'video/webm': { kind: 'video', ext: '.webm', max: 200_000_000 },
  'video/quicktime': { kind: 'video', ext: '.mov', max: 200_000_000 },
};
export const MAX_MEDIA_PER_REVIEW = 6;
const MAX_PENDING_MEDIA = 20;
const MAX_LINKS = 5;
const PAGE = 20;

export async function createMediaUpload(user: User, filename: string, contentType: string,
  size: number): Promise<Row> {
  const type = MEDIA_TYPES[contentType];
  if (!type) throw new HttpError(422, 'Attach a JPEG, PNG, WebP or GIF image, a PDF, or an MP4, WebM or MOV video.');
  if (!Number.isInteger(size) || size <= 0) throw new HttpError(422, 'The file is empty.');
  if (size > type.max) throw new HttpError(413, `That ${type.kind} is larger than ${type.max / 1_000_000} MB.`);
  const pending = await db()`SELECT count(*)::int AS n FROM review_media
                             WHERE user_id = ${user.id} AND status = 'pending'`;
  if (pending[0].n >= MAX_PENDING_MEDIA) {
    throw new HttpError(429, 'Too many unfinished uploads. Post or discard your review first.');
  }
  const id = randomUUID();
  const key = `users/${user.id}/reviews/media/${id}${type.ext}`;
  const name = (filename.split(/[\\/]/).pop() || `attachment${type.ext}`).slice(0, 160);
  await db()`INSERT INTO review_media (id, user_id, object_key, kind, content_type, size_bytes, filename)
             VALUES (${id}, ${user.id}, ${key}, ${type.kind}, ${contentType}, ${size}, ${name})`;
  return { id, kind: type.kind, url: await r2.presignPut(key, contentType, size),
    headers: { 'Content-Type': contentType } };
}

function cleanLinks(raw: unknown): string[] {
  if (raw == null) return [];
  if (!Array.isArray(raw)) throw new HttpError(422, 'links must be a list');
  const links = raw.map((l) => text(l, 'a link', 500)).filter(Boolean);
  if (links.length > MAX_LINKS) throw new HttpError(422, `Add at most ${MAX_LINKS} links.`);
  return links.map((link) => {
    let url: URL;
    try {
      url = new URL(link.includes('://') ? link : `https://${link}`);
    } catch {
      throw new HttpError(422, `'${link}' is not a web address.`);
    }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') {
      throw new HttpError(422, `'${link}' is not an http(s) link.`);
    }
    return url.href;
  });
}

export async function createReview(user: User, body: Row): Promise<Row> {
  const rating = Number(body.rating);
  if (!Number.isInteger(rating) || rating < 1 || rating > 5) throw new HttpError(422, 'Pick a rating from 1 to 5.');
  const title = text(body.title, 'a title', 140, true);
  const reviewBody = text(body.body, 'your review', 5000, true);
  const links = cleanLinks(body.links);
  let company = text(body.company, 'the company', 120);
  let jobId: string | null = null;
  if (body.job_id) {
    const jobs = await db()`SELECT id, company FROM jobs WHERE id = ${String(body.job_id)}`;
    if (!jobs.length) throw new HttpError(404, 'That job is no longer on the board.');
    jobId = jobs[0].id;
    company = jobs[0].company || company;
  }
  if (!company) throw new HttpError(422, 'Enter the company.');
  const mediaIds: string[] = Array.isArray(body.media_ids) ? body.media_ids.map(String) : [];
  if (mediaIds.length > MAX_MEDIA_PER_REVIEW) {
    throw new HttpError(422, `Attach at most ${MAX_MEDIA_PER_REVIEW} files.`);
  }
  const media = mediaIds.length ? await db()`
    SELECT * FROM review_media WHERE id = ANY(${mediaIds}::uuid[]) AND user_id = ${user.id}
      AND status = 'pending' AND review_id IS NULL` : [];
  if (media.length !== new Set(mediaIds).size) throw new HttpError(422, 'An attachment is missing; upload it again.');
  for (const m of media) {  // the object must be exactly what was signed for
    const found = await r2.head(m.object_key);
    if (!found || found.size !== Number(m.size_bytes) || found.contentType !== m.content_type) {
      throw new HttpError(422, `${m.filename} did not finish uploading; attach it again.`);
    }
  }
  const sql = db();
  const [inserted] = await sql`
    INSERT INTO reviews (user_id, company, job_id, rating, title, body, links)
    VALUES (${user.id}, ${company}, ${jobId}, ${rating}, ${title}, ${reviewBody}, ${JSON.stringify(links)}::jsonb)
    RETURNING id`;
  if (mediaIds.length) {
    await sql`UPDATE review_media SET status = 'ready', review_id = ${inserted.id}
              WHERE id = ANY(${mediaIds}::uuid[]) AND user_id = ${user.id}`;
  }
  return (await listReviews(user, { id: inserted.id }))[0];
}

export async function listReviews(user: User, filter: { id?: number; company?: string; jobId?: string;
  mine?: boolean; offset?: number }): Promise<Row[]> {
  const params: unknown[] = [user.id, user.is_admin];
  const where = ['(r.status = \'published\' OR r.user_id = $1 OR $2::boolean)'];
  const add = (v: unknown) => { params.push(v); return `$${params.length}`; };
  if (filter.id != null) where.push(`r.id = ${add(filter.id)}`);
  if (filter.company) where.push(`lower(r.company) = lower(${add(filter.company)})`);
  if (filter.jobId) where.push(`r.job_id = ${add(filter.jobId)}`);
  if (filter.mine) where.push('r.user_id = $1');
  const rows = await db().query(`
    SELECT r.*, u.name AS author, j.title AS job_title,
      COALESCE((SELECT json_agg(json_build_object('id', m.id, 'kind', m.kind, 'filename', m.filename,
        'content_type', m.content_type, 'size', m.size_bytes) ORDER BY m.created_at)
        FROM review_media m WHERE m.review_id = r.id AND m.status = 'ready'), '[]') AS media
    FROM reviews r JOIN users u ON u.id = r.user_id LEFT JOIN jobs j ON j.id = r.job_id
    WHERE ${where.join(' AND ')} ORDER BY r.created_at DESC, r.id DESC
    LIMIT ${PAGE} OFFSET ${add(Math.max(0, filter.offset ?? 0))}`, params);
  return rows.map((r) => ({
    id: r.id, company: r.company, job_id: r.job_id, job_title: r.job_title, rating: r.rating,
    title: r.title, body: r.body, links: r.links, status: r.status,
    created_at: new Date(r.created_at).toISOString(), author: r.author || 'A VouchStudio member',
    mine: r.user_id === user.id,
    media: (r.media as Row[]).map((m) => ({ ...m, url: `/api/reviews/media/${m.id}` })),
  }));
}

export async function deleteReview(user: User, id: number): Promise<void> {
  const rows = await db()`SELECT user_id FROM reviews WHERE id = ${id}`;
  if (!rows.length) throw new HttpError(404, 'No such review.');
  if (rows[0].user_id !== user.id && !user.is_admin) throw new HttpError(403, 'Only the author can delete this review.');
  const media = await db()`SELECT object_key FROM review_media WHERE review_id = ${id}`;
  await Promise.all(media.map((m) => r2.remove(m.object_key)));
  await db()`DELETE FROM reviews WHERE id = ${id}`;  // its media rows cascade
}

export async function mediaUrl(user: User, id: string): Promise<string> {
  if (!/^[0-9a-f-]{36}$/.test(id)) throw new HttpError(404, 'No such file.');
  const rows = await db()`
    SELECT m.object_key, m.filename, m.content_type FROM review_media m JOIN reviews r ON r.id = m.review_id
    WHERE m.id = ${id}::uuid AND m.status = 'ready'
      AND (r.status = 'published' OR r.user_id = ${user.id} OR ${user.is_admin})`;
  if (!rows.length) throw new HttpError(404, 'No such file.');
  const m = rows[0];
  return r2.presignGet(m.object_key, { filename: m.filename, contentType: m.content_type, inline: true });
}
