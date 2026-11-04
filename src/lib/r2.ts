// Cloudflare R2 through its S3 API. Browsers upload straight to R2 with presigned PUT URLs
// and read with short-lived presigned GET URLs, so file bytes never pass through a function.
import { DeleteObjectCommand, GetObjectCommand, HeadObjectCommand, ListObjectsV2Command,
  PutObjectCommand, S3Client } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { db } from './db';
import { HttpError } from './http';

const UPLOAD_SECONDS = 600;
const READ_SECONDS = 300;
// R2's free tier stores 10 GB. New uploads stop at this budget, which leaves room for
// profiles, build PDFs and the job index, none of which ask before writing.
const STORAGE_BUDGET_BYTES = Number(process.env.R2_BUDGET_BYTES) || 9_000_000_000;
const SNAPSHOT_MINUTES = 15;

let cached: { client: S3Client; bucket: string } | null = null;

function r2(): { client: S3Client; bucket: string } {
  if (cached) return cached;
  const { R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET } = process.env;
  if (!R2_ACCOUNT_ID || !R2_ACCESS_KEY_ID || !R2_SECRET_ACCESS_KEY || !R2_BUCKET) {
    throw new HttpError(503, 'File storage is not configured (R2_*).');
  }
  cached = {
    bucket: R2_BUCKET,
    client: new S3Client({
      region: 'auto',
      // R2_ENDPOINT: a jurisdiction endpoint such as https://<account>.eu.r2.cloudflarestorage.com
      endpoint: process.env.R2_ENDPOINT || `https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com`,
      forcePathStyle: true,
      credentials: { accessKeyId: R2_ACCESS_KEY_ID, secretAccessKey: R2_SECRET_ACCESS_KEY },
      // The SDK's default CRC32 checksums would be baked into presigned URLs that browsers
      // cannot satisfy; R2 needs none.
      requestChecksumCalculation: 'WHEN_REQUIRED',
      responseChecksumValidation: 'WHEN_REQUIRED',
    }),
  };
  return cached;
}

/** A URL the browser PUTs the file to; type and length are signed, so neither can change. */
export async function presignPut(key: string, contentType: string, size: number): Promise<string> {
  const { client, bucket } = r2();
  return getSignedUrl(client, new PutObjectCommand({ Bucket: bucket, Key: key, ContentType: contentType,
    ContentLength: size }), { expiresIn: UPLOAD_SECONDS,
    signableHeaders: new Set(['content-type', 'content-length']) });
}

export async function presignGet(key: string, options: { filename?: string; contentType?: string;
  inline?: boolean; expires?: number } = {}): Promise<string> {
  const { client, bucket } = r2();
  const disposition = options.filename
    ? `${options.inline === false ? 'attachment' : 'inline'}; filename="${options.filename.replace(/[^\w.\- ]/g, '_')}"`
    : undefined;
  return getSignedUrl(client, new GetObjectCommand({ Bucket: bucket, Key: key,
    ResponseContentDisposition: disposition, ResponseContentType: options.contentType }),
  { expiresIn: options.expires ?? READ_SECONDS });
}

export async function head(key: string): Promise<{ size: number; contentType: string } | null> {
  const { client, bucket } = r2();
  try {
    const out = await client.send(new HeadObjectCommand({ Bucket: bucket, Key: key }));
    return { size: out.ContentLength ?? 0, contentType: out.ContentType ?? '' };
  } catch (err) {
    if ((err as { $metadata?: { httpStatusCode?: number } }).$metadata?.httpStatusCode === 404) return null;
    throw err;
  }
}

export async function getBytes(key: string): Promise<Uint8Array> {
  const { client, bucket } = r2();
  const out = await client.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
  if (!out.Body) throw new HttpError(404, 'The file is missing from storage');
  return out.Body.transformToByteArray();
}

export async function put(key: string, body: Uint8Array | string, contentType: string): Promise<void> {
  const { client, bucket } = r2();
  await client.send(new PutObjectCommand({ Bucket: bucket, Key: key, Body: body, ContentType: contentType }));
}

export async function remove(key: string): Promise<void> {
  const { client, bucket } = r2();
  await client.send(new DeleteObjectCommand({ Bucket: bucket, Key: key }));
}

/** Bytes stored in the whole bucket (one list call per 1,000 objects). */
async function bucketBytes(): Promise<number> {
  const { client, bucket } = r2();
  let token: string | undefined;
  let total = 0;
  do {
    const page = await client.send(new ListObjectsV2Command({ Bucket: bucket, ContinuationToken: token }));
    for (const object of page.Contents ?? []) total += object.Size ?? 0;
    token = page.IsTruncated ? page.NextContinuationToken : undefined;
  } while (token);
  return total;
}

/**
 * Refuse an upload of `size` bytes that would take the bucket past STORAGE_BUDGET_BYTES.
 * The bucket total is listed at most every SNAPSHOT_MINUTES (board key 'storage'); review
 * media signed since then, and every pending upload, are added from Neon on top of it.
 */
export async function ensureRoom(size: number): Promise<void> {
  const fresh = await db()`SELECT 1 FROM board WHERE key = 'storage'
                           AND updated_at > now() - make_interval(mins => ${SNAPSHOT_MINUTES})`;
  if (!fresh.length) {
    const bytes = await bucketBytes();
    await db()`INSERT INTO board (key, value) VALUES ('storage', ${JSON.stringify({ bytes })}::jsonb)
               ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()`;
  }
  const rows = await db()`
    SELECT (b.value->>'bytes')::bigint + coalesce((SELECT sum(m.size_bytes) FROM review_media m
             WHERE m.status = 'pending' OR m.created_at > b.updated_at), 0) AS used
    FROM board b WHERE b.key = 'storage'`;
  if (Number(rows[0].used) + size > STORAGE_BUDGET_BYTES) {
    throw new HttpError(507, 'File storage is full for now, so uploads are paused. Try again later.');
  }
}

/** Delete everything under a folder prefix (an account's users/<id>/). */
export async function removePrefix(prefix: string): Promise<number> {
  if (!prefix.endsWith('/')) throw new Error('Only a folder prefix can be deleted');
  const { client, bucket } = r2();
  let token: string | undefined;
  let count = 0;
  do {
    const page = await client.send(new ListObjectsV2Command({ Bucket: bucket, Prefix: prefix,
      ContinuationToken: token }));
    const keys = (page.Contents ?? []).map((o) => o.Key).filter((k): k is string => Boolean(k));
    await Promise.all(keys.map((k) => remove(k)));
    count += keys.length;
    token = page.IsTruncated ? page.NextContinuationToken : undefined;
  } while (token);
  return count;
}
