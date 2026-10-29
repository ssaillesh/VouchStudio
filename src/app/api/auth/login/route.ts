import type { NextRequest } from 'next/server';
import { authenticate, createSession, normalizeEmail, setSessionCookie, throttleCheck, throttleFail,
  throttleReset } from '@/lib/auth';
import { clientIp, HttpError, json, readJson, route } from '@/lib/http';

export const POST = route(async (request: NextRequest) => {
  const body = await readJson(request);
  const keys = [`ip:${clientIp(request)}`, `email:${normalizeEmail(body.email)}`];
  await throttleCheck('login', ...keys);
  const user = await authenticate(body.email, body.password);
  if (!user) {
    await throttleFail('login', ...keys);
    throw new HttpError(401, "That email and password don't match an account.");
  }
  await throttleReset('login', keys[1]);
  const response = json({ user });
  setSessionCookie(response, await createSession(user.id));
  return response;
});
