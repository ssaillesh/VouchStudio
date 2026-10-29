'use client';

import { useParams } from 'next/navigation';
import { useEffect, useState } from 'react';
import { BuildCard, type Build } from '@/components/pro';
import { ReviewList, type Review } from '@/components/reviews';
import { useSession } from '@/components/session';
import { api, Chip, EmptyState, GAP_LABEL, Icon, Markdown, postingBody, Reveal, regionKind,
  safeUrl } from '@/components/ui';

interface JobDetail {
  id: string; title: string; company: string; location: string; region: string; level: string;
  url: string | null; text: string; profile: { error?: string } | null; my_build: Build | null;
  my_score: { score: number; insufficient: boolean; matched: string[]; gaps: [string, string][];
    requirements: number; skill_requirements: number } | null;
}

export default function JobPage() {
  const { id } = useParams<{ id: string }>();
  const { meta } = useSession();
  const [job, setJob] = useState<JobDetail | null>(null);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [error, setError] = useState('');

  useEffect(() => {
    api<JobDetail>(`/api/jobs/${encodeURIComponent(id)}`).then((data) => {
      setJob(data);
      if (data.company) {
        api(`/api/reviews?company=${encodeURIComponent(data.company)}`).then((r) => setReviews(r.reviews)).catch(() => undefined);
      }
    }).catch((err) => setError(err.message));
  }, [id]);

  if (error) return <EmptyState title="Something went wrong" text={error} />;
  if (!job) return <div className="panel empty">Loading the job…</div>;
  const mine = job.my_score;
  const pass = mine && !mine.insufficient && mine.score >= meta.threshold;
  const posting = safeUrl(job.url);
  let scoreBlock;
  if (!job.profile) {
    scoreBlock = <div className="notice">Upload your profile to see how you fit this job.{' '}
      <a href="/app/me">Upload it<Icon name="arrow" size={11} /></a></div>;
  } else if (!mine || mine.insufficient) {
    scoreBlock = <div className="notice">{mine
      ? `This posting names only ${mine.skill_requirements} recognized skill requirement${mine.skill_requirements === 1 ? '' : 's'}, too few to score. Read the description to judge fit.`
      : 'This posting has no full description, so it cannot be scored.'}</div>;
  } else {
    scoreBlock = (
      <div className="card">
        <div className="job-score">
          <span className={`score ${pass ? 'proceed' : ''}`}>{mine.score}<small>{mine.requirements} requirements</small></span>
          <div className="grow">
            <Chip kind={`${pass ? 'pass' : 'fail'} tag`}>{pass ? `${meta.threshold}+ for you` : `Below ${meta.threshold}`}</Chip>
            <p className="verdict-line">The share of this posting&apos;s weighted requirements your profile backs with evidence.</p>
            <div className="detail">
              <div><h4>Evidence matched</h4><div className="chips">{mine.matched.length
                ? mine.matched.map((m) => <Chip key={m} kind="pass">{m}</Chip>) : <span className="faint small">None</span>}</div></div>
              <div><h4>Gaps</h4><div className="chips">{mine.gaps.length
                ? mine.gaps.map(([name, st]) => <span key={name} className={`chip ${st === 'explicitly_no' ? 'fail' : ''}`}>
                  {name}<em>{GAP_LABEL[st] || st}</em></span>) : <span className="faint small">None</span>}</div></div>
            </div>
          </div>
        </div>
      </div>
    );
  }
  const reviewHref = `/app/reviews?job=${encodeURIComponent(job.id)}&company=${encodeURIComponent(job.company)}`;
  return (
    <>
      <Reveal className="job-header">
        <div className="grow">
          <p className="eyebrow"><a href="/app">All jobs</a></p>
          <h1>{job.title}</h1>
          <div className="meta">
            <span>{job.company || 'Unknown company'}</span>
            {job.location ? <>·<span>{job.location}</span></> : null}
            {job.region ? <Chip kind={regionKind(job.region)}>{job.region}</Chip> : null}
            {job.level ? <Chip kind="tag">{job.level}</Chip> : null}
            {posting ? <a href={posting} target="_blank" rel="noopener noreferrer">Open posting<Icon name="external" /></a> : null}
          </div>
        </div>
      </Reveal>
      <Reveal index={1}>{scoreBlock}</Reveal>
      <Reveal index={2}><BuildCard job={job} initial={job.my_build} /></Reveal>
      {job.company ? (
        <Reveal as="section" index={2} className="reviews-block">
          <div className="section-head"><h2>Reviews of {job.company}</h2>
            <span className="count">{reviews.length ? `${reviews.length}${reviews.length === 20 ? '+' : ''}` : 'None yet'}</span>
            <span className="spacer" />
            <a className="btn" href={reviewHref}>Write a review</a>
            {reviews.length ? <a className="btn ghost" href={`/app/reviews?company=${encodeURIComponent(job.company)}`}>See all</a> : null}
          </div>
          <ReviewList reviews={reviews.slice(0, 3)}
            onRemove={(rid) => setReviews((all) => all.filter((r) => r.id !== rid))}
            onUpdate={(review) => setReviews((all) => all.map((r) => (r.id === review.id ? review : r)))} />
        </Reveal>) : null}
      <Reveal as="section" index={2} className="panel jd-panel">
        <div className="panel-head"><h2>Job description</h2></div>
        <div className="panel-body"><Markdown text={postingBody(job.text)} /></div>
      </Reveal>
    </>
  );
}
