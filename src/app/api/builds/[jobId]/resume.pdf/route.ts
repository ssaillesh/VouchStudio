import { NextResponse, type NextRequest } from 'next/server';
import { requireUser } from '@/lib/auth';
import { db } from '@/lib/db';
import { HttpError, route } from '@/lib/http';
import { presignGet } from '@/lib/r2';

const slug = (text: string) => text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 48);

/** The verified PDF: a short-lived R2 link, or the bytes when the build kept them in Neon. */
export const GET = route(async (request: NextRequest, { params }: { params: Promise<{ jobId: string }> }) => {
  const user = await requireUser(request);
  const { jobId } = await params;
  const rows = await db()`SELECT title, company, pdf_key, encode(pdf, 'base64') AS pdf
                          FROM builds WHERE user_id = ${user.id} AND job_id = ${jobId}`;
  const build = rows[0];
  if (!build || (!build.pdf_key && !build.pdf)) throw new HttpError(404, 'No verified resume for this job yet');
  const filename = `${slug(build.company || 'resume') || 'resume'}_${slug(build.title) || 'job'}.pdf`;
  if (build.pdf_key) {
    return NextResponse.redirect(await presignGet(build.pdf_key, { filename, contentType: 'application/pdf' }), 302);
  }
  return new NextResponse(Buffer.from(build.pdf, 'base64'), { headers: { 'Content-Type': 'application/pdf',
    'Content-Disposition': `inline; filename="${filename}"`, 'Cache-Control': 'no-store' } });
});
