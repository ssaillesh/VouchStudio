// Accounts on the hosted app. The rules, the scrypt hash format and the session scheme are
// the ones accounts.py uses, so an account works the same here and in the local Studio
// pointed at the same Neon database.
import { createHash, randomBytes, scrypt, timingSafeEqual, type ScryptOptions } from 'node:crypto';
import type { NextRequest, NextResponse } from 'next/server';
import { db, type Row } from './db';
import { HttpError, nowSeconds } from './http';

export const SESSION_COOKIE = 'vouch_session';
export const SESSION_SECONDS = 30 * 24 * 3600;
export const PASSWORD_MIN = 8;
const PASSWORD_MAX = 256;
const NAME_MAX = 80;
const EMAIL_MAX = 254;
export const NOTE_MAX = 600;
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const SCRYPT = { N: 2 ** 14, r: 8, p: 1 };
const THROTTLES = { login: { limit: 10, window: 15 * 60 }, signup: { limit: 20, window: 3600 } };

export interface User {
  id: number;
  email: string;
  name: string;
  is_admin: boolean;
  plan: string;
  is_pro: boolean;
}

function derive(password: string, salt: Buffer, length: number, options: ScryptOptions): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    scrypt(Buffer.from(password, 'utf8'), salt, length, { ...options, maxmem: 64 * 1024 * 1024 },
      (err, key) => (err ? reject(err) : resolve(key)));
  });
}

export async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(16);
  const digest = await derive(password, salt, 32, SCRYPT);
  return `scrypt$${SCRYPT.N}$${SCRYPT.r}$${SCRYPT.p}$${salt.toString('hex')}$${digest.toString('hex')}`;
}

export async function checkPassword(password: string, stored: string): Promise<boolean> {
  const [scheme, n, r, p, salt, digest] = stored.split('$');
  const N = Number(n);
  if (scheme !== 'scrypt' || !digest || !Number.isInteger(N) || N > 2 ** 20) return false;
  const expected = Buffer.from(digest, 'hex');
  const actual = await derive(password, Buffer.from(salt, 'hex'), expected.length,
    { N, r: Number(r), p: Number(p) });
  return timingSafeEqual(actual, expected);
}

const DUMMY_HASH = hashPassword(randomBytes(8).toString('hex'));
const tokenHash = (token: string) => createHash('sha256').update(token, 'utf8').digest('hex');
export const normalizeEmail = (email: unknown) => String(email ?? '').trim().toLowerCase();

export function adminEmails(): Set<string> {
  return new Set((process.env.VOUCH_ADMIN_EMAILS || '').split(',').map(normalizeEmail).filter(Boolean));
}

export function publicUser(row: Row): User {
  const admin = Boolean(row.is_admin);
  return { id: row.id, email: row.email, name: row.name, is_admin: admin, plan: row.plan,
    is_pro: admin || row.plan === 'pro' };
}

export async function userById(id: number): Promise<User | null> {
  const rows = await db()`SELECT * FROM users WHERE id = ${id}`;
  return rows.length ? publicUser(rows[0]) : null;
}

export async function createUser(emailRaw: unknown, password: unknown, nameRaw: unknown): Promise<User> {
  const email = normalizeEmail(emailRaw);
  const name = String(nameRaw ?? '').trim();
  const pw = typeof password === 'string' ? password : '';
  if (!EMAIL.test(email) || email.length > EMAIL_MAX) throw new HttpError(422, 'Enter a valid email address.');
  if (pw.length < PASSWORD_MIN || pw.length > PASSWORD_MAX) {
    throw new HttpError(422, `Use a password of at least ${PASSWORD_MIN} characters.`);
  }
  if (pw.trim().toLowerCase() === email) throw new HttpError(422, "Your password can't be your email address.");
  if (name.length > NAME_MAX) throw new HttpError(422, `Keep your name under ${NAME_MAX} characters.`);
  const rows = await db()`
    INSERT INTO users (email, name, password_hash, is_admin, created_at)
    VALUES (${email}, ${name}, ${await hashPassword(pw)}, ${adminEmails().has(email) ? 1 : 0}, ${nowSeconds()})
    ON CONFLICT (email) DO NOTHING RETURNING *`;
  if (!rows.length) throw new HttpError(409, 'An account with this email already exists. Log in instead.');
  return publicUser(rows[0]);
}

