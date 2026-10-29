'use client';

import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useSession } from '@/components/session';
import { api, Chip, EmptyState, GAP_LABEL, Icon, Reveal, regionKind, safeUrl, ScoreBadge,
  useDebounced } from '@/components/ui';

const LEVELS = ['Intern', 'New grad', 'Junior', 'Mid or unspecified', 'Senior', 'Staff and principal',
  'Manager', 'Director and above'];

interface Job {
  id: string; title: string; company: string; location: string; region: string; level: string;
  source: string; url: string | null; posted: string | null; complete: boolean; score: number | null;
  insufficient: boolean; matched: string[]; gaps: [string, string][]; requirement_count: number | null;
  skill_requirements: number;
}
interface Profile { filename: string; mode?: string; evidence_lines?: number; error?: string }
interface Board {
  total: number; matching: number; scorable: number; jobs: Job[]; profile: Profile | null;
  facets: Record<'region' | 'level' | 'source' | 'verdict', Record<string, number>>; index_generated: string | null;
}

const EMPTY_QUERY = { q: '', region: '', levels: [] as string[], source: '', verdict: '', sort: 'match' };

function profileLine(p: Profile): string {
  if (p.error) return `Profile problem: ${p.error}`;
  return `${p.filename} · ${p.mode === 'master' ? 'master profile' : 'plain resume'} · ${p.evidence_lines} evidence lines`;
}

