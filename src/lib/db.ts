import { neon, type NeonQueryFunction } from '@neondatabase/serverless';
import { HttpError } from './http';

// Neon over HTTP: one round trip per query, nothing to pool or close in a serverless function.
let client: NeonQueryFunction<false, false> | null = null;

export function db(): NeonQueryFunction<false, false> {
  if (!client) {
    const url = process.env.DATABASE_URL;
    if (!url) throw new HttpError(503, 'The database is not configured (DATABASE_URL).');
    client = neon(url);
  }
  return client;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Row = Record<string, any>;
