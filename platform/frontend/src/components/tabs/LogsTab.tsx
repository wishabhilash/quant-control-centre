// Logs tab — historical + live streaming of the single strategy.log (spec 13).
// Live updates flow over WebSocket; the frontend never touches EC2 files directly
// (all access is through FastAPI). Auto-scroll, pause/resume, search and
// INFO/WARNING/ERROR filtering, plus log download.
import { useEffect, useMemo, useRef, useState } from "react";
import { logsApi } from "../../api/client";
import type { StrategyDocument } from "../../types/strategy";

type Level = "ALL" | "INFO" | "WARNING" | "ERROR" | "DEBUG" | "CRITICAL";
const LEVELS: Level[] = ["ALL", "INFO", "WARNING", "ERROR", "DEBUG", "CRITICAL"];
const TAIL = 500; // initial history to fetch
const CAP = 5000; // max lines retained in the client buffer

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

export function LogsTab({ doc }: { doc: StrategyDocument }) {
  const id = doc.strategy_id;

  const [lines, setLines] = useState<LogLine[]>([]);
  const [search, setSearch] = useState("");
  const [level, setLevel] = useState<Level>("ALL");
  const [autoscroll, setAutoscroll] = useState(true);
  const [paused, setPaused] = useState(false);
  const [pending, setPending] = useState(0);
  const [live, setLive] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const bufferRef = useRef<LogLine[]>([]);
  const pausedRef = useRef(paused);
  const autoscrollRef = useRef(autoscroll);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    pausedRef.current = paused;
  }, [paused]);
  useEffect(() => {
    autoscrollRef.current = autoscroll;
  }, [autoscroll]);

  const append = (line: LogLine) => {
    if (pausedRef.current) {
      bufferRef.current.push(line);
      setPending((p) => p + 1);
      return;
    }
    setLines((prev) => {
      const next = [...prev, line];
      return next.length > CAP ? next.slice(next.length - CAP) : next;
    });
  };

  const loadTail = async () => {
    try {
      const res = await logsApi.get(id, { tail: TAIL });
      setLines(res.lines.map((t) => ({ text: t, level: parseLevel(t) })));
      setErr(null);
    } catch (e: any) {
      setErr(e?.message ?? "failed to load logs");
    }
  };

  const disconnect = () => {
    wsRef.current?.close();
    wsRef.current = null;
    setLive(false);
  };

  const connect = () => {
    disconnect();
    try {
      const ws = new WebSocket(logsApi.streamUrl(id));
      wsRef.current = ws;
      ws.onopen = () => setLive(true);
      ws.onclose = () => {
        setLive(false);
        wsRef.current = null;
      };
      ws.onerror = () => {
        setLive(false);
        ws.close();
      };
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data as string) as { type: string; line?: string; level?: string };
          if (data.type === "line" && typeof data.line === "string") {
            append({ text: data.line, level: data.level ?? parseLevel(data.line) });
          }
          // "error" frames just mark the stream ended; connection state is tracked
          // via onclose/onerror.
        } catch {
          /* ignore malformed frames */
        }
      };
    } catch (e: any) {
      setErr(e?.message ?? "failed to open log stream");
    }
  };

  useEffect(() => {
    loadTail();
    connect();
    return () => disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // Auto-scroll to the newest line when enabled, live and not paused.
  useEffect(() => {
    if (autoscrollRef.current && !pausedRef.current && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [lines, autoscroll, paused]);

  const togglePause = () => {
    if (paused) {
      // Flush buffered lines that arrived while paused.
      if (bufferRef.current.length) {
        setLines((prev) => [...prev, ...bufferRef.current]);
        bufferRef.current = [];
        setPending(0);
      }
      setPaused(false);
    } else {
      setPaused(true);
    }
  };

  const download = () => {
    const a = document.createElement("a");
    a.href = logsApi.downloadUrl(id);
    a.download = `${id}.log`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return lines.filter((l) => {
      if (level !== "ALL" && l.level !== level) return false;
      if (needle && !l.text.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [lines, search, level]);

  return (
    <div className="panel flex flex-col h-[75vh]">
      {/* transport controls */}
      <div className="flex items-center gap-2 p-2 border-b border-border flex-wrap">
        <span className={`badge ${live ? "badge-ok" : "badge-idle"}`}>{live ? "● live" : "○ disconnected"}</span>
        <button className="btn-ghost text-xs" onClick={togglePause}>{paused ? "Resume" : "Pause"}</button>
        <button className="btn-ghost text-xs" onClick={connect} disabled={live}>Reconnect</button>
        <button className="btn-ghost text-xs" onClick={loadTail}>Refresh</button>
        <label className="flex items-center gap-1 text-xs text-slate-400">
          <input type="checkbox" checked={autoscroll} onChange={(e) => setAutoscroll(e.target.checked)} /> auto-scroll
        </label>
        <button className="btn-ghost text-xs ml-auto" onClick={download}>&#9660; Download</button>
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

      {err && <div className="p-2"><span className="badge badge-err">{err}</span></div>}

      {/* log body */}
      <div ref={containerRef} className="flex-1 overflow-auto p-2 mono text-xs bg-black/30 whitespace-pre-wrap">
        {filtered.length === 0 ? (
          <div className="text-slate-500 p-2">
            No matching log lines{paused && pending > 0 ? ` (${pending} buffered while paused)` : ""}
          </div>
        ) : (
          filtered.map((l, i) => (
            <div key={i} className={levelClass(l.level)}>
              {l.text}
            </div>
          ))
        )}
      </div>

      {paused && pending > 0 && (
        <div className="p-2 border-t border-border">
          <span className="badge badge-warn">Paused — {pending} new line(s) buffered</span>
        </div>
      )}
    </div>
  );
}
