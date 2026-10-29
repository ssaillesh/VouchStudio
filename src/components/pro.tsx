'use client';

// Plans: Basic scores the board; Pro also gets a tailored resume per job. On the hosted app
// a build is queued, then run where LaTeX is installed (`./run_ui.sh --process-builds`).
import { useCallback, useEffect, useState } from 'react';
import { useSession } from './session';
import { api, Chip, EmptyState, Icon, statusLabel } from './ui';

const PRO_POINTS = [
  'A one-page resume tailored to each job, built from your own master profile',
  'Every line traced to a verified fact; nothing invented, no AI rewriting',
  'Checked to compile, fit one page and read cleanly in applicant tracking systems',
  'A report of what each job asks, what your resume shows, and the top changes'];

const BUILD_NEEDS: Record<string, [string, string]> = {
  profile: ['Upload your master profile', 'The resume is built only from your own verified facts.'],
  master: ['Use a tagged master profile', 'A plain resume has no [V] or [NO] tags to build from.'],
  kit: ['Add a resume kit (section 9)', 'Your roles, projects and bullet bank, in your master profile.'],
  header: ['Add your resume header', 'Your name and contact line, plus education.'],
};

interface Needs { pro: boolean; missing: string[]; ready: boolean }
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Header = Record<string, any>;
export interface Build {
  job_id: string; title: string; company: string; status: string; created: string;
  has_pdf: boolean; has_report: boolean;
  summary: { verdict?: string; fit_score?: number; profile_fit?: number | null; resume_fit?: number | null;
    reason?: string; override_low_fit?: boolean };
}

export function ProPitch() {
  return <ul className="pro-points">{PRO_POINTS.map((t) => <li key={t}><Icon name="check" size={12} />{t}</li>)}</ul>;
}

export function ProRequestBox({ onSent }: { onSent?: () => void }) {
  const { proRequest, setProRequest, toast } = useSession();
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  if (proRequest?.status === 'pending') {
    return <div className="notice"><strong>Request sent. </strong>
      You asked for Pro on {new Date(proRequest.created_at * 1000).toLocaleDateString()}. Your account switches to Pro
      as soon as it is approved.</div>;
  }
  return (
    <div className="stack">
      {proRequest?.status === 'declined'
        ? <div className="notice warn small">Your last request was not approved. You can ask again.</div> : null}
      <textarea rows={3} maxLength={600} placeholder="Optional: what you are applying for" value={note}
        onChange={(e) => setNote(e.target.value)} />
      <p className="form-error">{error}</p>
      <div><button className="btn primary" type="button" disabled={busy} onClick={async () => {
        setBusy(true);
        try {
          setProRequest((await api('/api/me/pro-request', { method: 'POST', body: { note } })).pro_request);
          toast('Request sent to the admin');
          onSent?.();
        } catch (err) {
          setError((err as Error).message);
          setBusy(false);
        }
      }}>Request Pro</button></div>
    </div>
  );
}

export function NeedsList({ missing }: { missing: string[] }) {
  return (
    <ul className="needs">{Object.entries(BUILD_NEEDS).map(([key, [title, why]]) => (
      <li key={key} className={missing.includes(key) ? 'todo' : 'done'}>
        <Icon name={missing.includes(key) ? 'cross' : 'check'} size={12} />
        <div><strong>{title}</strong><span>{why}</span></div>
      </li>))}
    </ul>
  );
}

