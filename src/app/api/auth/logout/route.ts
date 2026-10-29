import type { NextRequest } from 'next/server';
import { clearSessionCookie, endSession, SESSION_COOKIE } from '@/lib/auth';
import { json, route } from '@/lib/http';

export const POST = route(async (request: NextRequest) => {
  await endSession(request.cookies.get(SESSION_COOKIE)?.value);
  const response = json({ ok: true });
  clearSessionCookie(response);
  return response;
});
