// Full-page, IDE-style strategy development workspace (spec amendment v1.3).
//
// ONE workspace serves BOTH creating and editing a strategy:
//   • `new-strategy` opens it empty (a starter project).
//   • `strategy` opens the same workspace populated from S3 (via the route id).
//
// Layout (amendment §3): a persistent header and a 4-tab bar (Code /
// Configuration / Schedules / Logs). Only the active tab's body is shown; the
// Code body stays mounted so the Monaco model survives tab switches, and the
// Logs body is lazy-mounted (amendment §8).
//
// All four tabs share a single editing state. "Save" persists the whole
// document (tree + configuration + schedules) to the strategy's single S3 JSON
// object: `strategiesApi.create` for a new strategy, otherwise an ETag-conditional
// `codeApi.save` followed by metadata `strategiesApi.update` and a schedule
// reconcile. "Deploy" saves first (blocking on save failure) then deploys the
// saved project (amendment §2, §8). "Cancel" returns to the list and confirms
// only when unsaved changes exist.
//
// The project tree is entirely user-defined (no fixed main.py structure). Files
// are stored base64-encoded in the S3 document and deployed with their original
// relative paths (golden rule 1, 2).
import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import { codeApi, deployApi, schedulesApi, strategiesApi, systemApi } from "../api/client";
import { useApp } from "../store";
import {
  filesToTree,
  getContent,
  languageFor,
  treeToFiles,
} from "../lib/project";
import { buildCronPreview, DEFAULT_CRON, DEFAULT_TZ } from "../lib/cron";
import type {
  DeploymentStatus,
  TreeNode,
} from "../types/strategy";
import { FileExplorer } from "./FileExplorer";
import { MonacoEditor } from "./MonacoEditor";
import { LogsPanel } from "./LogsPanel";

type Tab = "Code" | "Configuration" | "Schedules" | "Logs";
const TABS: Tab[] = ["Code", "Configuration", "Schedules", "Logs"];

// A local schedule before it has a server-assigned SCH-NNN id. `serverId` is set
// on save; it is the key we upsert by, so editing preserves the id (amendment §6).
type LocalSchedule = {
  key: string;
  serverId?: string;
  name: string;
  cron_expression: string;
  timezone: string;
  enabled: boolean;
  command: string;
};

// The entry point is fixed to main.py and is NOT user-configurable. A schedule's
// `command` supplies the arguments passed to it.
const ENTRYPOINT = "main.py";

