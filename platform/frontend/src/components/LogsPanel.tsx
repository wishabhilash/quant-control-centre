// Logs panel — the single strategy.log (spec 13).
//
// The log is a file on the server, so the panel POLLS it: an incremental
// fetch from the last known offset (the API returns `next_offset`), every few
// seconds. That is simpler and more robust than a WebSocket (no proxying, no
// token in the URL, no reconnect states) and it is the only thing the UI
// needs: history on open, new lines while it is open, and a Refresh button
// for an on-demand reload.
//
// Filters (search / level) are applied client-side, so the poll always asks
// for the raw tail and the cursor stays exact.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { deployApi, logsApi, getApiToken } from "../api/client";

type Level = "ALL" | "INFO" | "WARNING" | "ERROR" | "DEBUG" | "CRITICAL";
const LEVELS: Level[] = ["ALL", "INFO", "WARNING", "ERROR", "DEBUG", "CRITICAL"];
const TAIL = 500;
const CAP = 5000;
const POLL_MS = 3000;

interface LogLine {
  text: string;
  level: string;
}

function parseLevel(line: string): string {
  const m = line.match(/\|\s*(INFO|WARNING|ERROR|DEBUG|CRITICAL)\s*\|/i);
  return m ? m[1].toUpperCase() : "INFO";
}

function levelClass(level: string): string {
  switch (level) {
    case "ERROR":
      return "text-red-400";
    case "CRITICAL":
      return "text-red-500 font-semibold";
    case "WARNING":
      return "text-amber-400";
    case "DEBUG":
      return "text-slate-500";
    default:
      return "text-slate-200";
  }
}