export async function authenticate(emailRaw: unknown, password: unknown): Promise<User | null> {
  const email = normalizeEmail(emailRaw);
  const pw = typeof password === 'string' ? password : '';
  const rows = await db()`SELECT * FROM users WHERE email = ${email}`;
  if (!rows.length) {
    await checkPassword(pw, await DUMMY_HASH);  // the same work as a real check
    return null;
  }
  if (!(await checkPassword(pw, rows[0].password_hash))) return null;
  if (adminEmails().has(email) && !rows[0].is_admin) {
    await db()`UPDATE users SET is_admin = 1 WHERE id = ${rows[0].id}`;
    return userById(rows[0].id);
  }
  return publicUser(rows[0]);
}

export async function createSession(userId: number): Promise<string> {
  const token = randomBytes(32).toString('base64url');
  const now = nowSeconds();
  await db().transaction((sql) => [
    sql`DELETE FROM sessions WHERE expires_at < ${now}`,
    sql`INSERT INTO sessions (token_hash, user_id, created_at, expires_at)
        VALUES (${tokenHash(token)}, ${userId}, ${now}, ${now + SESSION_SECONDS})`,
  ]);
  return token;
}

export async function endSession(token: string | undefined): Promise<void> {
  if (token) await db()`DELETE FROM sessions WHERE token_hash = ${tokenHash(token)}`;
}

export async function currentUser(request: NextRequest): Promise<User | null> {
  const token = request.cookies.get(SESSION_COOKIE)?.value;
  if (!token || token.length > 200) return null;
  const rows = await db()`
    SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id
    WHERE sessions.token_hash = ${tokenHash(token)} AND sessions.expires_at > ${nowSeconds()}`;
  return rows.length ? publicUser(rows[0]) : null;
}

export async function requireUser(request: NextRequest): Promise<User> {
  const user = await currentUser(request);
  if (!user) throw new HttpError(401, 'Log in to continue');
  return user;
}

export async function requireAdmin(request: NextRequest): Promise<User> {
  const user = await requireUser(request);
  if (!user.is_admin) throw new HttpError(403, 'Only an admin can do this');
  return user;
}

export function setSessionCookie(response: NextResponse, token: string): void {
  response.cookies.set(SESSION_COOKIE, token, { httpOnly: true, sameSite: 'lax', path: '/',
    maxAge: SESSION_SECONDS, secure: process.env.NODE_ENV === 'production' });
}

export function clearSessionCookie(response: NextResponse): void {
  response.cookies.delete({ name: SESSION_COOKIE, path: '/' });
}

/** Attempts in the window, kept in Postgres because serverless instances share no memory. */
export async function throttleCheck(kind: keyof typeof THROTTLES, ...keys: string[]): Promise<void> {
  const { limit, window } = THROTTLES[kind];
  const since = nowSeconds() - window;
  const rows = await db()`
    SELECT key, count(*)::int AS n FROM auth_attempts
    WHERE key = ANY(${keys.map((k) => `${kind}:${k}`)}::text[]) AND at > ${since} GROUP BY key`;
  if (rows.some((row) => row.n >= limit)) {
    throw new HttpError(429, 'Too many attempts. Wait a few minutes and try again.');
  }
}

export async function throttleFail(kind: keyof typeof THROTTLES, ...keys: string[]): Promise<void> {
  const now = nowSeconds();
  await db()`INSERT INTO auth_attempts (key, at)
             SELECT k, ${now} FROM unnest(${keys.map((k) => `${kind}:${k}`)}::text[]) AS k`;
}

export async function throttleReset(kind: keyof typeof THROTTLES, key: string): Promise<void> {
  await db()`DELETE FROM auth_attempts WHERE key = ${`${kind}:${key}`}`;
}
