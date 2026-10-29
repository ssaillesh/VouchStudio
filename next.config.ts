import type { NextConfig } from 'next';

// The repository root is also the Python engine's home. Next.js only builds src/; the
// Python function in api/engine.py is deployed by Vercel next to it (see vercel.json).
const nextConfig: NextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
  serverExternalPackages: ['unpdf'],
  outputFileTracingExcludes: {
    '*': ['target_jobs/**', 'target_jobs.zip', 'outputs/**', 'tests/**', '.cache/**',
      '.studio/**', '.venv/**', '*.pdf', '*.py'],
  },
  async headers() {
    return [{
      source: '/:path*',
      headers: [
        { key: 'X-Content-Type-Options', value: 'nosniff' },
        { key: 'Referrer-Policy', value: 'no-referrer' },
        { key: 'X-Frame-Options', value: 'SAMEORIGIN' },
        { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=()' },
        { key: 'Strict-Transport-Security', value: 'max-age=63072000; includeSubDomains' },
      ],
    }];
  },
};

export default nextConfig;