export function LogsPanel({ id }: { id?: string | null }) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [search, setSearch] = useState("");
  const [level, setLevel] = useState<Level>("ALL");
  const [autoscroll, setAutoscroll] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Is a run live right now? Drives the ● running chip and the Halt button
  // (long-running strategies — a live tick consumer — need an explicit kill
  // from here; see the HALT note in the header).
  const [running, setRunning] = useState(false);
  const [halting, setHalting] = useState(false);

  const cursorRef = useRef(0);
  const autoscrollRef = useRef(autoscroll);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    autoscrollRef.current = autoscroll;
  }, [autoscroll]);

  const append = useCallback((incoming: string[]) => {
    if (incoming.length === 0) return;
    setLines((prev) => {
      const next = [...prev, ...incoming.map((t) => ({ text: t, level: parseLevel(t) }))];
      return next.length > CAP ? next.slice(next.length - CAP) : next;
    });
  }, []);

  // Full reload: the last N lines (used on open and by the Refresh button).
  const loadTail = useCallback(async () => {
    if (!id) return;
    try {
      const res = await logsApi.get(id, { tail: TAIL });
      const loaded = res.lines.map((t) => ({ text: t, level: parseLevel(t) }));
      setLines(loaded);
      // The cursor must point at the END of the file, not at the tail start.
      const next = res.total_lines ?? loaded.length;
      cursorRef.current = next;
      setErr(null);
      setUpdatedAt(Date.now());
    } catch (e) {
      setErr((e as Error)?.message ?? "failed to load logs");
    }
  }, [id]);

  // Incremental poll: everything after the last offset.
  const poll = useCallback(async () => {
    if (!id) return;
    try {
      const since = cursorRef.current;
      const res = await logsApi.get(id, { since });
      if (res.total_lines !== undefined && res.total_lines < since) {
        // The log was truncated (bounded) — the offsets shifted: reload.
        await loadTail();
        return;
      }
      append(res.lines ?? []);
      const next = res.next_offset ?? since + (res.lines?.length ?? 0);
      cursorRef.current = next;
      setErr(null);
      setUpdatedAt(Date.now());
    } catch (e) {
      setErr((e as Error)?.message ?? "failed to refresh logs");
    }
  }, [id, append, loadTail]);

  // Is a run live? (polled alongside the log — one cheap status fetch.)
  const refreshStatus = useCallback(async () => {
    if (!id) return;
    try {
      const status = await deployApi.status(id);
      setRunning(status.state === "running");
    } catch {
      // Status is advisory — a failure must not break the log panel.
    }
  }, [id]);

  useEffect(() => {
    if (!id) return;
    cursorRef.current = 0;
    void loadTail();
    void refreshStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    if (!id) return;
    const timer = setInterval(() => {
      // Don't poll a hidden tab.
      if (document.visibilityState === "visible") {
        void poll();
        void refreshStatus();
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [id, poll, refreshStatus]);

  // Halt the live run: an explicit, user-initiated kill (SIGTERM → grace →
  // SIGKILL). Confirmed — it is irreversible — and after it lands the log is
  // reloaded so the HALT verdict is immediately visible.
  const halt = async () => {
    if (!id || halting) return;
    const ok = window.confirm(
      "Halt the running strategy?\n\n" +
        "SIGTERM now, SIGKILL after the grace period. This does NOT change " +
        "the schedules (already-fired runs stay stopped only until the next " +
        "cron tick — use Stop for that) and it never cancels broker orders.",
    );
    if (!ok) return;
    setHalting(true);
    try {
      await deployApi.halt(id);
      await refreshStatus();
      await loadTail();
    } catch (e) {
      setErr((e as Error)?.message ?? "halt failed");
    } finally {
      setHalting(false);
    }
  };

  // Auto-scroll to the newest line when enabled.
  useEffect(() => {
    if (autoscrollRef.current && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [lines, autoscroll]);

  const download = async () => {
    if (!id) return;
    // A plain <a href> cannot carry the Authorization header, so the log is
    // fetched with the session token and saved from a blob URL.
    try {
      const headers: Record<string, string> = {};
      const t = getApiToken();
      if (t) headers.Authorization = `Bearer ${t}`;
      const res = await fetch(logsApi.downloadUrl(id), { headers });
      if (!res.ok) throw new Error(`download failed (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${id}.log`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setErr((e as Error)?.message ?? "failed to download the log");
    }
  };

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return lines.filter((l) => {
      if (level !== "ALL" && l.level !== level) return false;
      if (needle && !l.text.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [lines, search, level]);

  if (!id) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-slate-500 text-sm p-6">
        Save the strategy first, then open this tab to view its log.
      </div>
    );
  }

  return (
    <div className="panel flex flex-col h-full">
      {/* controls — one action per job: run state, refresh, scroll, download */}
      <div className="flex items-center gap-2 p-2 border-b border-border flex-wrap">
        {/* Run state + the explicit kill. stop (schedules) lives on the
            strategy card; a LIVE run is halted here, next to its log. */}
        {running ? (
          <>
            <span className="text-xs text-green-400" title="A run is live (runtime status)">
              ● running
            </span>
            <button
              className="btn-ghost text-xs text-red-400 hover:text-red-300 border border-red-900/60 rounded px-2 py-0.5"
              onClick={halt}
              disabled={halting}
              title="Kill the current run (SIGTERM → grace → SIGKILL). Schedules are not changed; broker orders are never cancelled."
            >
              {halting ? "halting…" : "■ Halt run"}
            </button>
          </>
        ) : (
          <span className="text-xs text-slate-600" title="No run is live (runtime status)">
            ○ idle
          </span>
        )}
        <button className="btn-ghost text-xs" onClick={loadTail} title="Reload the log from disk">
          Refresh
        </button>
        <label className="flex items-center gap-1 text-xs text-slate-400">
          <input
            type="checkbox"
            checked={autoscroll}
            onChange={(e) => setAutoscroll(e.target.checked)}
          />{" "}
          auto-scroll
        </label>
        <span className="text-[11px] text-slate-600">
          {updatedAt
            ? `updated ${new Date(updatedAt).toLocaleTimeString()}`
            : "loading…"}
        </span>
        <button className="btn-ghost text-xs ml-auto" onClick={download}>
          ▼ Download
        </button>
      </div>

      {/* filters */}
      <div className="flex items-center gap-2 p-2 border-b border-border flex-wrap">
        <input
          className="flex-1 min-w-[200px]"
          placeholder="Search logs…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="flex gap-1">
          {LEVELS.map((lv) => (
            <button
              key={lv}
              onClick={() => setLevel(lv)}
              className={`text-xs px-2 py-1 rounded ${
                level === lv ? "bg-accent/20 text-accent" : "text-slate-400 hover:text-white"
              }`}
            >
              {lv}
            </button>
          ))}
        </div>
        <span className="text-xs text-slate-500 ml-auto">
          {filtered.length} / {lines.length} line{lines.length === 1 ? "" : "s"}
        </span>
      </div>

      {err && (
        <div className="p-2">
          <span className="badge badge-err">{err}</span>
        </div>
      )}

      {/* log body */}
      <div
        ref={containerRef}
        className="flex-1 overflow-auto p-2 mono text-xs bg-black/30 whitespace-pre-wrap"
      >
        {filtered.length === 0 ? (
          <div className="text-slate-500 p-2">No matching log lines</div>
        ) : (
          filtered.map((l, i) => (
            <div key={i} className={levelClass(l.level)}>
              {l.text}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
