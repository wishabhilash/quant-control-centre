import { useEffect, useState } from "react";
import { deployApi, strategiesApi } from "../api/client";
import type { StrategyListResponse } from "../types/strategy";
import { useApp } from "../store";

function Badge({ status }: { status: string }) {
  const tone =
    status === "deployed" ? "ok" : status === "failed" ? "err" : status === "running" ? "ok" : "idle";
  return <span className={`badge badge-${tone}`}>{status}</span>;
}

export function StrategyList() {
  const [data, setData] = useState<StrategyListResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const navigate = useApp((s) => s.navigate);

  const load = async () => {
    try {
      setData(await strategiesApi.list());
      setErr(null);
    } catch (e: any) {
      setErr(e?.message);
    }
  };
  useEffect(() => {
    load();
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <h1 className="text-xl font-semibold">Strategies</h1>
        <div className="flex gap-2">
          <button className="btn-ghost" onClick={load}>Refresh</button>
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
                <td className="p-2"><Badge status={s.deployment_status} /></td>
                <td className="p-2">{s.schedule_count}</td>
                <td className="p-2 mono">{s.next_scheduled_execution ?? "—"}</td>
                <td className="p-2 mono">{s.last_deployed_at ?? "—"}</td>
                <td className="p-2 text-right whitespace-nowrap">
                  <button className="btn-ghost text-xs" onClick={async () => { await deployApi.deploy(s.strategy_id); load(); }}>Deploy</button>{" "}
                  <button className="btn-ghost text-xs" title="Enable this strategy's cron schedules" onClick={async () => { await deployApi.start(s.strategy_id); load(); }}>Enable</button>{" "}
                  <button className="btn-ghost text-xs" title="Disable this strategy's cron schedules" onClick={async () => { await deployApi.stop(s.strategy_id); load(); }}>Disable</button>{" "}
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
      </div>

    </div>
  );
}
