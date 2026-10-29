import type { Metadata } from 'next';
import { AuthPage } from '@/components/AuthPage';

export const metadata: Metadata = { title: 'VouchStudio: create your account' };

export default function Signup() {
  return <AuthPage mode="signup" />;
}