export default function JobsPage() {
  const { meta } = useSession();
  const threshold = meta.threshold;
  const [query, setQuery] = useState(EMPTY_QUERY);
  const [search, setSearch] = useState('');
  const [board, setBoard] = useState<Board | null>(null);
  const [rows, setRows] = useState<Job[]>([]);
  const [error, setError] = useState('');
  const loaded = useRef(0);

  const load = useCallback(async (append: boolean) => {
    const params = new URLSearchParams({ sort: query.sort, offset: String(append ? loaded.current : 0), limit: '40' });
    for (const key of ['q', 'region', 'source', 'verdict'] as const) if (query[key]) params.set(key, query[key]);
    if (query.levels.length) params.set('level', query.levels.join(','));
    try {
      const data = await api<Board>(`/api/jobs?${params}`);
      setBoard(data);
      setRows((old) => {
        const next = append ? old.concat(data.jobs) : data.jobs;
        loaded.current = next.length;
        return next;
      });
      setError('');
    } catch (err) {
      setError((err as Error).message);
    }
  }, [query]);

  useEffect(() => { load(false); }, [load]);
  const setQ = useDebounced((q: string) => setQuery((old) => ({ ...old, q })), 250);
  const toggleLevel = (name: string) => setQuery((old) => ({ ...old,
    levels: old.levels.includes(name) ? old.levels.filter((l) => l !== name) : [...old.levels, name] }));
  const update = (key: 'region' | 'source' | 'verdict' | 'sort') => (e: React.ChangeEvent<HTMLSelectElement>) =>
    setQuery((old) => ({ ...old, [key]: e.target.value }));

  const levelCounts = board?.facets.level ?? {};
  const regionCount = (name: string) => Object.entries(board?.facets.region ?? {})
    .filter(([r]) => r.includes(name)).reduce((sum, [, n]) => sum + n, 0);
  const profile = board?.profile;

  return (
    <>
      <Reveal as="header" className="page-head">
        <p className="eyebrow">VouchStudio · job board</p>
        <h1>Every open role, scored against your experience.</h1>
        <p className="lede">Browse every role and level across the US and Canada. Each job gets an
          evidence-based fit score from your profile; open one to see what it matched and what is missing.</p>
      </Reveal>
      <Reveal className="banner" index={1}>
        {profile ? (
          <>
            <p><span className="src">Scored against your profile</span>{profileLine(profile)}</p>
            <a className="btn" href="/app/me">Change profile</a>
          </>
        ) : (
          <>
            <p><span className="src">No profile yet</span>Upload your resume or master profile to see how you fit each job.</p>
            <a className="btn primary" href="/app/me">Upload your resume</a>
          </>
        )}
      </Reveal>
      {board ? (
        <div className="bento">
          <Stat value={board.total.toLocaleString()} label="Jobs on the board" cls="span-2" i={0} />
          <Stat value={board.scorable.toLocaleString()} cls="span-2" i={1}
            label={`With enough detail to score (${meta.min_skill_requirements}+ recognized skills)`} />
          {profile && !profile.error
            ? <Stat value={(board.facets.verdict.proceed || 0).toLocaleString()} cls="span-2 wide" i={2}
              label={`At or above the ${threshold} fit threshold for you`} />
            : <Stat value="—" label="Upload a profile to see how many you fit" cls="span-2 wide" i={2} />}
          <Reveal className="card span-3" index={3}>
            <h3>Regions</h3>
            <div className="split">
              <div><p className="stat-big">{regionCount('US').toLocaleString()}</p><p className="stat-label">United States</p></div>
              <div><p className="stat-big">{regionCount('Canada').toLocaleString()}</p><p className="stat-label">Canada</p></div>
            </div>
          </Reveal>
          <Reveal className="card span-3" index={4}>
            <h3>Levels</h3>
            <ul className="levels">{LEVELS.map((name) => (
              <li key={name}><button type="button" aria-pressed={query.levels.includes(name)} onClick={() => toggleLevel(name)}>
                <span>{name}</span><span>{(levelCounts[name] || 0).toLocaleString()}</span></button></li>))}
            </ul>
          </Reveal>
        </div>
      ) : null}
      <div className="section-head">
        <h2>All jobs</h2>
        <span className="count" aria-live="polite">
          {board ? `${rows.length.toLocaleString()} of ${board.matching.toLocaleString()} shown` : ''}</span>
      </div>
      <div className="toolbar">
        <input type="search" placeholder="Title, company or city" aria-label="Search jobs" value={search}
          onChange={(e) => { setSearch(e.target.value); setQ(e.target.value); }} />
        <select aria-label="Status" value={query.verdict} onChange={update('verdict')}>
          <option value="">Any status</option>
          <option value="proceed">At or above {threshold}</option>
          <option value="low_fit">Below threshold</option>
          <option value="thin">Not enough detail</option>
          <option value="listing">Listing only</option>
        </select>
        <select aria-label="Region" value={query.region} onChange={update('region')}>
          <option value="">Anywhere</option><option value="US">US</option><option value="Canada">Canada</option>
        </select>
        <select aria-label="Source" value={query.source} onChange={update('source')}>
          <option value="">All sources</option>
          {Object.entries(board?.facets.source ?? {}).map(([s, n]) => <option key={s} value={s}>{s} ({n})</option>)}
        </select>
        <select aria-label="Sort" value={query.sort} onChange={update('sort')}>
          <option value="match">Best match</option><option value="score">Fit score</option>
          <option value="recent">Newest posting</option><option value="company">Company A-Z</option>
        </select>
      </div>
      <div className="level-row" role="group" aria-label="Filter by level">
        <span className="label">Level</span>
        {LEVELS.filter((name) => levelCounts[name]).map((name) => (
          <button key={name} className="level-chip" type="button" aria-pressed={query.levels.includes(name)}
            onClick={() => toggleLevel(name)}>{name}<span className="n">{levelCounts[name]}</span></button>))}
        {query.levels.length ? <button className="btn ghost small" type="button"
          onClick={() => setQuery((old) => ({ ...old, levels: [] }))}>Clear</button> : null}
      </div>
      <p className="jobs-summary">{profile?.error ? <Chip kind="fail">Profile: {profile.error}</Chip> : null}</p>
      {error ? <div className="notice fail small">{error}</div> : null}
      <div className="panel job-list">
        {!board ? <div className="empty">Loading jobs… (the first load after an upload scores every job)</div>
          : rows.length ? rows.map((job, i) => <JobRow key={job.id} job={job} index={i % 8} threshold={threshold}
            minSkills={meta.min_skill_requirements} />)
            : <EmptyState title="No jobs match these filters" text="Clear a filter to see more." />}
      </div>
      <div className="load-more">
        {board && rows.length < board.matching ? (
          <button className="btn" type="button" onClick={() => load(true)}>
            Show more ({(board.matching - rows.length).toLocaleString()} left)</button>) : null}
      </div>
      <footer className="foot">
        <span>{board?.index_generated ? `Board published ${board.index_generated.replace('T', ' ')} · ` : ''}
          {board ? `${board.total.toLocaleString()} jobs` : ''}</span>
      </footer>
    </>
  );
}

