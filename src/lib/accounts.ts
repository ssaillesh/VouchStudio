// Pro requests, plans and the admin console: the same rules as accounts.py and server.py.
import { NOTE_MAX, userById, type User } from './auth';
import { db, type Row } from './db';
import { HttpError, isoSeconds, nowSeconds } from './http';

export const PLANS = ['basic', 'pro'];

export async function latestRequest(userId: number): Promise<Row | null> {
  const rows = await db()`SELECT * FROM pro_requests WHERE user_id = ${userId} ORDER BY id DESC LIMIT 1`;
  return rows[0] ?? null;
}

export async function requestPro(user: User, noteRaw: unknown): Promise<Row> {
  const note = String(noteRaw ?? '').trim();
  if (note.length > NOTE_MAX) throw new HttpError(422, `Keep the note under ${NOTE_MAX} characters.`);
  if (user.is_pro) throw new HttpError(409, 'Your account already has Pro.');
  const pending = await db()`SELECT * FROM pro_requests WHERE user_id = ${user.id} AND status = 'pending'
                             ORDER BY id DESC LIMIT 1`;
  if (pending.length) return pending[0];
  const rows = await db()`INSERT INTO pro_requests (user_id, note, status, created_at)
                          VALUES (${user.id}, ${note}, 'pending', ${nowSeconds()}) RETURNING *`;
  return rows[0];
}

export async function setPlan(userId: number, plan: unknown): Promise<void> {
  if (typeof plan !== 'string' || !PLANS.includes(plan)) {
    throw new HttpError(422, `A plan is one of: ${PLANS.join(', ')}.`);
  }
  if (!(await userById(userId))) throw new HttpError(404, 'No such account.');
  await db()`UPDATE users SET plan = ${plan} WHERE id = ${userId}`;
  if (plan === 'pro') {  // anything still waiting is now answered
    await db()`UPDATE pro_requests SET status = 'approved', decided_at = ${nowSeconds()}
               WHERE user_id = ${userId} AND status = 'pending'`;
  }
}

export async function decideRequest(requestId: number, approve: boolean, admin: User): Promise<void> {
  const rows = await db()`SELECT * FROM pro_requests WHERE id = ${requestId}`;
  if (!rows.length) throw new HttpError(404, 'No such request.');
  if (rows[0].status !== 'pending') throw new HttpError(409, `This request was already ${rows[0].status}.`);
  await db()`UPDATE pro_requests SET status = ${approve ? 'approved' : 'declined'},
             decided_at = ${nowSeconds()}, decided_by = ${admin.id} WHERE id = ${requestId}`;
  if (approve) await db()`UPDATE users SET plan = 'pro' WHERE id = ${rows[0].user_id}`;
}

export async function adminOverview(): Promise<Row> {
  const when = (ts: number | null) => (ts ? isoSeconds(ts) : null);
  const [users, requests] = await Promise.all([
    db()`SELECT users.*,
           EXISTS (SELECT 1 FROM profiles WHERE profiles.user_id = users.id) AS has_profile,
           (SELECT count(*)::int FROM builds WHERE builds.user_id = users.id) AS builds,
           EXISTS (SELECT 1 FROM pro_requests WHERE pro_requests.user_id = users.id
                   AND pro_requests.status = 'pending') AS pending_request
         FROM users ORDER BY created_at DESC`,
    db()`SELECT pro_requests.*, users.email, users.name, users.plan FROM pro_requests
         JOIN users ON users.id = pro_requests.user_id ORDER BY pro_requests.created_at DESC`,
  ]);
  return {
    users: users.map((u) => ({ id: u.id, email: u.email, name: u.name, is_admin: Boolean(u.is_admin),
      plan: u.plan, is_pro: Boolean(u.is_admin) || u.plan === 'pro', created_at: when(u.created_at),
      has_profile: u.has_profile, builds: u.builds, pending_request: u.pending_request })),
    requests: requests.map((r) => ({ ...r, created_at: when(r.created_at), decided_at: when(r.decided_at) })),
    pending: requests.filter((r) => r.status === 'pending').length,
    database: 'postgres',
  };
}
