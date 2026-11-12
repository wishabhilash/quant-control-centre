// System Settings page — surfaces the spec §14 system endpoints (health,
// storage connectivity, scheduler health) plus dev/prod guidance (§17).
import { useEffect, useState, type ReactNode } from "react";
import { systemApi } from "../api/client";
import type { HealthResponse, SchedulerStatus, StorageStatus } from "../types/strategy";

export function SystemSettings() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [storage, setStorage] = useState<StorageStatus | null>(null);
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [h, s, sc] = await Promise.all([
        systemApi.health(),
        systemApi.storage(),
        systemApi.scheduler(),
      ]);
      setHealth(h);
      setStorage(s);
      setScheduler(sc);
      setErr(null);
    } catch (e: any) {
      setErr(e?.message ?? "failed to load system status");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="space-y-6 max-w-4xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">System Settings</h1>
          <p className="text-sm text-slate-400">Infrastructure and storage health (spec §14 / §15).</p>
        </div>
        <button className="btn-ghost" onClick={load} disabled={loading}>Refresh</button>
      </div>

      {err && <div className="badge badge-err">{err}</div>}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Panel title="Runtime health" ok={health?.status === "ok"} loading={!health && loading}>
          <Row k="Status" v={health?.status ?? "—"} />
          <Row k="Environment" v={health?.environment ?? "—"} />
          <Row k="Version" v={health ? `v${health.version}` : "—"} />
          <Row k="Checks" v={health && Object.keys(health.checks).length ? Object.keys(health.checks).join(", ") : "—"} />
        </Panel>

        <Panel title="S3 connectivity" ok={storage ? storage.reachable : undefined} loading={!storage && loading}>
          <Row k="Reachable" v={storage ? (storage.reachable ? "yes" : "no") : "—"} />
          <Row k="Bucket" v={storage?.bucket ?? "—"} />
          <Row k="Region" v={storage?.region ?? "—"} />
          <Row k="Endpoint" v={storage && storage.endpoint_url ? storage.endpoint_url : "(default AWS)"} mono />
        </Panel>

        <Panel title="Scheduler" ok={scheduler ? scheduler.active : undefined} loading={!scheduler && loading}>
          <Row k="Mode" v={scheduler?.mode ?? "—"} />
          <Row k="Active" v={scheduler ? (scheduler.active ? "yes" : "no") : "—"} />
          <Row k="Managed schedules" v={scheduler ? String(scheduler.managed_schedule_count) : "—"} />
          <Row k="Cron file" v={scheduler?.cron_file ?? "—"} mono />
        </Panel>
      </div>

      {scheduler && scheduler.mode === "dry_run" && (
        <div className="panel p-3">
          <div className="badge badge-warn mb-2">Scheduler is in dry-run mode</div>
          <p className="text-xs text-slate-400">
            Schedules are written to the platform cron file but are NOT installed into the system crontab.
            This is the safe default for local development (spec §17). On EC2, set{" "}
            <span className="mono">SCHEDULER_DRY_RUN=false</span> to install the generated crontab.
          </p>
        </div>
      )}

      <div className="panel p-4 space-y-2">
        <h3 className="text-sm font-medium">Development &amp; recovery notes</h3>
        <p className="text-xs text-slate-400">
          S3 is the persistent source of truth. To rebuild after an EC2 failure, redeploy the platform and point
          it at the same bucket — strategy documents, venvs and cron schedules are reconstructed from S3 (spec §18).
          Live strategies do not resume automatically; broker-state reconciliation and explicit authorisation are required.
        </p>
        <p className="text-xs text-slate-400">
          Local dev uses LocalStack (<span className="mono">S3_ENDPOINT_URL=http://localhost:4566</span>) and a
          separate development bucket; it never accesses production strategy documents by default.
        </p>
      </div>
    </div>
  );
}

function Panel({
  title,
  ok,
  loading,
  children,
}: {
  title: string;
  ok?: boolean;
  loading?: boolean;
  children: ReactNode;
}) {
  const tone = ok === undefined ? "idle" : ok ? "ok" : "err";
  const label = ok === undefined ? "unknown" : ok ? "ok" : "down";
  return (
    <div className="panel p-4 space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium">{title}</div>
        {loading ? (
          <span className="text-xs text-slate-500">loading…</span>
        ) : (
          <span className={`badge badge-${tone}`}>{label}</span>
        )}
      </div>
      {children}
    </div>
  );
}

function Row({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-2 text-sm border-b border-border/40 pb-1">
      <span className="text-slate-400 shrink-0">{k}</span>
      <span className={`text-right ${mono ? "mono truncate" : ""}`} title={v}>{v}</span>
    </div>
  );
}
