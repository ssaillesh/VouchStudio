'use client';

// Reviews: what members say about a company or one of its postings. Attachments go straight
// from the browser to R2 (presigned PUT from /api/uploads) and are attached on posting.
import { useState } from 'react';
import { useSession } from './session';
import { api, Chip, EmptyState, ExtLink, Icon, uploadToR2 } from './ui';

export interface Media { id: string; kind: 'image' | 'pdf' | 'video'; filename: string; content_type: string;
  size: number; url: string }
export interface Review {
  id: number; company: string; job_id: string | null; job_title: string | null; rating: number;
  title: string; body: string; links: string[]; status: string; created_at: string; author: string;
  mine: boolean; media: Media[];
}

const ACCEPT = 'image/jpeg,image/png,image/webp,image/gif,application/pdf,video/mp4,video/webm,video/quicktime';
const MAX_FILES = 6;

function Stars({ rating }: { rating: number }) {
  return (
    <span className="stars" aria-label={`${rating} out of 5`}>
      {[1, 2, 3, 4, 5].map((n) => <span key={n} className={n <= rating ? 'on' : ''}><Icon name="star" size={14} /></span>)}
    </span>
  );
}

function MediaView({ media }: { media: Media }) {
  if (media.kind === 'image') {
    return <a className="media image" href={media.url} target="_blank" rel="noopener">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={media.url} alt={media.filename} loading="lazy" /></a>;
  }
  if (media.kind === 'video') {
    return <video className="media video" src={media.url} controls preload="metadata" aria-label={media.filename} />;
  }
  return <a className="media pdf chip tag" href={media.url} target="_blank" rel="noopener">{media.filename}<Icon name="external" /></a>;
}

export function ReviewCard({ review, onRemove, onUpdate }: { review: Review; onRemove?: (id: number) => void;
  onUpdate?: (review: Review) => void }) {
  const { user, toast } = useSession();
  const remove = async () => {
    if (!confirm('Delete this review and its attachments?')) return;
    try {
      await api(`/api/reviews/${review.id}`, { method: 'DELETE' });
      onRemove?.(review.id);
    } catch (err) { toast((err as Error).message, 'fail'); }
  };
  const toggle = async () => {
    const status = review.status === 'hidden' ? 'published' : 'hidden';
    try {
      await api(`/api/reviews/${review.id}`, { method: 'PATCH', body: { status } });
      onUpdate?.({ ...review, status });
    } catch (err) { toast((err as Error).message, 'fail'); }
  };
  return (
    <article className="card review">
      <div className="row">
        <Stars rating={review.rating} />
        <h3 className="review-title grow">{review.title}</h3>
        {review.status === 'hidden' ? <Chip kind="tag warn">Hidden</Chip> : null}
      </div>
      <p className="sub small muted">
        {review.company}{review.job_title ? <> · <a href={`/app/jobs/${encodeURIComponent(review.job_id || '')}`}>{review.job_title}</a></> : null}
        {' · '}{review.author} · {new Date(review.created_at).toLocaleDateString()}
      </p>
      <p className="review-body">{review.body}</p>
      {review.media.length ? <div className="media-grid">{review.media.map((m) => <MediaView key={m.id} media={m} />)}</div> : null}
      {review.links.length ? <ul className="review-links small">{review.links.map((l) =>
        <li key={l}><ExtLink href={l}>{new URL(l).host}</ExtLink></li>)}</ul> : null}
      {review.mine || user.is_admin ? (
        <div className="row gap-top">
          {user.is_admin ? <button className="btn ghost small" type="button" onClick={toggle}>
            {review.status === 'hidden' ? 'Publish' : 'Hide'}</button> : null}
          <button className="btn ghost small" type="button" onClick={remove}>Delete</button>
        </div>) : null}
    </article>
  );
}

export function ReviewList({ reviews, onRemove, onUpdate, empty }: { reviews: Review[];
  onRemove?: (id: number) => void; onUpdate?: (review: Review) => void; empty?: string }) {
  if (!reviews.length) return empty ? <EmptyState title="No reviews yet" text={empty} /> : null;
  return <div className="stack">{reviews.map((r) => <ReviewCard key={r.id} review={r} onRemove={onRemove} onUpdate={onUpdate} />)}</div>;
}

interface Pending { file: File; id?: string; progress: number; error?: string }

