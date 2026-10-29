'use client';

import { useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useState } from 'react';
import { ReviewComposer, ReviewList, type Review } from '@/components/reviews';
import { api, Reveal, useDebounced } from '@/components/ui';

function ReviewsView() {
  const params = useSearchParams();
  const jobId = params.get('job') || undefined;
  const [company, setCompany] = useState(params.get('company') || '');
  const [mine, setMine] = useState(false);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [more, setMore] = useState(false);
  const [error, setError] = useState('');
  const [writing, setWriting] = useState(Boolean(jobId));

  const load = useCallback(async (append: boolean, offset = 0) => {
    const q = new URLSearchParams({ offset: String(offset) });
    if (company.trim()) q.set('company', company.trim());
    if (mine) q.set('mine', '1');
    try {
      const data = await api<{ reviews: Review[] }>(`/api/reviews?${q}`);
      setReviews((old) => (append ? old.concat(data.reviews) : data.reviews));
      setMore(data.reviews.length === 20);
      setError('');
    } catch (err) { setError((err as Error).message); }
  }, [company, mine]);
  const reload = useDebounced(() => load(false), 250);
  useEffect(() => { reload(); }, [company, mine]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      <Reveal as="header" className="page-head">
        <p className="eyebrow">VouchStudio · reviews</p>
        <h1>What it is really like to apply.</h1>
        <p className="lede">Members review the companies on the board: interviews, offers and teams, with
          screenshots, documents and video when they help.</p>
      </Reveal>
      {writing ? (
        <ReviewComposer jobId={jobId} company={params.get('company') || ''} onPosted={(review) => {
          setReviews((all) => [review, ...all]);
          setWriting(false);
        }} />
      ) : null}
      <div className="section-head">
        <h2>{company.trim() ? `Reviews of ${company.trim()}` : 'Latest reviews'}</h2>
        <span className="spacer" />
        {writing ? null : <button className="btn primary" type="button" onClick={() => setWriting(true)}>Write a review</button>}
      </div>
      <div className="toolbar">
        <input type="search" placeholder="Company" aria-label="Filter by company" value={company}
          onChange={(e) => setCompany(e.target.value)} />
        <label className="check"><input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} />Only mine</label>
      </div>
      {error ? <div className="notice fail small">{error}</div> : null}
      <ReviewList reviews={reviews} empty="Be the first to review this company."
        onRemove={(id) => setReviews((all) => all.filter((r) => r.id !== id))}
        onUpdate={(review) => setReviews((all) => all.map((r) => (r.id === review.id ? review : r)))} />
      <div className="load-more">
        {more ? <button className="btn" type="button" onClick={() => load(true, reviews.length)}>Show more</button> : null}
      </div>
    </>
  );
}

export default function ReviewsPage() {
  return <Suspense fallback={<div className="panel empty">Loading…</div>}><ReviewsView /></Suspense>;
}
