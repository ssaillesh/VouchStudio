// Apply db/schema.sql to the Neon database in DATABASE_URL. Every statement is idempotent,
// so this runs before every Vercel build (`vercel-build`) and by hand with `npm run db:migrate`.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { neon } from '@neondatabase/serverless';

const url = process.env.DATABASE_URL;
if (!url) {
  console.error('DATABASE_URL is not set (the Neon connection string; see .env.example).');
  process.exit(1);
}
const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const schema = readFileSync(join(root, 'db', 'schema.sql'), 'utf8')
  .split('\n').filter((line) => !line.trimStart().startsWith('--')).join('\n');
const statements = schema.split(/;\s*(?:\n|$)/).map((s) => s.trim()).filter(Boolean);
const sql = neon(url);
for (const statement of statements) await sql.query(statement);
console.log(`Schema applied: ${statements.length} statements.`);