// The starter project — two files, freely renamed, replaced or deleted. It does
// NOT impose a project structure on the user (amendment §4 / golden rule 2).
// `requirements.txt` is installed into the strategy's OWN .venv on deploy
// (deploy_service._install_dependencies); comments are ignored by pip, so a
// blank/commented file deploys cleanly with no dependencies.
const STARTER_FILES: Record<string, string> = {
  "main.py":
    "import json\n" +
    "import os\n" +
    "import sys\n" +
    "\n" +
    "\n" +
    "def run(config):\n" +
    "    # Your strategy logic. `config` merges the strategy's parameters from the\n" +
    "    # Configuration tab with the schedule's CLI args (--key value → config[key]).\n" +
    '    print("strategy started", config)\n' +
    "\n" +
    "\n" +
    "def load_config():\n" +
    '    """Parameters (runtime config) + schedule args (sys.argv) → one dict."""\n' +
    "    config = {}\n" +
    '    rc = os.environ.get("QUANT_RUNTIME_CONFIG")\n' +
    "    if rc and os.path.exists(rc):\n" +
    "        with open(rc) as f:\n" +
    '            config = json.load(f).get("configuration", {}).get("parameters", {})\n' +
    "    argv = sys.argv[1:]\n" +
    "    i = 0\n" +
    "    while i < len(argv):\n" +
    '        if argv[i].startswith("--"):\n' +
    '            key = argv[i][2:]\n' +
    '            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):\n' +
    "                config[key] = argv[i + 1]\n" +
    "                i += 2\n" +
    "            else:\n" +
    "                config[key] = True  # a flag with no value, e.g. --dry-run\n" +
    "                i += 1\n" +
    "        else:\n" +
    "            i += 1\n" +
    "    return config\n" +
    "\n" +
    "\n" +
    'if __name__ == "__main__":\n' +
    "    # The platform executes `python main.py <schedule args>` on every cron tick.\n" +
    "    run(load_config())\n",
  "requirements.txt":
    "# Strategy Python dependencies.\n" +
    "#\n" +
    "# One requirement per line, e.g. `pandas==2.2.2`. These are installed into\n" +
    "# this strategy's OWN .venv when the strategy is deployed.\n" +
    "# Leave this file empty for a dependency-free strategy.\n",
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

export function StrategyWorkspace({ id }: { id?: string }) {
  const navigate = useApp((s) => s.navigate);
  const isEdit = Boolean(id);

  // --- header / configuration state (shared by the header + Configuration tab)
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [paramsText, setParamsText] = useState("{}");

  // --- editor state ---------------------------------------------------------
  const [tree, setTree] = useState<TreeNode>(() => filesToTree(STARTER_FILES));
  const [openTabs, setOpenTabs] = useState<string[]>(["main.py", "requirements.txt"]);
  const [activeTabPath, setActiveTabPath] = useState<string | null>("main.py");
  const [baseline, setBaseline] = useState<Record<string, string>>({});
  const [explorerWidth, setExplorerWidth] = useState(240);
  const fileInput = useRef<HTMLInputElement>(null);

  // --- schedules ------------------------------------------------------------
  const [schedules, setSchedules] = useState<LocalSchedule[]>([]);

  // --- persistence / status -------------------------------------------------
  const [strategyId, setStrategyId] = useState<string | null>(id ?? null);
  // Baseline S3 ETag for conditional writes (amendment §8). Refreshed after load
  // and after every save so a stale write surfaces as a 409 conflict.
  const [etag, setEtag] = useState<string | null>(null);
  const [deployment, setDeployment] = useState<DeploymentStatus>({
    status: "not_deployed",
    last_deployed_at: null,
    source_hash: null,
  });
  const [dirty, setDirty] = useState(!id); // a new project has nothing saved yet
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [deployMsg, setDeployMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showCancel, setShowCancel] = useState(false);

  // --- navigation between tabs ---------------------------------------------
  const [tab, setTab] = useState<Tab>("Code");
  const [logsMounted, setLogsMounted] = useState(false);

  // Effective runner paths (from platform settings) used in the cron-line
  // preview; falls back to the spec §4 EC2 layout until the API responds.
  const [runnerPaths, setRunnerPaths] = useState<NonNullable<
    Parameters<typeof buildCronPreview>[0]["paths"]
  > | null>(null);
  useEffect(() => {
    let alive = true;
    systemApi.scheduler().then((s) => {
      if (alive && (s.runner_python || s.runner_script || s.strategies_dir)) {
        setRunnerPaths({
          runner_python: s.runner_python,
          runner_script: s.runner_script,
          strategies_dir: s.strategies_dir,
        });
      }
    }).catch(() => { /* preview falls back to the EC2 layout */ });
    return () => { alive = false; };
  }, []);

  // --- load an existing strategy into the shared state ----------------------
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">(
    id ? "loading" : "ready",
  );
  const [loadErr, setLoadErr] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return; // a new strategy starts from the starter project
    let alive = true;
    setLoadState("loading");
    setLoadErr(null);
    (async () => {
      try {
        // Load the document together with its S3 ETag for conditional writes.
        const { doc, etag: et } = await strategiesApi.getWithEtag(id);
        // The editor tree is decoded text; the S3 store holds base64.
        const code = await codeApi.get(id);
        if (!alive) return;
        // The entry point is fixed to main.py; open it if present, else first file.
        const first = ENTRYPOINT in code.files ? ENTRYPOINT : (Object.keys(code.files)[0] ?? null);
        setName(doc.name);
        setDescription(doc.description ?? "");
        setEnabled(doc.enabled);
        setParamsText(JSON.stringify(doc.configuration.parameters ?? {}, null, 2));
        setTree(filesToTree(code.files));
        setOpenTabs(first ? [first] : []);
        setActiveTabPath(first);
        setSchedules(
          doc.schedules.map((s) => ({
            key: s.schedule_id,
            serverId: s.schedule_id,
            name: s.name,
            cron_expression: s.cron_expression,
            timezone: s.timezone,
            enabled: s.enabled,
            command: s.command ?? "",
          })),
        );
        setDeployment(doc.deployment);
        setEtag(et);
        setBaseline(code.files);
        setDirty(false);
        setSaveState("idle");
        setSaveMsg(null);
        setLoadState("ready");
      } catch (e) {
        if (!alive) return;
        setLoadErr((e as Error).message ?? "failed to load strategy");
        setLoadState("error");
      }
    })();
    return () => {
      alive = false;
    };
  }, [id]);

  // Re-layout Monaco when the Code tab becomes visible again, and lazy-mount the
  // Logs body the first time it is shown (amendment §8, §9).
  useEffect(() => {
    if (tab === "Code") {
      window.dispatchEvent(new Event("resize"));
    }
    if (tab === "Logs") setLogsMounted(true);
  }, [tab]);

  // Any local edit marks the workspace unsaved and clears the last status.
  const markDirty = () => {
    setDirty(true);
    setSaveState("idle");
    setSaveMsg(null);
    setDeployMsg(null);
  };

  const activeContent = activeTabPath ? getContent(tree, activeTabPath) ?? "" : "";
  const activeLang = activeTabPath ? languageFor(activeTabPath) : "plaintext";
  const isTabDirty = (path: string): boolean =>
    (getContent(tree, path) ?? "") !== (baseline[path] ?? "");

  // --- editor content -------------------------------------------------------
  const setActiveContent = (content: string) => {
    if (!activeTabPath) return;
    setTree((t) => {
      const walk = (n: TreeNode): void => {
        if (n.file?.path === activeTabPath) n.file.content = content;
        n.children?.forEach(walk);
      };
      walk(t);
      return { ...t };
    });
    markDirty();
  };

  // --- file explorer operations --------------------------------------------
  // FileExplorer calls onChange for every mutation (create/rename/delete of
  // files AND folders), so marking dirty in onChange covers folder creation too.
  const handleFileCreated = (path: string) => {
    if (!openTabs.includes(path)) setOpenTabs([...openTabs, path]);
    setActiveTabPath(path);
  };
  const handleFileRenamed = (oldPath: string, newPath: string) => {
    setOpenTabs(openTabs.map((p) => (p === oldPath ? newPath : p)));
    if (activeTabPath === oldPath) setActiveTabPath(newPath);
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
    if (activeTabPath === path) setActiveTabPath(next[next.length - 1] ?? null);
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
    if (activeTabPath === path) setActiveTabPath(next[next.length - 1] ?? null);
  };

  // --- ZIP upload (amendment §4) -------------------------------------------
  // The server persists the import, so refresh the tree + baseline ETag. Only
  // available for an already-saved strategy (it targets the S3 document).
  const onUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !strategyId) return;
    setBusy(true);
    setSaveMsg(null);
    setDeployMsg(null);
    try {
      const res = await codeApi.uploadZip(strategyId, file);
      const code = await codeApi.get(strategyId);
      const refreshed = await strategiesApi.getWithEtag(strategyId);
      setTree(filesToTree(code.files));
      const first = ENTRYPOINT in code.files ? ENTRYPOINT : (Object.keys(code.files)[0] ?? null);
      const kept = openTabs.filter((p) => p in code.files);
      setOpenTabs(kept.length ? kept : (first ? [first] : []));
      setActiveTabPath(activeTabPath && activeTabPath in code.files ? activeTabPath : first);
      setEtag(refreshed.etag);
      setBaseline(code.files);
      setDirty(false);
      setSaveState("saved");
      setSaveMsg(`Imported ${res.file_count} file(s) from ZIP → ${strategyId}`);
    } catch (err) {
      setSaveState("error");
      setSaveMsg((err as Error).message ?? "upload failed");
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  // --- resizable explorer ---------------------------------------------------
  const startResize = (e: ReactMouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = explorerWidth;
    const onMove = (ev: MouseEvent) => {
      const w = Math.max(160, Math.min(640, startW + (ev.clientX - startX)));
      setExplorerWidth(w);
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      window.dispatchEvent(new Event("resize")); // re-layout Monaco after a drag
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  };

  // --- schedules ------------------------------------------------------------
  const addSchedule = () => {
    setSchedules((list) => [
      ...list,
      {
        key: `s-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        name: "",
        cron_expression: DEFAULT_CRON,
        timezone: DEFAULT_TZ,
        enabled: true,
        command: "",
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

  // --- persistence ----------------------------------------------------------
  // Reconcile the server's schedules with the local list: drop server schedules
  // no longer present locally, then upsert each local schedule by its server id
  // (created ones get a new SCH-NNN). Keeps ids stable on edit (amendment §6).
  const reconcileSchedules = async (sid: string, local: LocalSchedule[]) => {
    const { schedules: server } = await schedulesApi.list(sid);
    const keep = new Set(local.filter((s) => s.serverId).map((s) => s.serverId as string));
    for (const s of server) {
      if (!keep.has(s.schedule_id)) await schedulesApi.remove(sid, s.schedule_id);
    }
    for (const s of local) {
      const body = {
        name: s.name.trim() || "schedule",
        cron_expression: s.cron_expression.trim(),
        timezone: s.timezone.trim() || DEFAULT_TZ,
        enabled: s.enabled,
        command: s.command.trim(),
      };
      if (s.serverId) await schedulesApi.update(sid, s.serverId, body);
      else await schedulesApi.create(sid, body);
    }
  };

  // Persist the WHOLE document (amendment §2, §8). Never generates a new id when
  // editing. The ETag-conditional code write detects concurrent S3 edits.
  const save = async (): Promise<string | null> => {
    setBusy(true);
    setSaveState("saving");
    setSaveMsg(null);
    setDeployMsg(null);
    try {
      const files = treeToFiles(tree);
      if (!files[ENTRYPOINT]) {
        throw new Error(`Entry point "${ENTRYPOINT}" must be present in the project`);
      }
      const parameters = parseParams(paramsText);

      let sid = strategyId;
      if (!sid) {
        // New strategy: create its single S3 JSON document.
        const created = await strategiesApi.create({
          name: name.trim() || "Untitled",
          description,
          enabled,
          parameters,
          files,
        });
        sid = created.strategy_id;
        setStrategyId(sid);
      } else {
        // Existing strategy: ETag-conditional code write, then metadata update.
        await codeApi.save(sid, files, etag ?? undefined);
        await strategiesApi.update(sid, {
          name: name.trim() || "Untitled",
          description,
          enabled,
          configuration: { parameters },
        });
      }

      await reconcileSchedules(sid, schedules);

      // Refresh the baseline tree + capture the new S3 ETag.
      const refreshed = await strategiesApi.getWithEtag(sid);
      setSchedules(
        refreshed.doc.schedules.map((s) => ({
          key: s.schedule_id,
          serverId: s.schedule_id,
          name: s.name,
          cron_expression: s.cron_expression,
          timezone: s.timezone,
          enabled: s.enabled,
          command: s.command ?? "",
        })),
      );
      setEtag(refreshed.etag);
      setDeployment(refreshed.doc.deployment);
      setBaseline(files);
      setDirty(false);
      setSaveState("saved");
      setSaveMsg(
        `Saved ${Object.keys(files).length} file(s), ${refreshed.doc.schedules.length} schedule(s) → ${sid}`,
      );
      return sid;
    } catch (e) {
      const err = e as Error & { status?: number };
      setSaveState("error");
      let msg = err.message ?? "Save failed";
      if (err.status === 409) {
        msg =
          "Concurrent modification: this strategy was changed elsewhere. Reload and re-apply your changes.";
      }
      setSaveMsg(msg);
      return null;
    } finally {
      setBusy(false);
    }
  };

  // Deploy saves pending changes first (blocking on failure), then deploys the
  // saved project — preserving the exact directory structure (amendment §2).
  const deploy = async () => {
    setDeployMsg(null);
    setSaveMsg(null);
    const sid = dirty || !strategyId ? await save() : strategyId;
    if (!sid) {
      setDeployMsg("Deploy skipped: save failed");
      return;
    }
    setBusy(true);
    setDeployMsg("Deploying…");
    try {
      await deployApi.deploy(sid, false);
      const refreshed = await strategiesApi.getWithEtag(sid);
      setDeployment(refreshed.doc.deployment);
      setEtag(refreshed.etag);
      setDeployMsg("Deployed");
    } catch (e) {
      setSaveState("error");
      setDeployMsg((e as Error).message ?? "Deploy failed");
    } finally {
      setBusy(false);
    }
  };

  // Cancel: never save or deploy. Confirm only when there are unsaved changes.
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

  // --- render ---------------------------------------------------------------
  if (id && loadState === "error") {
    return (
      <div className="flex-1 flex items-center justify-center p-6">
        <span className="badge badge-err">{loadErr}</span>
      </div>
    );
  }
  if (id && loadState === "loading") {
    return <div className="flex-1 flex items-center justify-center text-slate-500">Loading strategy…</div>;
  }

  return (
    <div className="flex-1 flex flex-col min-w-0">
      {/* Persistent header (amendment §2) */}
      <header className="flex items-center gap-3 px-4 py-2 border-b border-border bg-panel/40 shrink-0">
        <button
          className="btn-ghost text-xs"
          onClick={requestCancel}
          title="Return to the strategy list"
        >
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
        {isEdit && strategyId && (
          <span className="text-xs mono text-slate-400 shrink-0" title="Strategy id">
            {strategyId}
          </span>
        )}

        <div className="ml-auto flex items-center gap-2 shrink-0">
          <StatusBadge saveState={saveState} dirty={dirty} />
          <DeploymentBadge deployment={deployment} deployMsg={deployMsg} />
          <button
            className="btn-ghost text-xs"
            onClick={deploy}
            disabled={busy}
            title="Save pending changes, then deploy the saved project"
          >
            {deployMsg === "Deploying…" ? "Deploying…" : "Deploy"}
          </button>
          <button
            className="btn-primary text-xs"
            onClick={save}
            disabled={busy}
            title="Persist the whole document (tree, configuration, schedules)"
          >
            {saveState === "saving" ? "Saving…" : "Save"}
          </button>
        </div>
      </header>

      {/* Tab bar (amendment §3) */}
      <nav className="flex items-center gap-1 px-4 border-b border-border bg-panel/30 shrink-0">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-3 py-2 text-sm border-b-2 -mb-px ${
              tab === t ? "border-accent text-accent" : "border-transparent text-slate-400 hover:text-white"
            }`}
          >
            {t}
          </button>
        ))}
        <span className="ml-auto text-xs pr-2 shrink-0">
          {dirty ? (
            <span className="text-amber-400">● unsaved changes</span>
          ) : (
            <span className="text-emerald-400">saved</span>
          )}
        </span>
      </nav>

      {/* Tab bodies — only the active one is displayed. The Code body stays
          mounted so the Monaco model survives; the Logs body is lazy-mounted. */}
      <div className="flex-1 min-h-0 relative">
        {/* Code — always mounted */}
        <div className={`absolute inset-0 flex min-h-0 ${tab !== "Code" ? "hidden" : ""}`}>
          {/* File explorer (resizable width, amendment §9) */}
          <div
            style={{ width: explorerWidth }}
            className="shrink-0 border-r border-border bg-panel/30"
          >
            <FileExplorer
              root={tree}
              onChange={(t) => {
                setTree(t);
                markDirty();
              }}
              openPath={activeTabPath}
              onOpen={(p) => setActiveTabPath(p)}
              onFileCreated={handleFileCreated}
              onFileRenamed={handleFileRenamed}
              onFileDeleted={handleFileDeleted}
            />
          </div>

          {/* Resize handle */}
          <div
            onMouseDown={startResize}
            className="w-1 shrink-0 cursor-col-resize hover:bg-accent/40"
            title="Drag to resize the explorer"
          />

          {/* Editor with open-file tabs */}
          <div className="flex-1 flex flex-col min-w-0">
            <div className="flex items-center border-b border-border bg-panel/30 text-xs shrink-0">
              <div className="flex-1 overflow-x-auto flex items-center">
                {openTabs.length === 0 ? (
                  <span className="px-3 py-1.5 text-slate-500">No open files</span>
                ) : (
                  openTabs.map((p) => (
                    <div
                      key={p}
                      className={`flex items-center gap-1 px-3 py-1.5 border-r border-border cursor-pointer whitespace-nowrap ${
                        activeTabPath === p ? "bg-accent/15 text-accent" : "text-slate-400 hover:bg-white/5"
                      }`}
                      onClick={() => setActiveTabPath(p)}
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
              <label
                className={`flex items-center gap-1 px-3 border-l border-border shrink-0 ${
                  strategyId ? "cursor-pointer hover:bg-white/5" : "text-slate-600 cursor-not-allowed"
                }`}
                title={strategyId ? "Import a project ZIP" : "Save the strategy first to upload a ZIP"}
              >
                <span>⬆ ZIP</span>
                <input
                  type="file"
                  accept=".zip"
                  ref={fileInput}
                  onChange={onUpload}
                  className="hidden"
                  disabled={!strategyId}
                />
              </label>
            </div>

            <div className="flex-1 min-h-0">
              {activeTabPath ? (
                <MonacoEditor
                  path={activeTabPath}
                  language={activeLang}
                  value={activeContent}
                  onChange={setActiveContent}
                />
              ) : (
                <div className="p-6 text-slate-500 text-sm">
                  Select or create a file to start editing.
                </div>
              )}
            </div>

            <div className="border-t border-border px-3 py-1 text-xs text-slate-500 flex items-center gap-3 shrink-0">
              <span className="mono">{strategyId ?? "unsaved"}</span>
              <span>·</span>
              <span>{Object.keys(treeToFiles(tree)).length} file(s)</span>
              <span>·</span>
              <span>{schedules.length} schedule(s)</span>
              <span className="ml-auto">
                {dirty ? (
                  <span className="text-amber-400">● unsaved</span>
                ) : (
                  <span className="text-emerald-400">saved</span>
                )}
              </span>
            </div>
          </div>
        </div>

        {/* Configuration — always mounted (state is shared at the top level) */}
        <div className={`absolute inset-0 overflow-auto ${tab !== "Configuration" ? "hidden" : ""}`}>
          <div className="p-6 max-w-4xl space-y-5">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <label className="block text-sm">
                <span className="text-xs text-slate-400">Strategy name</span>
                <input
                  className="w-full mt-1"
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value);
                    markDirty();
                  }}
                />
              </label>
              <label className="flex items-center gap-2 text-sm self-end pb-1">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => {
                    setEnabled(e.target.checked);
                    markDirty();
                  }}
                />
                Enabled (install crons on save)
              </label>
              <label className="block text-sm md:col-span-2">
                <span className="text-xs text-slate-400">Description</span>
                <textarea
                  className="w-full mt-1"
                  rows={3}
                  value={description}
                  onChange={(e) => {
                    setDescription(e.target.value);
                    markDirty();
                  }}
                />
              </label>
              <label className="block text-sm md:col-span-2">
                <span className="text-xs text-slate-400">Strategy parameters (JSON object)</span>
                <textarea
                  className="w-full mt-1 mono"
                  rows={6}
                  spellCheck={false}
                  value={paramsText}
                  onChange={(e) => {
                    setParamsText(e.target.value);
                    markDirty();
                  }}
                />
              </label>
            </div>
          </div>
        </div>

        {/* Schedules — always mounted; the editor fills the tab's full width (§9) */}
        <div className={`absolute inset-0 overflow-auto ${tab !== "Schedules" ? "hidden" : ""}`}>
          <div className="p-6 w-full">
            <div className="flex items-start justify-between mb-3 gap-4">
              <div>
                <h3 className="text-sm font-medium">Schedules · Linux cron</h3>
                <p className="text-xs text-slate-500">
                  Each enabled schedule installs a cron trigger that runs the generic runner with a
                  unique <span className="mono">--schedule-id</span>. The runner invokes the fixed
                  entry point <span className="mono">main.py</span> in the strategy's own venv,
                  passing the schedule's <span className="mono">Command</span> as arguments (split
                  into an argument array — no shell). Default timezone{" "}
                  <span className="mono">{DEFAULT_TZ}</span>. A schedule keeps its id when you edit it.
                </p>
              </div>
              <button className="btn-ghost text-xs shrink-0" onClick={addSchedule}>
                + Add schedule
              </button>
            </div>
            {schedules.length === 0 && (
              <div className="text-xs text-slate-500">
                No schedules yet. Add one to schedule a run.
              </div>
            )}
            {/* Full-width responsive grid so cards fill the tab on wide screens. */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              {schedules.map((s) => (
                <div key={s.key} className="border border-border rounded-lg p-3 space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="mono text-xs text-slate-400">
                      {s.serverId ?? "new · assigned on save"}
                    </span>
                    <button
                      className="text-xs text-red-400 hover:text-red-300"
                      onClick={() => removeSchedule(s.key)}
                    >
                      delete
                    </button>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <label className="block text-sm">
                      <span className="text-xs text-slate-400">Name</span>
                      <input
                        className="w-full mt-1"
                        value={s.name}
                        placeholder="Morning execution"
                        onChange={(e) => updateSchedule(s.key, { name: e.target.value })}
                      />
                    </label>
                    <label className="block text-sm">
                      <span className="text-xs text-slate-400">Cron expression</span>
                      <input
                        className="w-full mt-1 mono"
                        value={s.cron_expression}
                        placeholder={DEFAULT_CRON}
                        onChange={(e) => updateSchedule(s.key, { cron_expression: e.target.value })}
                      />
                    </label>
                    <label className="block text-sm">
                      <span className="text-xs text-slate-400">Timezone</span>
                      <input
                        className="w-full mt-1 mono"
                        value={s.timezone}
                        placeholder={DEFAULT_TZ}
                        onChange={(e) => updateSchedule(s.key, { timezone: e.target.value })}
                      />
                    </label>
                    <label className="block text-sm md:col-span-2">
                      <span className="text-xs text-slate-400">
                        Args <span className="text-slate-500">(args passed to main.py, optional)</span>
                      </span>
                      <input
                        className="w-full mt-1 mono"
                        value={s.command}
                        placeholder="--mode backtest --date 2024-01-01"
                        onChange={(e) => updateSchedule(s.key, { command: e.target.value })}
                      />
                    </label>
                    <label className="flex items-center gap-2 text-sm md:mt-1">
                      <input
                        type="checkbox"
                        checked={s.enabled}
                        onChange={(e) => updateSchedule(s.key, { enabled: e.target.checked })}
                      />
                      Enabled
                    </label>
                    {/* Live preview of the exact line this schedule installs into
                        the platform crontab (mirrors scheduler_service). */}
                    <ScheduleCronPreview schedule={s} strategyId={strategyId} paths={runnerPaths} />
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Logs — lazy-mounted on first activation */}
        {logsMounted && (
          <div className={`absolute inset-0 ${tab !== "Logs" ? "hidden" : ""}`}>
            <LogsPanel id={strategyId} />
          </div>
        )}
      </div>

      {/* Save / deploy status line */}
      {(saveMsg || deployMsg) && (
        <div className="border-t border-border px-4 py-1.5 text-xs shrink-0">
          <span className={saveState === "error" ? "text-red-400" : "text-slate-300"}>
            {saveMsg || deployMsg}
          </span>
        </div>
      )}

      {/* Cancel confirmation — shown only when there are unsaved changes */}
      {showCancel && (
        <div
          className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
          onClick={() => setShowCancel(false)}
        >
          <div className="panel p-5 w-[420px]" onClick={(e) => e.stopPropagation()}>
            <h2 className="font-semibold mb-2">Unsaved changes</h2>
            <p className="text-sm text-slate-400 mb-4">
              This strategy has unsaved changes. Discarding will return to the list without saving or
              deploying.
            </p>
            <div className="flex justify-end gap-2">
              <button className="btn-ghost text-xs" onClick={() => setShowCancel(false)}>
                Keep editing
              </button>
              <button className="btn-danger text-xs" onClick={discardChanges}>
                Discard changes
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// --- sub-components ---------------------------------------------------------

// Live preview of the exact cron line this schedule installs into the platform
// crontab. Mirrors SchedulerService.build_cron_entry (see lib/cron.ts): the args
// are not part of the cron line — the runner reads them from S3 at fire time and
// appends them to the fixed main.py entry point.
function ScheduleCronPreview({ schedule, strategyId, paths }: {
  schedule: LocalSchedule;
  strategyId: string | null;
  paths: { runner_python?: string | null; runner_script?: string | null; strategies_dir?: string | null } | null;
}) {
  const preview = buildCronPreview({
    strategyId,
    scheduleId: schedule.serverId,
    cronExpression: schedule.cron_expression,
    timezone: schedule.timezone,
    command: schedule.command,
    paths: paths ?? undefined,
  });
  return (
    <div className="md:col-span-2 rounded border border-border bg-black/30 px-2.5 py-2">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">Crontab preview</div>
      <code className="block mt-1 text-[11px] mono leading-relaxed text-slate-300 break-all whitespace-pre-wrap">
        {preview.line}
      </code>
      <div className="mt-1 text-[11px] text-slate-500">
        ↳ runner then launches (strategy's own venv){" "}
        <span className="mono text-slate-400">{preview.runs}</span>
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

function DeploymentBadge({
  deployment,
  deployMsg,
}: {
  deployment: DeploymentStatus;
  deployMsg: string | null;
}) {
  const tone =
    deployment.status === "deployed" ? "ok" : deployment.status === "failed" ? "err" : "idle";
  return (
    <span className={`badge badge-${tone}`} title={deployMsg ?? "deployment status"}>
      {deployment.status}
    </span>
  );
}
