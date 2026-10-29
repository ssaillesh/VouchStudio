import { AuthForm } from './site';
import { Brand, MarkSvg, RevealAll } from './ui';

export function AuthPage({ mode }: { mode: 'login' | 'signup' }) {
  const login = mode === 'login';
  return (
    <>
      <RevealAll />
      <header className="topbar site-bar">
        <Brand />
        <span className="tagline bar-tagline">Better jobs. Smarter search.</span>
      </header>
      <main className="auth-main">
        <section className="auth-card reveal" aria-labelledby="auth-title">
          <MarkSvg className="mark auth-mark" />
          <p className="eyebrow">{login ? 'Log in' : 'Create your account'}</p>
          <h1 id="auth-title">{login ? 'Welcome back.' : 'Start with your resume.'}</h1>
          <p className="lede">{login ? 'Log in to see the board scored against your profile.'
            : 'Make an account, upload your resume or master profile, and every job on the board is scored for you.'}</p>
          <AuthForm mode={mode} />
          <p className="auth-switch">
            {login ? <>New to VouchStudio? <a href="/signup">Create an account</a></>
              : <>Already have an account? <a href="/login">Log in</a></>}
          </p>
        </section>
      </main>
    </>
  );
}