export function ReviewComposer({ jobId, company: initialCompany, onPosted }: { jobId?: string; company?: string;
  onPosted: (review: Review) => void }) {
  const [company, setCompany] = useState(initialCompany || '');
  const [rating, setRating] = useState(0);
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [links, setLinks] = useState('');
  const [files, setFiles] = useState<Pending[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const setFile = (index: number, patch: Partial<Pending>) =>
    setFiles((all) => all.map((f, i) => (i === index ? { ...f, ...patch } : f)));

  async function upload(file: File, index: number): Promise<string> {
    const ticket = await api('/api/uploads', { method: 'POST', body: { purpose: 'review', filename: file.name,
      content_type: file.type, size: file.size } });
    await uploadToR2(ticket.url, file, ticket.headers, (p) => setFile(index, { progress: p }));
    setFile(index, { id: ticket.id, progress: 1 });
    return ticket.id;
  }

  async function submit() {
    setError('');
    if (!rating) { setError('Pick a rating from 1 to 5.'); return; }
    setBusy(true);
    try {
      const ids: string[] = [];
      for (const [i, f] of files.entries()) {
        try {
          ids.push(f.id ?? await upload(f.file, i));
        } catch (err) {
          setFile(i, { error: (err as Error).message });
          throw err;
        }
      }
      const res = await api('/api/reviews', { method: 'POST', body: { job_id: jobId || null, company, rating,
        title, body, links: links.split(/\s+/).filter(Boolean), media_ids: ids } });
      onPosted(res.review);
      setRating(0); setTitle(''); setBody(''); setLinks(''); setFiles([]);
    } catch (err) {
      setError((err as Error).message);
    }
    setBusy(false);
  }

  return (
    <section className="card composer">
      <h2>Write a review</h2>
      <div className="form-grid">
        <label className="field">Company
          <input type="text" value={company} maxLength={120} disabled={Boolean(jobId)}
            onChange={(e) => setCompany(e.target.value)} placeholder="Company name" /></label>
        <div className="field"><span>Rating</span>
          <div className="rating-input" role="radiogroup" aria-label="Rating">
            {[1, 2, 3, 4, 5].map((n) => (
              <button key={n} type="button" role="radio" aria-checked={rating === n} aria-label={`${n} star${n === 1 ? '' : 's'}`}
                className={n <= rating ? 'on' : ''} onClick={() => setRating(n)}><Icon name="star" size={20} /></button>))}
          </div>
        </div>
      </div>
      <label className="field">Title
        <input type="text" value={title} maxLength={140} onChange={(e) => setTitle(e.target.value)}
          placeholder="One line: the interview, the team, the offer" /></label>
      <label className="field">Your review
        <textarea rows={7} value={body} maxLength={5000} onChange={(e) => setBody(e.target.value)}
          placeholder="What happened, what you would tell a friend applying here" />
        <span className="hint">{body.length.toLocaleString()} / 5,000</span></label>
      <label className="field">Links <span className="faint">(optional, up to 5, separated by spaces)</span>
        <input type="text" value={links} onChange={(e) => setLinks(e.target.value)}
          placeholder="https://www.glassdoor.com/… https://github.com/…" /></label>
      <label className="drop">
        <input type="file" multiple accept={ACCEPT} onChange={(e) => {
          const chosen = Array.from(e.target.files || []);
          setFiles((all) => [...all, ...chosen.map((file) => ({ file, progress: 0 }))].slice(0, MAX_FILES));
          e.target.value = '';
        }} />
        <span className="drop-title">Add photos, PDFs or a video</span>
        <span className="drop-sub">Images and PDFs up to 10 MB, videos (MP4, WebM, MOV) up to 200 MB. Up to {MAX_FILES} files.</span>
      </label>
      {files.length ? (
        <ul className="uploads">{files.map((f, i) => (
          <li key={`${f.file.name}-${i}`}>
            <span className="grow">{f.file.name} <span className="faint">({(f.file.size / 1e6).toFixed(1)} MB)</span></span>
            {f.error ? <Chip kind="fail tag">{f.error}</Chip>
              : <progress max={1} value={f.progress} aria-label={`Upload of ${f.file.name}`} />}
            <button className="btn ghost small" type="button" disabled={busy}
              onClick={() => setFiles((all) => all.filter((_, j) => j !== i))}>Remove</button>
          </li>))}
        </ul>) : null}
      <p className="faint small">Shown to signed-in members under your account name. Your email is never shown.</p>
      <p className="form-error" role="alert">{error}</p>
      <div><button className="btn primary" type="button" disabled={busy} onClick={submit}>
        {busy ? 'Posting…' : 'Post review'}</button></div>
    </section>
  );
}
