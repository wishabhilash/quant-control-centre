import { useEffect, useState } from "react";
import { strategiesApi, systemApi, deployApi } from "../api/client";
import type { HealthResponse, SchedulerStatus, StorageStatus, StrategyListResponse } from "../types/strategy";
import { useApp } from "../store";

function StatCard({ label, value, tone = "idle" }: { label: string; value: number | string; tone?: string }) {
  return (
    <div className="panel p-4">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="text-2xl font-semibold mt-1">
        <span className={`badge badge-${tone}`}>{value}</span>
      </div>
    </div>
  );
}

export function Dashboard() {
  const [list, setList] = useState<StrategyListResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [storage, setStorage] = useState<StorageStatus | null>(null);
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [running, setRunning] = useState(0);
  const navigate = useApp((s) => s.navigate);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [l, h, s, sc] = await Promise.all([
          strategiesApi.list(),
          systemApi.health(),
          systemApi.storage(),
          systemApi.scheduler(),
        ]);
        setList(l);
        setHealth(h);
        setStorage(s);
        setScheduler(sc);
        // Count running strategies via status probes.
        let count = 0;
        for (const st of l.strategies) {
          try {
            const status = await deployApi.status(st.strategy_id);
            if (status.state === "running") count++;
          } catch {
            /* ignore */
          }
        }
        setRunning(count);
      } catch (e: any) {
        setErr(e?.message ?? "failed to load dashboard");
      }
    })();
  }, []);

  const total = list?.count ?? 0;
  const deployed = list?.strategies.filter((s) => s.deployment_status === "deployed").length ?? 0;
  const stopped = total - deployed - running;
  const failed = list?.strategies.filter((s) => s.deployment_status === "failed").length ?? 0;
  const upcoming = (list?.strategies ?? [])
    .map((s) => ({ id: s.strategy_id, name: s.name, when: s.next_scheduled_execution }))
    .filter((x) => x.when)
    .sort((a, b) => (a.when! < b.when! ? -1 : 1))
    .slice(0, 8);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Dashboard</h1>
        <p className="text-sm text-slate-400">
          Platform: <span className="mono">{health?.environment ?? "—"}</span> ·
          v{health?.version ?? "—"}
        </p>
      </div>

      {err && <div className="badge badge-err">{err}</div>}

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
        <StatCard label="Total strategies" value={total} tone="idle" />
        <StatCard label="Deployed" value={deployed} tone="ok" />
        <StatCard label="Running" value={running} tone="ok" />
        <StatCard label="Stopped" value={stopped} tone="idle" />
        <StatCard label="Failed" value={failed} tone={failed ? "err" : "idle"} />
        <StatCard label="Upcoming triggers" value={upcoming.length} tone="warn" />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="panel p-4">
          <div className="text-sm font-medium mb-2">EC2 / Runtime health</div>
          <div className={`badge ${health?.status === "ok" ? "badge-ok" : "badge-err"}`}>
            {health?.status ?? "unknown"}
          </div>
        </div>
        <div className="panel p-4">
          <div className="text-sm font-medium mb-2">S3 connectivity</div>
          <div className={`badge ${storage?.reachable ? "badge-ok" : "badge-err"}`}>
            {storage ? (storage.reachable ? "reachable" : "unreachable") : "unknown"}
          </div>
          {storage && <div className="mono mt-2 text-slate-400">{storage.bucket} · {storage.region}</div>}
        </div>
        <div className="panel p-4">
          <div className="text-sm font-medium mb-2">Scheduler</div>
          <div className={`badge ${scheduler?.active ? "badge-ok" : "badge-warn"}`}>
            {scheduler ? scheduler.mode : "unknown"}
          </div>
          {scheduler && <div className="mono mt-2 text-slate-400">{scheduler.managed_schedule_count} managed</div>}
        </div>
      </div>

      <div className="panel p-4">
        <div className="text-sm font-medium mb-2">Upcoming scheduled triggers</div>
        {upcoming.length === 0 ? (
          <div className="text-sm text-slate-500">No upcoming triggers</div>
        ) : (
          <ul className="space-y-1">
            {upcoming.map((u) => (
              <li
                key={u.id}
                className="flex justify-between items-center text-sm cursor-pointer hover:bg-white/5 px-2 py-1 rounded"
                onClick={() => navigate({ name: "strategy", id: u.id })}
              >
                <span>{u.name}</span>
                <span className="mono text-slate-400">{u.when}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
