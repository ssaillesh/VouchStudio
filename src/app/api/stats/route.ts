import { db } from '@/lib/db';
import { json, route } from '@/lib/http';

/** What the home page may show anyone: counts about the board, nothing personal. */
export const GET = route(async () => {
  const [row] = await db()`
    SELECT count(*)::int AS jobs, count(DISTINCT NULLIF(company, ''))::int AS companies,
           count(*) FILTER (WHERE enough_detail)::int AS scorable,
           (SELECT (value->>'threshold')::int FROM board WHERE key = 'index') AS threshold
    FROM jobs`;
  return json({ ...row, threshold: row.threshold ?? 65 });
});
