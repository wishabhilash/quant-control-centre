// Schedules tab — manage a strategy's independent Linux cron schedules
// (spec sections 11, 15). Each cron entry invokes the generic runner with a
// unique --schedule-id. Unlimited schedules are supported (subject to limits).
import { useEffect, useState } from "react";
import { schedulesApi, strategiesApi } from "../../api/client";
import type { Schedule, StrategyDocument } from "../../types/strategy";

const DEFAULT_TZ = "Asia/Kolkata";
const DEFAULT_CRON = "20 9 * * 1-5";

type ScheduleForm = Omit<Schedule, "schedule_id">;

export function SchedulesTab({ doc, onSaved }: { doc: StrategyDocument; onSaved: (d: StrategyDocument) => void }) {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [editing, setEditing] = useState<Schedule | null>(null);

  const load = async () => {
    try {
      const res = await schedulesApi.list(doc.strategy_id);
      setSchedules(res.schedules);
      setErr(null);
    } catch (e: any) {
      setErr(e?.message ?? "failed to load schedules");
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc.strategy_id]);

  // After any mutation, refresh the whole document so other tabs stay in sync.
  const refreshDoc = async () => {
    try {
      onSaved(await strategiesApi.get(doc.strategy_id));
    } catch {
      /* keep the locally-mutated list on transient refresh errors */
    }
  };

  const toggleEnabled = async (s: Schedule) => {
    setBusy(true);
    try {
      if (s.enabled) await schedulesApi.disable(doc.strategy_id, s.schedule_id);
      else await schedulesApi.enable(doc.strategy_id, s.schedule_id);
      await Promise.all([load(), refreshDoc()]);
      setMsg(`${s.schedule_id} ${s.enabled ? "disabled" : "enabled"}`);
    } catch (e: any) {
      setErr(e?.message ?? "failed to update schedule");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (s: Schedule) => {
    if (!confirm(`Delete schedule ${s.schedule_id} (${s.name})?`)) return;
    setBusy(true);
    try {
      await schedulesApi.remove(doc.strategy_id, s.schedule_id);
      await Promise.all([load(), refreshDoc()]);
      setMsg(`Deleted ${s.schedule_id}`);
    } catch (e: any) {
      setErr(e?.message ?? "failed to delete schedule");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium">Schedules · Linux cron</h3>
        <button className="btn-ghost text-xs" onClick={() => setShowCreate(true)}>+ New schedule</button>
      </div>
      <p className="text-xs text-slate-500">
        Each enabled schedule installs a cron line that runs the generic runner with{" "}
        <span className="mono">--schedule-id</span> set to that schedule&#39;s ID. Default timezone{" "}
        <span className="mono">{DEFAULT_TZ}</span>.
      </p>

      {err && <div className="badge badge-err">{err}</div>}
      {msg && <div className="badge badge-ok">{msg}</div>}

      <div className="overflow-hidden border border-border rounded">
        <table className="w-full text-sm">
          <thead className="text-slate-400 text-xs border-b border-border bg-black/20">
            <tr>
              <th className="text-left p-2">Name</th>
              <th className="text-left p-2">ID</th>
              <th className="text-left p-2">Cron expression</th>
              <th className="text-left p-2">Timezone</th>
              <th className="text-left p-2">Enabled</th>
              <th className="text-right p-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {schedules.map((s) => (
              <tr key={s.schedule_id} className="border-b border-border/50 hover:bg-white/5">
                <td className="p-2">{s.name}</td>
                <td className="p-2 mono">{s.schedule_id}</td>
                <td className="p-2 mono">{s.cron_expression}</td>
                <td className="p-2 mono">{s.timezone}</td>
                <td className="p-2">
                  <span className={`badge ${s.enabled ? "badge-ok" : "badge-idle"}`}>
                    {s.enabled ? "enabled" : "disabled"}
                  </span>
                </td>
                <td className="p-2 text-right whitespace-nowrap">
                  <button className="btn-ghost text-xs" onClick={() => toggleEnabled(s)} disabled={busy}>
                    {s.enabled ? "Disable" : "Enable"}
                  </button>{" "}
                  <button className="btn-ghost text-xs" onClick={() => setEditing(s)} disabled={busy}>Edit</button>{" "}
                  <button className="btn-danger text-xs" onClick={() => remove(s)} disabled={busy}>Delete</button>
                </td>
              </tr>
            ))}
            {schedules.length === 0 && (
              <tr>
                <td colSpan={6} className="p-4 text-center text-slate-500">
                  No schedules yet. Create one above.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {showCreate && (
        <ScheduleModal
          title="New schedule"
          initial={{ name: "", cron_expression: DEFAULT_CRON, timezone: DEFAULT_TZ, enabled: true }}
          busy={busy}
          onCancel={() => setShowCreate(false)}
          onSubmit={async (body) => {
            setBusy(true);
            try {
              await schedulesApi.create(doc.strategy_id, body);
              setShowCreate(false);
              await Promise.all([load(), refreshDoc()]);
              setMsg("Schedule created");
            } catch (e: any) {
              setErr(e?.message ?? "failed to create schedule");
            } finally {
              setBusy(false);
            }
          }}
        />
      )}

      {editing && (
        <ScheduleModal
          title={`Edit ${editing.schedule_id}`}
          initial={{
            name: editing.name,
            cron_expression: editing.cron_expression,
            timezone: editing.timezone,
            enabled: editing.enabled,
          }}
          busy={busy}
          onCancel={() => setEditing(null)}
          onSubmit={async (body) => {
            setBusy(true);
            try {
              await schedulesApi.update(doc.strategy_id, editing.schedule_id, body);
              setEditing(null);
              await Promise.all([load(), refreshDoc()]);
              setMsg("Schedule updated");
            } catch (e: any) {
              setErr(e?.message ?? "failed to update schedule");
            } finally {
              setBusy(false);
            }
          }}
        />
      )}
    </div>
  );
}

function ScheduleModal({
  title,
  initial,
  busy,
  onCancel,
  onSubmit,
}: {
  title: string;
  initial: ScheduleForm;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (body: ScheduleForm) => void;
}) {
  const [name, setName] = useState(initial.name);
  const [cron, setCron] = useState(initial.cron_expression);
  const [tz, setTz] = useState(initial.timezone);
  const [enabled, setEnabled] = useState(initial.enabled);
  const [localErr, setLocalErr] = useState<string | null>(null);

  const submit = () => {
    if (!name.trim() || !cron.trim()) {
      setLocalErr("Name and cron expression are required");
      return;
    }
    onSubmit({ name: name.trim(), cron_expression: cron.trim(), timezone: tz.trim() || DEFAULT_TZ, enabled });
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onCancel}>
      <div className="panel p-5 w-[440px]" onClick={(e) => e.stopPropagation()}>
        <h2 className="font-semibold mb-3">{title}</h2>
        <label className="block text-sm mb-2">Name
          <input className="w-full mt-1" value={name} onChange={(e) => setName(e.target.value)} placeholder="Morning execution" />
        </label>
        <label className="block text-sm mb-2">Cron expression (5 or 6 fields)
          <input className="w-full mt-1 mono" value={cron} onChange={(e) => setCron(e.target.value)} placeholder="20 9 * * 1-5" />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block text-sm">Timezone
            <input className="w-full mt-1 mono" value={tz} onChange={(e) => setTz(e.target.value)} placeholder="Asia/Kolkata" />
          </label>
          <label className="flex items-center gap-2 text-sm mt-7">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            Enabled
          </label>
        </div>
        {localErr && <div className="badge badge-err mt-3">{localErr}</div>}
        <div className="flex justify-end gap-2 mt-4">
          <button className="btn-ghost" onClick={onCancel}>Cancel</button>
          <button className="btn-primary" onClick={submit} disabled={busy}>Save</button>
        </div>
      </div>
    </div>
  );
}
