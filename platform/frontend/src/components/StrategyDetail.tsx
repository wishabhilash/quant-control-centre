import { useEffect, useState } from "react";
import { strategiesApi } from "../api/client";
import type { StrategyDocument } from "../types/strategy";
import { useApp } from "../store";
import { OverviewTab } from "./tabs/OverviewTab";
import { CodeTab } from "./tabs/CodeTab";
import { ConfigTab } from "./tabs/ConfigTab";
import { SchedulesTab } from "./tabs/SchedulesTab";
import { LogsTab } from "./tabs/LogsTab";

const TABS = ["Overview", "Code", "Configuration", "Schedules", "Logs"] as const;
type Tab = (typeof TABS)[number];

export function StrategyDetail({ id }: { id: string }) {
  const [doc, setDoc] = useState<StrategyDocument | null>(null);
  const [tab, setTab] = useState<Tab>("Overview");
  const [err, setErr] = useState<string | null>(null);
  const navigate = useApp((s) => s.navigate);
  const [busyEnabled, setBusyEnabled] = useState(false);

  // Toggling enabled changes whether the strategy's crons run, so persist it to
  // the server (which reconciles the crontab) and reload the fresh document.
  const toggleEnabled = async () => {
    if (!doc) return;
    setBusyEnabled(true);
    try {
      await strategiesApi.update(doc.strategy_id, { enabled: !doc.enabled });
      setDoc(await strategiesApi.get(id));
    } catch (e: any) {
      setErr(e?.message);
    } finally {
      setBusyEnabled(false);
    }
  };

  useEffect(() => {
    (async () => {
      try {
        setDoc(await strategiesApi.get(id));
        setErr(null);
      } catch (e: any) {
        setErr(e?.message);
      }
    })();
  }, [id]);

  if (err) return <div className="badge badge-err">{err}</div>;
  if (!doc) return <div className="text-slate-500">Loading…</div>;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <button className="text-slate-400 hover:text-white text-sm" onClick={() => navigate({ name: "strategies" })}>
            ← Strategies
          </button>
          <h1 className="text-xl font-semibold mt-1">{doc.name}</h1>
          <p className="text-sm text-slate-400 mono">{doc.strategy_id}</p>
        </div>
        <div className="flex gap-2">
          <button className="btn-ghost" title="Enable/disable this strategy's cron schedules" onClick={() => toggleEnabled()} disabled={busyEnabled}>
            {doc.enabled ? "Disable crons" : "Enable crons"}
          </button>
        </div>
      </div>

      <div className="flex gap-1 border-b border-border">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-3 py-2 text-sm border-b-2 ${
              tab === t ? "border-accent text-accent" : "border-transparent text-slate-400 hover:text-white"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === "Overview" && <OverviewTab doc={doc} onSaved={setDoc} />}
      {tab === "Code" && <CodeTab doc={doc} onSaved={setDoc} />}
      {tab === "Configuration" && <ConfigTab doc={doc} onSaved={setDoc} />}
      {tab === "Schedules" && <SchedulesTab doc={doc} onSaved={setDoc} />}
      {tab === "Logs" && <LogsTab doc={doc} />}
    </div>
  );
}
