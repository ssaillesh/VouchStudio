import { NextResponse, type NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { route } from '@/lib/http';
import { mediaUrl } from '@/lib/reviews';

/** A review attachment: redirects to a five-minute R2 link for anyone who can see the review. */
export const GET = route(async (request: NextRequest, { params }: { params: Promise<{ id: string }> }) => {
  const user = await requireUser(request);
  return NextResponse.redirect(await mediaUrl(user, (await params).id), 302);
});
