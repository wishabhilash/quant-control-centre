import { useState } from "react";
import { deployApi, strategiesApi } from "../../api/client";
import type { StrategyDocument } from "../../types/strategy";

export function OverviewTab({ doc, onSaved }: { doc: StrategyDocument; onSaved: (d: StrategyDocument) => void }) {
  const [name, setName] = useState(doc.name);
  const [desc, setDesc] = useState(doc.description);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      const updated = await strategiesApi.update(doc.strategy_id, { name, description: desc });
      onSaved(updated);
      setMsg("Saved");
    } catch (e: any) {
      setMsg(e?.message ?? "save failed");
    } finally {
      setBusy(false);
    }
  };

  const deploy = async () => {
    setBusy(true);
    try {
      await deployApi.deploy(doc.strategy_id);
      setMsg("Deployed");
    } catch (e: any) {
      setMsg(e?.message ?? "deploy failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid grid-cols-2 gap-4">
      <div className="panel p-4 space-y-3">
        <h3 className="text-sm font-medium">Details</h3>
        <label className="block text-sm">Name
          <input className="w-full mt-1" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="block text-sm">Description
          <textarea className="w-full mt-1" rows={3} value={desc} onChange={(e) => setDesc(e.target.value)} />
        </label>
        <div className="flex gap-2">
          <button className="btn-primary" onClick={save} disabled={busy}>Save details</button>
          <button className="btn-ghost" onClick={deploy} disabled={busy}>Deploy</button>
        </div>
        {msg && <div className="badge badge-ok">{msg}</div>}
      </div>

      <div className="panel p-4 space-y-2">
        <h3 className="text-sm font-medium">Runtime</h3>
        <Row k="Deployment status" v={doc.deployment.status} />
        <Row k="Last deployed" v={doc.deployment.last_deployed_at ?? "—"} />
        <Row k="Source hash" v={(doc.deployment.source_hash ?? "—").slice(0, 16)} mono />
        <Row k="Python version" v={doc.runtime.python_version} />
        <Row k="Timeout (s)" v={String(doc.runtime.timeout_seconds)} />
        <Row k="Concurrent runs" v={doc.runtime.allow_concurrent_runs ? "yes" : "no"} />
        <Row k="Schedules" v={String(doc.schedules.length)} />
        <Row k="Enabled" v={doc.enabled ? "yes" : "no"} />
      </div>
    </div>
  );
}

function Row({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex justify-between text-sm border-b border-border/40 pb-1">
      <span className="text-slate-400">{k}</span>
      <span className={mono ? "mono" : ""}>{v}</span>
    </div>
  );
}