function HeaderForm({ header, onSaved }: { header: Header | null; onSaved: (data: { header: Header; build: Needs }) => void }) {
  const { toast } = useSession();
  const hd = header || {};
  const [fields, setFields] = useState({ name: hd.name || '', email: hd.email || '', phone: hd.phone || '',
    location: hd.location || '' });
  const [links, setLinks] = useState<string[]>([0, 1, 2, 3].map((i) => (hd.links || [])[i] || ''));
  const [education, setEducation] = useState<Header[]>([0, 1].map((i) => ({ school: '', degree: '', dates: '',
    location: '', ...((hd.education || [])[i] || {}) })));
  const [error, setError] = useState('');
  const field = (key: keyof typeof fields, label: string, attrs: Record<string, unknown> = {}) => (
    <label className="field">{label}
      <input type="text" value={fields[key]} {...attrs}
        onChange={(e) => setFields((f) => ({ ...f, [key]: e.target.value }))} /></label>
  );
  const placeholders: Record<string, string> = { school: 'School', degree: 'Degree', dates: 'Dates (2022 to 2026)', location: 'Location' };
  return (
    <div className="stack">
      <div className="form-grid">
        {field('name', 'Name on the resume', { maxLength: 120 })}
        {field('email', 'Email', { maxLength: 120, type: 'email' })}
        {field('phone', 'Phone', { maxLength: 25 })}
        {field('location', 'Location', { maxLength: 120, placeholder: 'City, Province' })}
      </div>
      <h3>Links</h3>
      <div className="form-grid">{links.map((value, i) => (
        <input key={i} type="text" value={value} aria-label={`Link ${i + 1}`}
          placeholder={i === 0 ? 'linkedin.com/in/you' : i === 1 ? 'github.com/you' : 'Another link'}
          onChange={(e) => setLinks((all) => all.map((l, j) => (j === i ? e.target.value : l)))} />))}</div>
      <h3>Education</h3>
      {education.map((row, i) => (
        <div key={i} className="form-grid four">{['school', 'degree', 'dates', 'location'].map((k) => (
          <input key={k} type="text" value={row[k]} placeholder={placeholders[k]} aria-label={`Education ${i + 1} ${k}`}
            onChange={(e) => setEducation((all) => all.map((r, j) => (j === i ? { ...r, [k]: e.target.value } : r)))} />))}
        </div>))}
      <p className="faint small">Education and roles are checked against your master profile before they appear.</p>
      <p className="form-error">{error}</p>
      <div><button className="btn primary" type="button" onClick={async () => {
        setError('');
        try {
          const saved = await api('/api/me/resume-header', { method: 'POST',
            body: { ...fields, links: links.filter((l) => l.trim()), education } });
          toast('Resume header saved');
          onSaved(saved);
        } catch (err) { setError((err as Error).message); }
      }}>Save header</button></div>
    </div>
  );
}

export function PlanSection() {
  const { user } = useSession();
  const [data, setData] = useState<{ header: Header | null; build: Needs } | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (user.is_pro) api('/api/me/resume-header').then(setData).catch((err) => setError(err.message));
  }, [user.is_pro]);
  if (!user.is_pro) {
    return (
      <section className="plan">
        <div className="plan-head"><h2>Pro: a tailored resume for every job</h2><Chip kind="tag">Basic plan</Chip></div>
        <p className="muted">Your plan scores every job against your profile. Pro also builds the resume.</p>
        <ProPitch /><ProRequestBox />
      </section>
    );
  }
  if (error) return <section className="plan"><EmptyState title="Could not load your plan" text={error} /></section>;
  if (!data) return <section className="plan"><p className="muted">Loading your Pro setup…</p></section>;
  return (
    <section className="plan">
      <div className="plan-head"><h2>Your Pro resume setup</h2><Chip kind="tag pass">{user.is_admin ? 'Admin' : 'Pro'}</Chip></div>
      <p className="muted">{data.build.ready ? 'Everything is ready. Open any job and request a resume tailored to it.'
        : 'Finish these and every job page gets a Build my resume button.'}</p>
      <NeedsList missing={data.build.missing} />
      <h3 className="gap-head">Resume header</h3>
      <HeaderForm header={data.header} onSaved={setData} />
    </section>
  );
}

function FitCard({ label, value, sub, kind }: { label: string; value: number | null | undefined; sub: string; kind: string }) {
  return (
    <div className={`fit-card ${kind}`}>
      <div className="fit-label">{label}</div>
      <div className="fit-value">{value == null ? 'n/a' : value.toFixed(1)}<span>/10</span></div>
      {sub ? <div className="fit-sub">{sub}</div> : null}
    </div>
  );
}

