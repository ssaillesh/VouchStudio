'use client';

import { useEffect, useState } from 'react';
import { PlanSection } from '@/components/pro';
import { useSession } from '@/components/session';
import { api, Chip, Reveal, uploadToR2 } from '@/components/ui';

const MAX_BYTES = 2_000_000;

interface MyProfile {
  stored: { filename: string; text: string | null; editable: boolean; error: string | null; updated: string } | null;
  summary: { mode?: string; filename: string; evidence_lines?: number; skills?: string[]; no_skills?: string[];
    warnings?: string[]; error?: string } | null;
  threshold: number; scored: number; proceed: number; thin: number;
}

export default function MePage() {
  const { toast } = useSession();
  const [data, setData] = useState<MyProfile | null>(null);
  const [status, setStatus] = useState('');
  const [paste, setPaste] = useState('');
  const [draft, setDraft] = useState('');
  const [over, setOver] = useState(false);
  const [password, setPassword] = useState('');

  useEffect(() => { api<MyProfile>('/api/me/profile').then(setData).catch((err) => setStatus(err.message)); }, []);
  useEffect(() => { setDraft(data?.stored?.text ?? ''); }, [data?.stored?.text]);

  const scored = (d: MyProfile) => setStatus(`Scored ${d.scored.toLocaleString()} jobs; ${d.proceed.toLocaleString()} at or above `
    + `${d.threshold}. ${d.thin.toLocaleString()} had too little detail to score.`);

  async function submitText(filename: string, text: string) {
    setStatus(`Scoring every job against ${filename}…`);
    try {
      const d = await api<MyProfile>('/api/me/profile', { method: 'POST', body: { filename, text } });
      setData(d);
      scored(d);
    } catch (err) { setStatus((err as Error).message); }
  }

  async function submitFile(file: File | undefined) {
    if (!file) return;
    if (file.size > MAX_BYTES) { setStatus('That file is larger than 2 MB.'); return; }
    try {
      setStatus(`Uploading ${file.name}…`);
      const ticket = await api('/api/uploads', { method: 'POST', body: { purpose: 'profile', filename: file.name, size: file.size } });
      await uploadToR2(ticket.url, file, ticket.headers);
      setStatus(`Scoring every job against ${file.name}…`);
      const d = await api<MyProfile>('/api/me/profile', { method: 'POST', body: { filename: file.name, upload_key: ticket.key } });
      setData(d);
      scored(d);
    } catch (err) { setStatus((err as Error).message); }
  }

  const stored = data?.stored;
  const p = data?.summary;
  let card;
  if (!data) card = <p className="muted">Loading…</p>;
  else if (!stored) {
    card = <><h2>No profile yet</h2><p className="stat-label">Upload your resume or master profile. Every job on the
      board is then scored against it, and only you can see it.</p></>;
  } else if (!p || p.error) {
    card = <><h2>{stored.filename}</h2><div className="notice fail small">{p?.error || stored.error || 'This file could not be scored.'}</div></>;
  } else {
    card = (
      <>
        <div className="row"><h2 className="grow">Your profile</h2>
          <Chip kind={`tag ${p.mode === 'master' ? 'pass' : 'warn'}`}>{p.mode === 'master' ? 'Master profile' : 'Plain resume'}</Chip></div>
        <p className="profile-file">{stored.filename} · updated {stored.updated.replace('T', ' ')}</p>
        <p className="stat-label">{p.evidence_lines} evidence lines · {p.skills?.length ?? 0} recognized skills ·{' '}
          {data.proceed.toLocaleString()} jobs at or above {data.threshold}</p>
        {p.warnings?.length ? <div className="notice warn small">{p.warnings.join(' ')}</div> : null}
        <h3 className="gap-head">Skills with evidence</h3>
        <div className="row">{p.skills?.length ? p.skills.map((s) => <Chip key={s} kind="pass">{s}</Chip>)
          : <span className="faint small">No recognized skills</span>}</div>
        {p.no_skills?.length ? <><h3 className="gap-head">Marked [NO]</h3>
          <div className="row">{p.no_skills.map((s) => <Chip key={s} kind="fail">{s}</Chip>)}</div></> : null}
        <div className="row gap-top">
          <a className="btn primary" href="/app">See your matches</a>
          <button className="btn ghost" type="button" onClick={async () => {
            if (!confirm('Remove your profile? The board will stop scoring jobs for you until you upload one again.')) return;
            setData(await api('/api/me/profile/delete', { method: 'POST' }));
            setStatus('Profile removed.');
          }}>Remove profile</button>
        </div>
      </>
    );
  }
  const editName = stored ? (stored.editable ? stored.filename : `${stored.filename.replace(/\.[^.]+$/, '')}.txt`) : '';

  return (
    <>
      <Reveal as="header" className="page-head">
        <p className="eyebrow">VouchStudio · your profile</p>
        <h1>See every job through your own evidence.</h1>
        <p className="lede">Upload your resume or a tagged master profile. It is kept with your account, used only
          to score the board for you, and never shown to anyone else.</p>
      </Reveal>
      <div className="resume-grid">
        <Reveal className="card" index={1}>
          <h2>{stored ? 'Replace it' : 'Upload'}</h2>
          <label className={`drop${over ? ' over' : ''}`}
            onDragEnter={(e) => { e.preventDefault(); setOver(true); }} onDragOver={(e) => { e.preventDefault(); setOver(true); }}
            onDragLeave={(e) => { e.preventDefault(); setOver(false); }}
            onDrop={(e) => { e.preventDefault(); setOver(false); submitFile(e.dataTransfer.files[0]); }}>
            <input type="file" accept=".md,.txt,.pdf" onChange={(e) => { submitFile(e.target.files?.[0]); e.target.value = ''; }} />
            <span className="drop-title">Drop a file here, or choose one</span>
            <span className="drop-sub">Master profile (.md), plain resume (.md or .txt), or PDF. Up to 2 MB.</span>
          </label>
          <p className="or">or paste the text</p>
          <textarea rows={7} placeholder="Paste your resume text here" value={paste} onChange={(e) => setPaste(e.target.value)} />
          <div className="row gap-top"><button className="btn primary" type="button" onClick={() => {
            if (!paste.trim()) { setStatus('Paste your resume text first.'); return; }
            submitText('pasted-resume.txt', paste);
          }}>Score pasted text</button></div>
          <p className="muted small" role="status">{status}</p>
        </Reveal>
        <Reveal className="card" index={2}>{card}</Reveal>
      </div>
      {stored && stored.text != null ? (
        <section className="faq">
          <h2>Edit your profile</h2>
          <p className="muted">{stored.editable ? 'Changes are scored as soon as you save.'
            : 'This profile was uploaded as a PDF. Saving here replaces it with this text.'}</p>
          <textarea className="profile-editor" spellCheck={false} aria-label="Your profile text" value={draft}
            onChange={(e) => setDraft(e.target.value)} />
          <div className="row gap-top"><button className="btn primary" type="button"
            onClick={() => submitText(editName, draft)}>Save changes</button></div>
        </section>) : null}
      <Reveal index={3}><PlanSection /></Reveal>
      <Reveal as="section" index={3} className="faq">
        <h2>How it works</h2>
        <details><summary>What the fit score means</summary><p>Each job&apos;s requirements are read from its description
          and weighted 3 for must-haves, 2 for required and 1 for nice-to-haves. The score is the share of that weight your
          profile backs with evidence. It measures coverage of what the posting asks for, not your chance of being hired.</p></details>
        <details><summary>Master profile versus plain resume</summary><p>A master profile tags each line as verified or [NO],
          so skills you have ruled out never count. A plain resume has no tags: every line counts as your own claim, which
          makes the score less strict. The card shows which mode was used.</p></details>
        <details><summary>Why some jobs say &quot;not enough detail&quot;</summary><p>A score needs at least three skill
          requirements the parser recognizes. Many sales, finance and operations postings name only a degree or terms
          outside the skill vocabulary, and a score built on one match would be misleading, so those jobs are listed without one.</p></details>
        <details><summary>Who can see my profile</summary><p>Only you. The file is stored privately with your account and
          used to score jobs for you. Removing it, or deleting your account, deletes it.</p></details>
      </Reveal>
      <Reveal as="section" index={4} className="danger">
        <h2>Delete your account</h2>
        <p>This deletes your account, your stored profile, your reviews and their files, and your sessions. It cannot be undone.</p>
        <div className="row">
          <input type="password" autoComplete="current-password" placeholder="Your password" aria-label="Password to confirm"
            value={password} onChange={(e) => setPassword(e.target.value)} />
          <button className="btn danger-btn" type="button" onClick={async () => {
            if (!password) { toast('Enter your password to confirm.', 'fail'); return; }
            if (!confirm('Delete your account and everything in it?')) return;
            try {
              await api('/api/me/delete', { method: 'POST', body: { password } });
              window.location.href = '/';
            } catch (err) { toast((err as Error).message, 'fail'); }
          }}>Delete account</button>
        </div>
      </Reveal>
    </>
  );
}
