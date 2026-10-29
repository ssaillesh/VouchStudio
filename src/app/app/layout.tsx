import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import { AppShell } from '@/components/session';

export const metadata: Metadata = { title: 'VouchStudio' };

export default function AppLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
