import { useEffect, useState } from "react";
import { adminApi, copyApi, deployApi, getSession, strategiesApi } from "../api/client";
import type { StrategyListResponse, StrategySummary } from "../types/strategy";
import { useApp } from "../store";

function Badge({ status }: { status: string }) {
  const tone =
    status === "deployed" ? "ok" : status === "failed" ? "err" : status === "running" ? "ok" : "idle";
  return <span className={`badge badge-${tone}`}>{status}</span>;
}

export function StrategyList() {
  const [data, setData] = useState<StrategyListResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Server-side pagination: the strategies list is a page of the
  // caller's own strategies, most recently edited first.
  const [page, setPage] = useState(1);

  function CopyBadge({ text, tone = "slate" }: { text: string; tone?: string }) {
    const tones: Record<string, string> = {
      slate: "border-border text-slate-400",
      ok: "border-emerald-700 text-emerald-400",
      warn: "border-amber-700 text-amber-400",
    };
    return (
      <span className={`text-[10px] px-1.5 py-0.5 rounded border ${tones[tone]}`}>
        {text}
      </span>
    );
  }
  const navigate = useApp((s) => s.navigate);
  // The operator sees only strategies that belong to nobody (legacy documents);
  // assigning one to a user is an explicit, audited action.
  const isOperator = getSession()?.operator === true;
  const [unassigned, setUnassigned] = useState<StrategySummary[]>([]);
  const [mobile, setMobile] = useState<Record<string, string>>({});
  const [assignMsg, setAssignMsg] = useState<string | null>(null);

  const load = async (p: number = page) => {
    try {
      setData(await strategiesApi.list({ page: p }));
      setErr(null);
      if (isOperator) {
        try {
          setUnassigned(await adminApi.unassignedStrategies());
        } catch {
          setUnassigned([]);
        }
      }
    } catch (e: any) {
      setErr(e?.message);
    }
  };
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <h1 className="text-xl font-semibold">Strategies</h1>
        <div className="flex gap-2">
          <button className="btn-ghost" onClick={() => load()}>Refresh</button>
          <button className="btn-primary" onClick={() => navigate({ name: "new-strategy" })}>+ New strategy</button>
        </div>
      </div>

      {err && <div className="badge badge-err">{err}</div>}

      <div className="panel overflow-hidden">
        <table className="w-full text-sm">
          <thead className="text-slate-400 text-xs border-b border-border">
            <tr>
              <th className="text-left p-2">Name</th>
              <th className="text-left p-2">ID</th>
              <th className="text-left p-2">Enabled</th>
              <th className="text-left p-2">Copy</th>
              <th className="text-left p-2">Deployment</th>
              <th className="text-left p-2">Schedules</th>
              <th className="text-left p-2">Next execution</th>
              <th className="text-left p-2">Last deployed</th>
              <th className="text-right p-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {(data?.strategies ?? []).map((s) => (
              <tr key={s.strategy_id} className="border-b border-border/50 hover:bg-white/5">
                <td className="p-2">
                  <button className="text-accent hover:underline" onClick={() => navigate({ name: "strategy", id: s.strategy_id })}>
                    {s.name}
                  </button>
                </td>
                <td className="p-2 mono">{s.strategy_id}</td>
                <td className="p-2">{s.enabled ? "yes" : "no"}</td>
                <td className="p-2">
                  {/* Copy trading toggle: the leader publishes (or withdraws)
                      the strategy from the Universe. A copied strategy (badge)
                      cannot itself be offered. */}
                  {s.copied ? (
                    <CopyBadge text="copy" tone="slate" />
                  ) : (
                    <button
                      className={`text-xs ${s.copy_trading ? "text-emerald-400 hover:text-emerald-300" : "text-slate-500 hover:text-slate-300"}`}
                      title={s.copy_trading
                        ? "Offered for copy trading — click to withdraw"
                        : "Offer this strategy for copy trading (others can follow it)"}
                      disabled={busy}
                      onClick={async () => {
                        // disclosure: an author may publish a strategy they
                        // never ran themselves (they may write FOR others) —
                        // but confirm first, since followers will copy it
                        // with no track record.
                        if (!s.copy_trading && !s.last_deployed_at
                            && !window.confirm(
                              "You haven't deployed this strategy yourself — "
                              + "followers will copy it with no track record. "
                              + "Offer it for copy trading anyway?")) {
                          return;
                        }
                        setBusy(true);
                        try {
                          await copyApi.setCopyTrading(s.strategy_id, !s.copy_trading);
                          load();
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      {s.copy_trading ? "◉ copy on" : "○ copy off"}
                    </button>
                  )}
                </td>
                <td className="p-2"><Badge status={s.deployment_status} /></td>
                <td className="p-2">{s.schedule_count}</td>
                <td className="p-2 mono">{s.next_scheduled_execution ?? "—"}</td>
                <td className="p-2 mono">{s.last_deployed_at ?? "—"}</td>
                <td className="p-2 text-right whitespace-nowrap">
                  <button className="btn-ghost text-xs" onClick={async () => { await deployApi.deploy(s.strategy_id); load(); }}>Deploy</button>{" "}
                  {/* Show only the meaningful action: an enabled strategy can be
                      disabled (schedules off) and vice-versa (spec §14 start/stop). */}
                  {s.enabled ? (
                    <button className="btn-ghost text-xs" title="Disable this strategy's schedules" onClick={async () => { await deployApi.stop(s.strategy_id); load(); }}>Disable</button>
                  ) : (
                    <button className="btn-ghost text-xs" title="Enable this strategy's cron schedules" onClick={async () => { await deployApi.start(s.strategy_id); load(); }}>Enable</button>
                  )}{" "}
                  <button
                    className="btn-danger text-xs"
                    onClick={async () => {
                      if (!confirm(`Delete ${s.name}? This removes local files and the S3 document.`)) return;
                      await strategiesApi.remove(s.strategy_id);
                      load();
                    }}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
            {(data?.strategies ?? []).length === 0 && (
              <tr><td colSpan={8} className="p-4 text-center text-slate-500">No strategies yet</td></tr>
            )}
          </tbody>
        </table>
        {/* Server-side pagination: a page of the caller's own strategies,
            most recently edited first. */}
        {data && data.total > 0 && (
          <div className="flex items-center gap-3 text-xs text-slate-500">
            <button className="btn-ghost text-xs"
                    disabled={data.page <= 1}
                    onClick={() => { setPage(data.page - 1); load(data.page - 1); }}>
              ← Prev
            </button>
            <span>
              page {data.page} of {data.pages} · {data.total} strateg
              {data.total === 1 ? "y" : "ies"}
            </span>
            <button className="btn-ghost text-xs"
                    disabled={data.page >= data.pages}
                    onClick={() => { setPage(data.page + 1); load(data.page + 1); }}>
              Next →
            </button>
          </div>
        )}
      </div>

      {/* Operator: strategies that belong to nobody must be assigned to a user. */}
      {isOperator && unassigned.length > 0 && (
        <div className="panel p-3">
          <div className="text-sm font-medium mb-1">Unassigned strategies</div>
          <p className="text-xs text-slate-500 mb-2">
            These documents predate user accounts. Assign each one to a user
            (mobile number) — the owner then sees it; nobody else can.
          </p>
          {assignMsg && <div className="text-xs text-slate-400 mb-2">{assignMsg}</div>}
          <table className="w-full text-sm">
            <tbody>
              {unassigned.map((s) => (
                <tr key={s.strategy_id} className="border-b border-border/50">
                  <td className="p-2">{s.name}</td>
                  <td className="p-2 mono text-slate-400">{s.strategy_id}</td>
                  <td className="p-2">
                    <input
                      className="mono text-xs"
                      placeholder="mobile number"
                      value={mobile[s.strategy_id] ?? ""}
                      onChange={(e) =>
                        setMobile((m) => ({ ...m, [s.strategy_id]: e.target.value }))
                      }
                    />
                  </td>
                  <td className="p-2 text-right">
                    <button
                      className="btn-ghost text-xs"
                      onClick={async () => {
                        const value = (mobile[s.strategy_id] ?? "").trim();
                        if (!value) return;
                        try {
                          await adminApi.assign(s.strategy_id, value);
                          setAssignMsg(`${s.strategy_id} → ${value}`);
                          await load();
                        } catch (e: any) {
                          setAssignMsg(e?.message ?? "assignment failed");
                        }
                      }}
                    >
                      Assign
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

    </div>
  );
}
