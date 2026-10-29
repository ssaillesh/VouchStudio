// Copy the brand assets from static/ (the local Studio's folder, the one source of truth)
// to public/static/ so Next.js serves them at the same /static/brand/... URLs.
import { cpSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const target = join(root, 'public', 'static', 'brand');
rmSync(target, { recursive: true, force: true });
mkdirSync(dirname(target), { recursive: true });
cpSync(join(root, 'static', 'brand'), target, { recursive: true });
