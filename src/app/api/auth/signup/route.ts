import type { NextRequest } from 'next/server';
import { createSession, createUser, setSessionCookie, throttleCheck, throttleFail } from '@/lib/auth';
import { clientIp, json, readJson, route } from '@/lib/http';

export const POST = route(async (request: NextRequest) => {
  const body = await readJson(request);
  const ip = clientIp(request);
  await throttleCheck('signup', `ip:${ip}`);
  await throttleFail('signup', `ip:${ip}`);  // every sign-up counts toward the limit
  const user = await createUser(body.email, body.password, body.name);
  const response = json({ user }, 201);
  setSessionCookie(response, await createSession(user.id));
  return response;
});
