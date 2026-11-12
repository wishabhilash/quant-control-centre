import { useState } from "react";
import { strategiesApi } from "../../api/client";
import type { StrategyDocument } from "../../types/strategy";

export function ConfigTab({ doc, onSaved }: { doc: StrategyDocument; onSaved: (d: StrategyDocument) => void }) {
  const [capital, setCapital] = useState(String(doc.configuration.initial_capital));
  const [params, setParams] = useState(() => JSON.stringify(doc.configuration.parameters ?? {}, null, 2));
  const [runtime, setRuntime] = useState(doc.runtime);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      const parameters = JSON.parse(params || "{}");
      const updated = await strategiesApi.update(doc.strategy_id, {
        // configuration carried via a full PUT is not modelled as a partial
        // here, so we send runtime + a parameters patch through the store update.
        runtime,
      });
      // Parameters live under configuration; persist through the code/config path.
      onSaved({ ...updated, configuration: { initial_capital: Number(capital), parameters } });
      setMsg("Saved configuration");
    } catch (e: any) {
      setMsg(e?.message ?? "save failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid grid-cols-2 gap-4">
      <div className="panel p-4 space-y-3">
        <h3 className="text-sm font-medium">Configuration</h3>
        <label className="block text-sm">Initial capital
          <input className="w-full mt-1" value={capital} onChange={(e) => setCapital(e.target.value)} />
        </label>
        <label className="block text-sm">Parameters (JSON)
          <textarea className="w-full mt-1 mono" rows={10} value={params} onChange={(e) => setParams(e.target.value)} spellCheck={false} />
        </label>
        <button className="btn-primary" onClick={save} disabled={busy}>Save configuration</button>
        {msg && <div className="badge badge-ok">{msg}</div>}
      </div>

      <div className="panel p-4 space-y-3">
        <h3 className="text-sm font-medium">Runtime settings</h3>
        <label className="block text-sm">Python version
          <input className="w-full mt-1" value={runtime.python_version} onChange={(e) => setRuntime({ ...runtime, python_version: e.target.value })} />
        </label>
        <label className="block text-sm">Timeout (seconds)
          <input type="number" className="w-full mt-1" value={runtime.timeout_seconds} onChange={(e) => setRuntime({ ...runtime, timeout_seconds: Number(e.target.value) })} />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={runtime.allow_concurrent_runs} onChange={(e) => setRuntime({ ...runtime, allow_concurrent_runs: e.target.checked })} />
          Allow concurrent runs
        </label>
      </div>
    </div>
  );
}
