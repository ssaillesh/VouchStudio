import type { Metadata } from 'next';
import { AuthPage } from '@/components/AuthPage';

export const metadata: Metadata = { title: 'VouchStudio: log in' };

export default function Login() {
  return <AuthPage mode="login" />;
}