function Stat({ value, label, cls, i }: { value: string; label: string; cls: string; i: number }) {
  return <Reveal className={`card ${cls}`} index={i}><p className="stat-big">{value}</p><p className="stat-label">{label}</p></Reveal>;
}

function JobRow({ job, index, threshold, minSkills }: { job: Job; index: number; threshold: number; minSkills: number }) {
  const router = useRouter();
  const open = () => router.push(`/app/jobs/${encodeURIComponent(job.id)}`);
  const scored = job.score != null && !job.insufficient;
  const pass = scored && (job.score ?? 0) >= threshold;
  const posting = safeUrl(job.url);
  let detail = null;
  if (scored) {
    detail = (
      <div className="detail">
        <div><h4>Evidence matched</h4><div className="chips">{job.matched.length
          ? job.matched.map((m) => <span key={m} className="chip pass" title={m}><span className="clip">{m}</span></span>)
          : <span className="faint small">None</span>}</div></div>
        <div><h4>Gaps</h4><div className="chips">{job.gaps.length
          ? job.gaps.map(([name, status]) => (
            <span key={name} className={`chip ${status === 'explicitly_no' ? 'fail' : ''}`} title={name}>
              <span className="clip">{name}</span><em>{GAP_LABEL[status] || status}</em></span>))
          : <span className="faint small">None</span>}</div></div>
      </div>
    );
  } else if (job.insufficient) {
    detail = <p className="faint small" style={{ margin: '10px 0 0' }}>
      Only {job.skill_requirements} recognized skill requirement{job.skill_requirements === 1 ? '' : 's'};
      a score needs {minSkills}. Read the posting to judge fit.</p>;
  } else if (!job.complete) {
    detail = <p className="faint small" style={{ margin: '10px 0 0' }}>
      This source lists the job without a description, so it cannot be scored.</p>;
  }
  return (
    <Reveal as="article" index={index} className="job-row" role="link" tabIndex={0} onClick={open}
      onKeyDown={(e: React.KeyboardEvent) => { if (e.key === 'Enter') open(); }}>
      <ScoreBadge job={job} threshold={threshold} />
      <div className="grow">
        <div className="title">{job.title}</div>
        <div className="sub">{[job.company || 'Unknown company', job.location, job.posted ? `posted ${job.posted}` : null]
          .filter(Boolean).join(' · ')}</div>
        <div className="tags">
          {scored ? <Chip kind={`${pass ? 'pass' : 'fail'} tag`}>{pass ? `${threshold}+` : `Below ${threshold}`}</Chip>
            : job.insufficient ? <Chip kind="tag">Not enough detail</Chip> : !job.complete ? <Chip kind="tag">Listing only</Chip> : null}
          {job.region ? <Chip kind={regionKind(job.region)}>{job.region}</Chip> : null}
          <Chip kind="tag">{job.level}</Chip><Chip kind="tag">{job.source}</Chip>
        </div>
        {detail}
      </div>
      <div className="side-col">
        {posting ? <a href={posting} target="_blank" rel="noopener noreferrer" className="small"
          onClick={(e) => e.stopPropagation()}>Posting<Icon name="external" /></a> : null}
      </div>
    </Reveal>
  );
}
