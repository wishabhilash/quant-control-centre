// Full-page, IDE-style strategy development workspace (spec 7, 8, 11, 14).
//
// Layout: left = Monaco project file explorer, centre = Monaco editor with
// open-file tabs, right = independent Linux-cron schedule management.
//
// The project tree is entirely user-defined (no fixed main.py structure, no
// helper files). "Save" persists the whole tree, configuration and schedules
// into the strategy's single S3 JSON document. "Deploy" deploys the saved
// project. Only cron triggers run a strategy; the platform never launches a
// process itself.
import { useMemo, useState } from "react";
import { codeApi, deployApi, schedulesApi, strategiesApi } from "../api/client";
import { useApp } from "../store";
import {
  filesToTree,
  getContent,
  languageFor,
  treeToFiles,
} from "../lib/project";
import type {
  DeploymentStatus,
  RuntimeSettings,
  TreeNode,
} from "../types/strategy";
import { FileExplorer } from "./FileExplorer";
import { MonacoEditor } from "./MonacoEditor";

type LocalSchedule = {
  key: string;
  serverId?: string;
  name: string;
  cron_expression: string;
  timezone: string;
  enabled: boolean;
};

const DEFAULT_TZ = "Asia/Kolkata";
const DEFAULT_CRON = "20 9 * * 1-5";

// A single starter entry file — freely rename, replace or delete. It does NOT
// impose a project structure on the user.
const STARTER_FILES: Record<string, string> = {
  "main.py":
    'def run(config):\n' +
    '    # Replace this starter entry point with your strategy logic.\n' +
    '    print("strategy started")\n',
};

function parseParams(text: string): Record<string, unknown> {
  const t = text.trim();
  if (!t) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(t);
  } catch (e) {
    throw new Error("Parameters must be valid JSON: " + (e as Error).message);
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Parameters must be a JSON object");
  }
  return parsed as Record<string, unknown>;
}

