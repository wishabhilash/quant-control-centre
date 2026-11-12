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
  const [creating, setCreating] = useState(false);
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
          <button className="btn-primary" onClick={() => setCreating(true)}>+ New strategy</button>
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

      {creating && <CreateStrategyModal onClose={() => setCreating(false)} onCreated={() => { setCreating(false); load(); }} />}
    </div>
  );
}

function CreateStrategyModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [entry, setEntry] = useState("main.py");
  const [capital, setCapital] = useState("100000");
  const [body, setBody] = useState('def run(config):\n    print("hello from strategy")\n');
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    try {
      await strategiesApi.create({
        name,
        description: "",
        entrypoint: entry,
        initial_capital: Number(capital),
        files: { [entry]: body },
      });
      onCreated();
    } catch (e: any) {
      setErr(e?.message);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div className="panel p-5 w-[480px]" onClick={(e) => e.stopPropagation()}>
        <h2 className="font-semibold mb-3">New strategy</h2>
        <label className="block text-sm mb-2">Name
          <input className="w-full mt-1" value={name} onChange={(e) => setName(e.target.value)} placeholder="Weekly Supertrend" />
        </label>
        <div className="grid grid-cols-2 gap-3 mb-2">
          <label className="block text-sm">Entry point
            <input className="w-full mt-1" value={entry} onChange={(e) => setEntry(e.target.value)} />
          </label>
          <label className="block text-sm">Initial capital
            <input className="w-full mt-1" value={capital} onChange={(e) => setCapital(e.target.value)} />
          </label>
        </div>
        <label className="block text-sm">Initial {entry}
          <textarea className="w-full mt-1 mono" rows={6} value={body} onChange={(e) => setBody(e.target.value)} />
        </label>
        {err && <div className="badge badge-err mt-2">{err}</div>}
        <div className="flex justify-end gap-2 mt-4">
          <button className="btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn-primary" onClick={submit} disabled={!name || !entry}>Create</button>
        </div>
      </div>
    </div>
  );
}
