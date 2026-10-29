'use client';

import { useEffect, useState } from 'react';
import { useSession } from '@/components/session';
import { api, Chip, EmptyState, Reveal } from '@/components/ui';

interface Overview {
  users: { id: number; email: string; name: string; is_admin: boolean; plan: string; created_at: string | null;
    has_profile: boolean; builds: number; pending_request: boolean }[];
  requests: { id: number; email: string; name: string; note: string; status: string; created_at: string | null;
    decided_at: string | null }[];
  pending: number;
}

const when = (iso: string | null) => (iso ? iso.replace('T', ' ').slice(0, 16) : '');

export default function AdminPage() {
  const { user, toast } = useSession();
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (user.is_admin) api<Overview>('/api/admin/overview').then(setData).catch((err) => setError(err.message));
  }, [user.is_admin]);
  if (!user.is_admin) return <EmptyState title="Admins only" text="This page manages accounts and Pro requests." />;
  const act = async (path: string, body?: unknown) => {
    try { setData(await api(path, { method: 'POST', body })); } catch (err) { toast((err as Error).message, 'fail'); }
  };
  const pending = data?.requests.filter((r) => r.status === 'pending') ?? [];
  const past = data?.requests.filter((r) => r.status !== 'pending') ?? [];
  return (
    <>
      <Reveal as="header" className="page-head">
        <p className="eyebrow">VouchStudio · admin</p>
        <h1>Accounts and Pro requests.</h1>
        <p className="lede">Approve a request to switch that account from Basic to Pro, or change any plan directly.
          Queued Pro builds run on your machine with <code>./run_ui.sh --process-builds</code>.</p>
      </Reveal>
      {error ? <EmptyState title="Could not load the console" text={error} /> : null}
      {data ? (
        <div className="stack">
          <section>
            <div className="section-head"><h2>Pro requests</h2><span className="count">{pending.length} waiting</span></div>
            {pending.length ? (
              <div className="panel">{pending.map((r) => (
                <div key={r.id} className="admin-row">
                  <div className="grow"><div className="title">{r.name || r.email}</div>
                    <div className="sub">{r.email} · asked {when(r.created_at)}</div>
                    {r.note ? <blockquote className="quote">{r.note}</blockquote> : null}</div>
                  <div className="row">
                    <button className="btn primary" type="button" onClick={() => act(`/api/admin/requests/${r.id}/approve`)}>Approve: switch to Pro</button>
                    <button className="btn ghost" type="button" onClick={() => act(`/api/admin/requests/${r.id}/decline`)}>Decline</button>
                  </div>
                </div>))}
              </div>) : <p className="muted">No requests waiting.</p>}
          </section>
          <section>
            <div className="section-head"><h2>Accounts</h2><span className="count">{data.users.length} total</span></div>
            <div className="panel"><table className="data">
              <thead><tr>{['Account', 'Plan', 'Profile', 'Builds', 'Joined'].map((t) => <th key={t}>{t}</th>)}</tr></thead>
              <tbody>{data.users.map((u) => (
                <tr key={u.id}>
                  <td><b>{u.name || u.email}</b><div className="faint small">{u.email}</div></td>
                  <td>{u.is_admin ? <Chip kind="tag accent">Admin</Chip> : (
                    <select aria-label={`Plan for ${u.email}`} value={u.plan}
                      onChange={(e) => act(`/api/admin/users/${u.id}/plan`, { plan: e.target.value })}>
                      <option value="basic">Basic</option><option value="pro">Pro</option></select>)}
                    {u.pending_request ? <Chip kind="tag warn">Requested Pro</Chip> : null}</td>
                  <td>{u.has_profile ? <Chip kind="tag pass">Uploaded</Chip> : <span className="faint">None</span>}</td>
                  <td>{u.builds}</td>
                  <td className="nowrap faint small">{when(u.created_at)}</td>
                </tr>))}
              </tbody>
            </table></div>
          </section>
          {past.length ? (
            <details className="more"><summary>Past requests ({past.length})</summary>
              <ul className="small muted">{past.map((r) => <li key={r.id}>{r.email}: {r.status} {when(r.decided_at)}</li>)}</ul>
            </details>) : null}
          <p className="faint small">Accounts are stored in Neon (Postgres); files in Cloudflare R2.</p>
        </div>) : null}
    </>
  );
}