export function StrategyWorkspace() {
  const navigate = useApp((s) => s.navigate);

  // --- header / meta ------------------------------------------------------
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [entrypoint, setEntrypoint] = useState("main.py");
  const [capital, setCapital] = useState("100000");
  const [paramsText, setParamsText] = useState("{}");
  const [runtime, setRuntime] = useState<RuntimeSettings>({
    python_version: "3.12",
    timeout_seconds: 3600,
    allow_concurrent_runs: false,
  });

  // --- editor -------------------------------------------------------------
  const [tree, setTree] = useState<TreeNode>(() => filesToTree(STARTER_FILES));
  const [openTabs, setOpenTabs] = useState<string[]>(["main.py"]);
  const [activeTab, setActiveTab] = useState<string | null>("main.py");
  const [baseline, setBaseline] = useState<Record<string, string>>({});

  // --- schedules ----------------------------------------------------------
  const [schedules, setSchedules] = useState<LocalSchedule[]>([]);

  // --- persistence state --------------------------------------------------
  const [strategyId, setStrategyId] = useState<string | null>(null);
  const [deployment, setDeployment] = useState<DeploymentStatus>({
    status: "not_deployed",
    last_deployed_at: null,
    source_hash: null,
  });
  const [dirty, setDirty] = useState(true); // a new project has nothing saved yet
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [deployMsg, setDeployMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showCancel, setShowCancel] = useState(false);
  const [showConfig, setShowConfig] = useState(false);

  // Any local edit marks the workspace unsaved and clears the last status.
  const markDirty = () => {
    setDirty(true);
    setSaveState("idle");
    setSaveMsg(null);
    setDeployMsg(null);
  };

  const activeContent = activeTab ? getContent(tree, activeTab) ?? "" : "";
  const activeLang = activeTab ? languageFor(activeTab) : "plaintext";
  const isTabDirty = (path: string): boolean =>
    (getContent(tree, path) ?? "") !== (baseline[path] ?? "");

  const pyFiles = useMemo(() => {
    const out: string[] = [];
    const walk = (n: TreeNode): void => {
      if (n.type === "file" && n.name.endsWith(".py")) out.push(n.path);
      n.children?.forEach(walk);
    };
    walk(tree);
    return out.sort();
  }, [tree]);
  const entryOptions = pyFiles.includes(entrypoint) ? pyFiles : [entrypoint, ...pyFiles];

  // --- editor content -----------------------------------------------------
  const setActiveContent = (content: string) => {
    if (!activeTab) return;
    setTree((t) => {
      const walk = (n: TreeNode): void => {
        if (n.file?.path === activeTab) n.file.content = content;
        n.children?.forEach(walk);
      };
      walk(t);
      return { ...t };
    });
    markDirty();
  };

  // --- file explorer operations ------------------------------------------
  // FileExplorer calls onChange for every mutation (create/rename/delete of
  // files AND folders), so marking dirty in onChange covers folder creation too.
  const handleFileCreated = (path: string) => {
    if (!openTabs.includes(path)) setOpenTabs([...openTabs, path]);
    setActiveTab(path);
  };
  const handleFileRenamed = (oldPath: string, newPath: string) => {
    setOpenTabs(openTabs.map((p) => (p === oldPath ? newPath : p)));
    if (activeTab === oldPath) setActiveTab(newPath);
    setBaseline((b) => {
      const next = { ...b };
      if (oldPath in next) {
        next[newPath] = next[oldPath];
        delete next[oldPath];
      }
      return next;
    });
  };
  const handleFileDeleted = (path: string) => {
    const next = openTabs.filter((p) => p !== path);
    setOpenTabs(next);
    if (activeTab === path) setActiveTab(next[next.length - 1] ?? null);
    setBaseline((b) => {
      if (!(path in b)) return b;
      const next = { ...b };
      delete next[path];
      return next;
    });
  };
  const closeTab = (path: string) => {
    const next = openTabs.filter((p) => p !== path);
    setOpenTabs(next);
    if (activeTab === path) setActiveTab(next[next.length - 1] ?? null);
  };

  // --- schedules ----------------------------------------------------------
  const addSchedule = () => {
    setSchedules((list) => [
      ...list,
      {
        key: `s-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        name: "",
        cron_expression: DEFAULT_CRON,
        timezone: DEFAULT_TZ,
        enabled: true,
      },
    ]);
    markDirty();
  };
  const updateSchedule = (key: string, patch: Partial<LocalSchedule>) => {
    setSchedules((list) => list.map((s) => (s.key === key ? { ...s, ...patch } : s)));
    markDirty();
  };
  const removeSchedule = (key: string) => {
    setSchedules((list) => list.filter((s) => s.key !== key));
    markDirty();
  };

  // --- persistence --------------------------------------------------------
  const reconcileSchedules = async (sid: string, local: LocalSchedule[]) => {
    const { schedules: server } = await schedulesApi.list(sid);
    const keep = new Set(local.filter((s) => s.serverId).map((s) => s.serverId as string));
    // Drop server schedules that no longer exist locally.
    for (const s of server) {
      if (!keep.has(s.schedule_id)) await schedulesApi.remove(sid, s.schedule_id);
    }
    // Create or update every local schedule.
    for (const s of local) {
      const body = {
        name: s.name.trim() || "schedule",
        cron_expression: s.cron_expression.trim(),
        timezone: s.timezone.trim() || DEFAULT_TZ,
        enabled: s.enabled,
      };
      if (s.serverId) await schedulesApi.update(sid, s.serverId, body);
      else await schedulesApi.create(sid, body);
    }
  };

  const save = async (): Promise<string | null> => {
    setBusy(true);
    setSaveState("saving");
    setSaveMsg(null);
    setDeployMsg(null);
    try {
      const files = treeToFiles(tree);
      if (!files[entrypoint]) {
        throw new Error(`Entry point "${entrypoint}" is not in the project tree`);
      }
      const parameters = parseParams(paramsText);
      const initialCapital = capital.trim() === "" ? 0 : Number(capital);
      if (Number.isNaN(initialCapital)) throw new Error("Initial capital must be a number");

      let sid = strategyId;
      if (!sid) {
        const created = await strategiesApi.create({
          name: name.trim() || "Untitled",
          description,
          enabled,
          entrypoint,
          initial_capital: initialCapital,
          parameters,
          runtime,
          files,
        });
        sid = created.strategy_id;
        setStrategyId(sid);
      } else {
        await codeApi.save(sid, entrypoint, files);
        await strategiesApi.update(sid, {
          name: name.trim() || "Untitled",
          description,
          enabled,
          runtime,
          configuration: { initial_capital: initialCapital, parameters },
        });
      }

      await reconcileSchedules(sid, schedules);

      const doc = await strategiesApi.get(sid);
      setSchedules(doc.schedules.map((s) => ({ ...s, key: s.schedule_id, serverId: s.schedule_id })));
      setDeployment(doc.deployment);
      setBaseline(files);
      setDirty(false);
      setSaveState("saved");
      setSaveMsg(
        `Saved ${Object.keys(files).length} file(s), ${doc.schedules.length} schedule(s) → ${sid}`,
      );
      return sid;
    } catch (e) {
      setSaveState("error");
      setSaveMsg((e as Error).message ?? "Save failed");
      return null;
    } finally {
      setBusy(false);
    }
  };

  const deploy = async () => {
    setDeployMsg(null);
    const sid = dirty || !strategyId ? await save() : strategyId;
    if (!sid) {
      setDeployMsg("Deploy skipped: save failed");
      return;
    }
    setBusy(true);
    setDeployMsg("Deploying…");
    try {
      await deployApi.deploy(sid, false);
      const doc = await strategiesApi.get(sid);
      setDeployment(doc.deployment);
      setDeployMsg("Deployed");
    } catch (e) {
      setSaveState("error");
      setDeployMsg((e as Error).message ?? "Deploy failed");
    } finally {
      setBusy(false);
    }
  };

  // Cancel: never save or deploy. Prompt only when there are unsaved changes.
  const requestCancel = () => {
    if (dirty) {
      setShowCancel(true);
      return;
    }
    navigate({ name: "strategies" });
  };
  const discardChanges = () => {
    setShowCancel(false);
    navigate({ name: "strategies" });
  };

  return (
    <div className="flex-1 flex flex-col min-w-0">
      {/* Header */}
      <header className="flex items-center gap-3 px-4 py-2 border-b border-border bg-panel/40 shrink-0">
        <button className="btn-ghost text-xs" onClick={requestCancel} title="Return to the strategy list">
          ← Cancel
        </button>
        <input
          className="text-sm font-semibold bg-transparent border-b border-transparent focus:border-accent outline-none flex-1 min-w-0 max-w-xs"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            markDirty();
          }}
          placeholder="Strategy name"
          aria-label="Strategy name"
        />
        <div className="flex items-center gap-2 text-xs shrink-0">
          <span className="text-slate-400">Entry point</span>
          <select
            className="text-xs"
            value={entrypoint}
            onChange={(e) => {
              setEntrypoint(e.target.value);
              markDirty();
            }}
            title="Python entry point"
          >
            {entryOptions.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>

        <div className="ml-auto flex items-center gap-2 shrink-0">
          <StatusBadge saveState={saveState} dirty={dirty} />
          <DeploymentBadge deployment={deployment} deployMsg={deployMsg} />
          <button className="btn-ghost text-xs" onClick={deploy} disabled={busy} title="Save the project, then deploy every saved file">
            {deployMsg === "Deploying…" ? "Deploying…" : "Deploy"}
          </button>
          <button className="btn-primary text-xs" onClick={save} disabled={busy} title="Persist tree, configuration and schedules">
            {saveState === "saving" ? "Saving…" : "Save"}
          </button>
        </div>
      </header>

      {/* Compact, collapsible configuration */}
      <div className="border-b border-border shrink-0">
        <button
          className="w-full flex items-center gap-2 px-4 py-1.5 text-xs text-slate-400 hover:bg-white/5"
          onClick={() => setShowConfig((v) => !v)}
        >
          <span>{showConfig ? "▾" : "▸"}</span>
          <span>Configuration</span>
          <span className="ml-auto mono text-slate-500">
            capital {capital || "0"} · {runtime.python_version} · {runtime.timeout_seconds}s · {enabled ? "enabled" : "disabled"}
          </span>
        </button>
        {showConfig && (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 px-4 py-3 text-xs">
            <label className="block">
              Initial capital
              <input className="w-full mt-1" value={capital} onChange={(e) => { setCapital(e.target.value); markDirty(); }} />
            </label>
            <label className="block">
              Runtime timeout (s)
              <input type="number" className="w-full mt-1" value={runtime.timeout_seconds}
                onChange={(e) => { setRuntime({ ...runtime, timeout_seconds: Number(e.target.value) }); markDirty(); }} />
            </label>
            <label className="block">
              Python version
              <input className="w-full mt-1" value={runtime.python_version}
                onChange={(e) => { setRuntime({ ...runtime, python_version: e.target.value }); markDirty(); }} />
            </label>
            <label className="block md:col-span-3">
              Parameters (JSON object)
              <textarea className="w-full mt-1 mono" rows={4} spellCheck={false} value={paramsText}
                onChange={(e) => { setParamsText(e.target.value); markDirty(); }} />
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={enabled} onChange={(e) => { setEnabled(e.target.checked); markDirty(); }} />
              Enabled (install crons on save)
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={runtime.allow_concurrent_runs}
                onChange={(e) => { setRuntime({ ...runtime, allow_concurrent_runs: e.target.checked }); markDirty(); }} />
              Allow concurrent runs
            </label>
            <label className="block md:col-span-3">
              Description
              <input className="w-full mt-1" value={description} onChange={(e) => { setDescription(e.target.value); markDirty(); }} />
            </label>
          </div>
        )}
      </div>

      {/* Three-panel IDE body */}
      <div className="flex-1 flex min-h-0">
        {/* Left: file explorer */}
        <div className="w-60 shrink-0 border-r border-border bg-panel/30">
          <FileExplorer
            root={tree}
            onChange={(t) => { setTree(t); markDirty(); }}
            openPath={activeTab}
            onOpen={(p) => setActiveTab(p)}
            onFileCreated={handleFileCreated}
            onFileRenamed={handleFileRenamed}
            onFileDeleted={handleFileDeleted}
          />
        </div>

        {/* Centre: editor with open-file tabs */}
        <div className="flex-1 flex flex-col min-w-0">
          <div className="flex items-center border-b border-border bg-panel/30 text-xs overflow-x-auto shrink-0">
            {openTabs.length === 0 ? (
              <div className="px-3 py-1.5 text-slate-500">No open files</div>
            ) : (
              openTabs.map((p) => (
                <div
                  key={p}
                  className={`flex items-center gap-1 px-3 py-1.5 border-r border-border cursor-pointer whitespace-nowrap ${
                    activeTab === p ? "bg-accent/15 text-accent" : "text-slate-400 hover:bg-white/5"
                  }`}
                  onClick={() => setActiveTab(p)}
                >
                  <span>{isTabDirty(p) ? "● " : ""}{p.split("/").pop()}</span>
                  <button
                    className="text-slate-500 hover:text-white"
                    title="Close tab"
                    onClick={(e) => {
                      e.stopPropagation();
                      closeTab(p);
                    }}
                  >
                    ×
                  </button>
                </div>
              ))
            )}
          </div>

          <div className="flex-1 min-h-0">
            {activeTab ? (
              <MonacoEditor path={activeTab} language={activeLang} value={activeContent} onChange={setActiveContent} />
            ) : (
              <div className="p-6 text-slate-500 text-sm">Select or create a file to start editing.</div>
            )}
          </div>

          <div className="border-t border-border px-3 py-1 text-xs text-slate-500 flex items-center gap-3 shrink-0">
            <span className="mono">{strategyId ?? "not saved"}</span>
            <span>·</span>
            <span>{Object.keys(treeToFiles(tree)).length} file(s)</span>
            <span>·</span>
            <span>{schedules.length} schedule(s)</span>
            <span className="ml-auto">
              {dirty ? <span className="text-amber-400">● unsaved changes</span> : <span className="text-emerald-400">saved</span>}
            </span>
          </div>
        </div>

        {/* Right: schedule management */}
        <div className="w-80 shrink-0 border-l border-border bg-panel/30">
          <SchedulePanel schedules={schedules} onAdd={addSchedule} onUpdate={updateSchedule} onRemove={removeSchedule} />
        </div>
      </div>

      {/* Save / deploy status line */}
      {(saveMsg || deployMsg) && (
        <div className="border-t border-border px-4 py-1.5 text-xs shrink-0">
          <span className={saveState === "error" ? "text-red-400" : "text-slate-300"}>
            {saveMsg || deployMsg}
          </span>
        </div>
      )}

      {/* Cancel confirmation */}
      {showCancel && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => setShowCancel(false)}>
          <div className="panel p-5 w-[420px]" onClick={(e) => e.stopPropagation()}>
            <h2 className="font-semibold mb-2">Unsaved changes</h2>
            <p className="text-sm text-slate-400 mb-4">
              This strategy has unsaved changes. Discarding will return to the list without saving or deploying.
            </p>
            <div className="flex justify-end gap-2">
              <button className="btn-ghost text-xs" onClick={() => setShowCancel(false)}>Keep editing</button>
              <button className="btn-danger text-xs" onClick={discardChanges}>Discard changes</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// --- sub-components -------------------------------------------------------

function SchedulePanel({
  schedules,
  onAdd,
  onUpdate,
  onRemove,
}: {
  schedules: LocalSchedule[];
  onAdd: () => void;
  onUpdate: (key: string, patch: Partial<LocalSchedule>) => void;
  onRemove: (key: string) => void;
}) {
  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
        <span className="text-xs text-slate-400">Schedules · cron</span>
        <button className="btn-ghost text-xs" onClick={onAdd}>+ Add</button>
      </div>
      <div className="flex-1 overflow-auto p-3 space-y-3">
        <p className="text-[11px] text-slate-500">
          Each enabled schedule installs a cron trigger. Default timezone <span className="mono">{DEFAULT_TZ}</span>.
        </p>
        {schedules.length === 0 && <div className="text-xs text-slate-500">No schedules yet. Add one to schedule a run.</div>}
        {schedules.map((s) => (
          <div key={s.key} className="border border-border rounded p-2 space-y-2 text-xs">
            <div className="flex items-center justify-between">
              <span className="mono text-slate-500">{s.serverId ?? "new"}</span>
              <button className="text-red-400 hover:text-red-300" onClick={() => onRemove(s.key)}>delete</button>
            </div>
            <label className="block">
              Name
              <input className="w-full mt-1" value={s.name} placeholder="Morning execution"
                onChange={(e) => onUpdate(s.key, { name: e.target.value })} />
            </label>
            <label className="block">
              Cron expression
              <input className="w-full mt-1 mono" value={s.cron_expression} placeholder={DEFAULT_CRON}
                onChange={(e) => onUpdate(s.key, { cron_expression: e.target.value })} />
            </label>
            <div className="grid grid-cols-2 gap-2">
              <label className="block">
                Timezone
                <input className="w-full mt-1 mono" value={s.timezone} placeholder={DEFAULT_TZ}
                  onChange={(e) => onUpdate(s.key, { timezone: e.target.value })} />
              </label>
              <label className="flex items-center gap-2 mt-5">
                <input type="checkbox" checked={s.enabled} onChange={(e) => onUpdate(s.key, { enabled: e.target.checked })} />
                Enabled
              </label>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function StatusBadge({ saveState, dirty }: { saveState: string; dirty: boolean }) {
  if (saveState === "error") return <span className="badge badge-err">Save failed</span>;
  if (saveState === "saving") return <span className="badge badge-warn">Saving…</span>;
  if (saveState === "saved" && !dirty) return <span className="badge badge-ok">Saved</span>;
  if (dirty) return <span className="text-xs text-amber-400">unsaved</span>;
  return <span className="text-xs text-slate-500">—</span>;
}

function DeploymentBadge({ deployment, deployMsg }: { deployment: DeploymentStatus; deployMsg: string | null }) {
  const tone = deployment.status === "deployed" ? "ok" : deployment.status === "failed" ? "err" : "idle";
  return (
    <span className={`badge badge-${tone}`} title={deployMsg ?? "deployment status"}>
      {deployment.status}
    </span>
  );
}