/** The job page's resume card: a pitch for Basic, a checklist or the build for Pro. */
export function BuildCard({ job, initial }: { job: { id: string; title: string; company: string }; initial: Build | null }) {
  const { user, meta } = useSession();
  const [needs, setNeeds] = useState<Needs | null>(null);
  const [build, setBuild] = useState<Build | null>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [override, setOverride] = useState(false);
  const refresh = useCallback(() => api(`/api/jobs/${encodeURIComponent(job.id)}`).then((j) => setBuild(j.my_build)), [job.id]);
  useEffect(() => {
    if (user.is_pro) api('/api/me/resume-header').then((d) => setNeeds(d.build)).catch((err) => setError(err.message));
  }, [user.is_pro]);
  useEffect(() => {  // a queued build finishes on the owner's machine; look again every 30 s
    if (build?.status !== 'queued') return;
    const timer = setInterval(() => { refresh().catch(() => undefined); }, 30_000);
    return () => clearInterval(timer);
  }, [build?.status, refresh]);

  if (!user.is_pro) {
    return (
      <div className="card build-card">
        <div className="plan-head"><h2>Want a resume tailored to this job?</h2><Chip kind="tag accent">Pro</Chip></div>
        <ProPitch /><ProRequestBox />
      </div>
    );
  }
  const s = build?.summary || {};
  const pdfUrl = `/api/builds/${encodeURIComponent(job.id)}/resume.pdf?v=${encodeURIComponent(build?.created || '')}`;
  const queued = build?.status === 'queued';
  return (
    <div className="card build-card">
      <div className="plan-head"><h2>Your tailored resume</h2><Chip kind="tag pass">Pro</Chip></div>
      {needs && !needs.ready ? (
        <>
          <p className="muted">A few things first:</p>
          <NeedsList missing={needs.missing} />
          <a className="btn" href="/app/me">Finish setup</a>
        </>
      ) : null}
      {error ? <div className="notice fail small">{error}</div> : null}
      {build ? (
        <div className="stack">
          <div className="row">
            <Chip kind={`tag ${build.has_pdf && !queued ? 'pass' : 'warn'}`}>
              {queued ? 'Queued' : build.has_pdf ? 'Verified one page' : statusLabel(build.status)}</Chip>
            {s.verdict ? <Chip kind="tag">{s.verdict === 'PROCEED' ? `Skill gate ${s.fit_score}` : `Low fit ${s.fit_score}`}</Chip> : null}
            <span className="faint small">{queued ? 'requested' : 'built'} {build.created.replace('T', ' ')}</span>
          </div>
          {queued ? <p className="muted small">Your resume is in the build queue. It is compiled and checked on the
            VouchStudio build machine, usually within a day; this card updates when it is ready.</p> : null}
          {s.profile_fit != null ? (
            <div className="fit-cards">
              <FitCard label="Profile fit" value={s.profile_fit} sub="what your verified facts support"
                kind={s.profile_fit >= 7 ? 'good' : s.profile_fit >= 5 ? 'mid' : 'low'} />
              <FitCard label="Resume fit" value={s.resume_fit} sub={s.resume_fit == null ? 'no resume built' : 'what this PDF shows'}
                kind={s.resume_fit == null ? '' : s.resume_fit >= 7 ? 'good' : 'mid'} />
            </div>) : null}
          {!build.has_pdf && s.reason ? <div className="notice warn small">{s.reason}</div> : null}
          {!build.has_pdf && s.verdict && s.verdict !== 'PROCEED' && !s.override_low_fit
            ? <p className="muted small">This job scores below {meta.threshold}, so no resume was built. Tick
              &quot;Build even if low fit&quot; to build one anyway.</p> : null}
          {build.has_pdf ? (
            <div className="window">
              <div className="window-bar"><span /><span /><span /><em>{job.company || ''} · {job.title}</em></div>
              <iframe className="build-frame" src={pdfUrl} title="Your tailored resume" />
            </div>) : null}
          <div className="row">
            {build.has_pdf ? <a className="btn" href={pdfUrl} target="_blank" rel="noopener">Open PDF<Icon name="external" /></a> : null}
            {build.has_report ? <a className="btn ghost" href={`/api/builds/${encodeURIComponent(job.id)}/report`}
              target="_blank" rel="noopener">Run report<Icon name="external" /></a> : null}
          </div>
        </div>) : null}
      {needs?.ready ? (
        <div className="row gap-top">
          <button className="btn primary" type="button" disabled={busy || queued} onClick={async () => {
            setBusy(true);
            setError('');
            try {
              const res = await api(`/api/builds/${encodeURIComponent(job.id)}`, { method: 'POST',
                body: { override_low_fit: override } });
              setBuild(res.build);
            } catch (err) { setError((err as Error).message); }
            setBusy(false);
          }}>{queued ? 'Queued' : busy ? 'Requesting…' : build ? 'Rebuild' : 'Build my resume'}</button>
          <label className="check" title="The skill gate stays low; the resume is still built">
            <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />Build even if low fit</label>
        </div>) : null}
    </div>
  );
}
