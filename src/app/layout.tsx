import type { Metadata, Viewport } from 'next';
import { headers } from 'next/headers';
import type { ReactNode } from 'react';
import '../../static/studio.css';
import '../styles/web.css';

export const metadata: Metadata = {
  title: 'VouchStudio: Better jobs. Smarter search.',
  description: 'Upload your resume or a tagged master profile and see every open role scored against the evidence you have.',
  icons: { icon: { url: '/static/brand/favicon.svg', type: 'image/svg+xml' },
    apple: '/static/brand/apple-touch-icon.png' },
};

export const viewport: Viewport = { width: 'device-width', initialScale: 1, colorScheme: 'light' };

export default async function RootLayout({ children }: { children: ReactNode }) {
  await headers();  // every page renders per request, so Next.js can apply the CSP nonce (src/proxy.ts)
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
