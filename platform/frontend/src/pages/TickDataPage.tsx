// Tick data — one broker account's live market-data feed.
//
// The backend owns one feed worker per user+broker account (a single broker
// socket); this page manages its subscription set and renders the live tick
// stream. Ticks arrive over a WebSocket that forwards the account's Redis
// pub/sub channel — a TRANSIENT bus (nothing is stored, there is no history:
// only what arrives while the page is open, like tailing a log). Feed state
// (running / subscriptions / errors) is polled so enable/disable and
// subscribe/unsubscribe reflect the server, not local guesses.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../store";
import { ticksApi, ticksStreamUrl } from "../api/client";
import type { BrokerName, TickEvent, TickFeedStatus } from "../types/strategy";

/** Kept in memory only — the tail the panel shows (the bus keeps nothing). */
const VIEW_CAP = 400;
const MAX_SYMBOLS_PER_SUBMIT = 50;

interface ViewLine {
  id: number;
  kind: "tick" | "event" | "error";
  text: string;
  tone: string;
}

function fmtNum(n: number | null | undefined, dp = 2): string {
  return n === null || n === undefined ? "—" : n.toFixed(dp);
}

function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  return `${d.toLocaleTimeString("en-IN", { hour12: false })}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}

function tickLine(t: TickEvent): string {
  const inst = `${t.instrument.exchange ?? "?"}:${t.instrument.symbol}`;
  const parts = [
    fmtTime(t.ts_gateway),
    inst.padEnd(24),
    String(fmtNum(t.ltp)).padStart(10),
    `vol ${fmtNum(t.ohlcv?.volume, 0)}`.padEnd(14),
    `bid ${fmtNum(t.top?.bid)}×${fmtNum(t.top?.bid_qty, 0)}`.padEnd(20),
    `ask ${fmtNum(t.top?.ask)}×${fmtNum(t.top?.ask_qty, 0)}`.padEnd(20),
    `ex ${t.ts_exchange ? fmtTime(t.ts_exchange) : "—"}`,
  ];
  return parts.join("  ");
}

function stateBadge(state: TickFeedStatus["state"]): string {
  switch (state) {
    case "connected":
      return "badge badge-ok";
    case "error":
      return "badge badge-err";
    case "starting":
    case "reconnecting":
      return "badge badge-idle";
    default:
      return "badge badge-idle";
  }
}

export function TickDataPage({ broker }: { broker: BrokerName }) {
  const navigate = useApp((s) => s.navigate);
  const [status, setStatus] = useState<TickFeedStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [symbolInput, setSymbolInput] = useState("");
  const [search, setSearch] = useState("");
  const [autoscroll, setAutoscroll] = useState(true);
  const [lines, setLines] = useState<ViewLine[]>([]);
  // Socket liveness as STATE (a ref alone would not re-render the chip).
  const [live, setLive] = useState(false);

  const bufferRef = useRef<ViewLine[]>([]);
  const idRef = useRef(0);
  const wsRef = useRef<WebSocket | null>(null);
  const autoscrollRef = useRef(autoscroll);
  const containerRef = useRef<HTMLDivElement>(null);
  // Auto-reconnect state (refs: survive re-renders, cleared on unmount).
  const retryTimerRef = useRef<number | null>(null);
  const attemptsRef = useRef(0);
  const aliveRef = useRef(false);

  useEffect(() => {
    autoscrollRef.current = autoscroll;
  }, [autoscroll]);

  const loadStatus = useCallback(async () => {
    try {
      const res = await ticksApi.status(broker);
      setStatus(res);
      setErr(null);
    } catch (e) {
      setErr((e as Error)?.message ?? "failed to load the feed status");
    }
  }, [broker]);

  // Feed status: poll every 3 s (like the dashboard's broker poll).
  useEffect(() => {
    loadStatus();
    const t = window.setInterval(loadStatus, 3000);
    return () => window.clearInterval(t);
  }, [loadStatus]);

  // The live stream. Ticks can arrive fast — they buffer in a ref and flush
  // to state every 250 ms, so React re-renders at a human rate, not a tick rate.
  // The socket AUTO-RECONNECTS with a capped backoff (1 s → 15 s): a backend
  // restart or a proxy blip must not leave a silently dead panel — the stream
  // closed once and stayed closed before this (regression from prod).
  useEffect(() => {
    bufferRef.current = [];
    setLines([]);
    attemptsRef.current = 0;
    aliveRef.current = false;

    const connect = () => {
      const ws = new WebSocket(ticksStreamUrl(broker));
      wsRef.current = ws;

      ws.onopen = () => {
        // A successful handshake resets the backoff: the next drop retries fast.
        attemptsRef.current = 0;
        aliveRef.current = true;
        setLive(true);
      };
      ws.onmessage = (ev) => {
        let data: Record<string, unknown>;
        try {
          data = JSON.parse(ev.data as string);
        } catch {
          return;
        }
        if (data.type === "tick") {
          const tick = data as unknown as TickEvent;
          bufferRef.current.push({
            id: ++idRef.current,
            kind: "tick",
            text: tickLine(tick),
            tone: "text-slate-200",
          });
        } else if (data.type === "error") {
          bufferRef.current.push({
            id: ++idRef.current, kind: "error",
            text: `${fmtTime(Date.now() / 1000)}  [error] ${String(data.message ?? "")}`,
            tone: "text-red-400",
          });
        } else if (data.type === "feed") {
          bufferRef.current.push({
            id: ++idRef.current, kind: "event",
            text: `${fmtTime(Date.now() / 1000)}  [feed] ${String(data.state ?? "")}`,
            tone: "text-amber-400",
          });
        }
        // heartbeats are liveness only — not displayed
      };
      ws.onclose = () => {
        wsRef.current = null;
        setLive(false);
        if (!aliveRef.current) {
          bufferRef.current.push({
            id: ++idRef.current, kind: "event",
            text: `${fmtTime(Date.now() / 1000)}  [stream closed]`,
            tone: "text-slate-500",
          });
        }
        aliveRef.current = false;
        // Exponential backoff, capped at 15 s. Auth failures (4401) are NOT
        // special-cased: a fresh sign-in on this page re-mounts the effect,
        // and retrying a 4401 is harmless (the handshake is rejected cheaply).
        const delay = Math.min(1000 * 2 ** attemptsRef.current, 15000);
        attemptsRef.current += 1;
        retryTimerRef.current = window.setTimeout(connect, delay);
      };
    };
    connect();

    const flush = window.setInterval(() => {
      if (bufferRef.current.length === 0) return;
      const buffered = bufferRef.current;
      bufferRef.current = [];
      setLines((prev) => {
        const next = [...prev, ...buffered];
        return next.length > VIEW_CAP ? next.slice(next.length - VIEW_CAP) : next;
      });
    }, 250);

    return () => {
      window.clearInterval(flush);
      if (retryTimerRef.current !== null) {
        window.clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
      aliveRef.current = true; // unmounting: suppress the closed line
      wsRef.current?.close();
      wsRef.current = null;
      setLive(false);
    };
  }, [broker]);

  // Auto-scroll (same behaviour as the Logs tab).
  useEffect(() => {
    if (autoscrollRef.current && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [lines]);

  const visible = useMemo(() => {
    const q = search.trim().toUpperCase();
    if (!q) return lines;
    return lines.filter((l) => l.text.toUpperCase().includes(q));
  }, [lines, search]);

  const act = async (fn: () => Promise<TickFeedStatus>, done: string) => {
    setBusy(true);
    setMsg(null);
    try {
      setStatus(await fn());
      setMsg(done);
    } catch (e) {
      setMsg((e as Error)?.message ?? "request failed");
    } finally {
      setBusy(false);
    }
  };

  const parseSymbols = (): string[] => {
    const symbols = symbolInput
      .split(/[\s,;]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    return [...new Set(symbols)];
  };

  const subscribe = async () => {
    const symbols = parseSymbols();
    if (symbols.length === 0) {
      setMsg("enter at least one symbol, e.g. NSE:SBIN-EQ");
      return;
    }
    if (symbols.length > MAX_SYMBOLS_PER_SUBMIT) {
      setMsg(`at most ${MAX_SYMBOLS_PER_SUBMIT} symbols per request`);
      return;
    }
    await act(() => ticksApi.subscribe(broker, symbols),
      `subscribed ${symbols.join(", ")}`);
    setSymbolInput("");
  };

  const unsubscribe = async (symbol: string) => {
    await act(() => ticksApi.unsubscribe(broker, [symbol]),
      `unsubscribed ${symbol}`);
  };

  const enabled = status?.enabled ?? false;

  return (
    <div className="space-y-4 flex flex-col h-full">
      <div className="flex items-start gap-3 flex-wrap">
        {/* In-app navigation, NOT history.back(): the SPA never pushes
            browser history entries (routes are zustand state), so back()
            would escape to the bare origin. */}
        <button className="btn-ghost text-xs"
                onClick={() => navigate({ name: "brokers" })}>
          ← Brokers
        </button>
        <div className="flex-1 min-w-[240px]">
          <h1 className="text-xl font-semibold capitalize">
            Tick data — {broker}
          </h1>
          <p className="text-xs text-slate-500 mt-1">
            One live market-data connection for this account. Symbols use the
            broker's notation (e.g. NSE:SBIN-EQ). The stream is live-only —
            like a log tail, nothing is stored.
          </p>
        </div>
        {status && (
          <div className="text-xs text-slate-500 space-y-0.5">
            <div>
              Feed: <span className={stateBadge(status.state)}>{status.state ?? "off"}</span>
            </div>
            <div>
              Ticks received this session: <span className="mono">{status.ticks_published}</span>
              {status.last_tick_at && (
                <> · last at <span className="mono">{fmtTime(new Date(status.last_tick_at).getTime() / 1000)}</span></>
              )}
            </div>
          </div>
        )}
      </div>

      {err && <div className="badge badge-err">{err}</div>}
      {msg && <div className="text-xs text-slate-400">{msg}</div>}
      {status?.last_error && status.state === "error" && (
        <div className="badge badge-err">{status.last_error}</div>
      )}

      {/* Feed control */}
      <div className="panel p-3 flex items-center gap-3 flex-wrap">
        {!enabled ? (
          <button className="btn-ghost" disabled={busy}
                  onClick={() => act(() => ticksApi.enable(broker),
                    "feed enabled")}>
            Enable tick data
          </button>
        ) : (
          <button className="btn-ghost text-slate-400" disabled={busy}
                  onClick={() => act(() => ticksApi.disable(broker),
                    "feed disabled")}>
            Disable tick data
          </button>
        )}
        {!enabled && (
          <span className="text-xs text-slate-500">
            The feed starts on the first subscribe — enable it explicitly to
            hold the connection open.
          </span>
        )}
      </div>

      {/* Subscriptions */}
      <div className="panel p-3 space-y-2">
        <div className="flex gap-2 flex-wrap">
          <input
            className="mono text-xs flex-1 min-w-[260px]"
            placeholder="NSE:SBIN-EQ, NSE:INFY-EQ  (comma or space separated)"
            value={symbolInput}
            onChange={(e) => setSymbolInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") { e.preventDefault(); void subscribe(); }
            }}
            disabled={busy}
          />
          <button className="btn-ghost" disabled={busy} onClick={subscribe}>
            Subscribe
          </button>
        </div>
        <div className="flex gap-2 flex-wrap">
          {(status?.subscriptions ?? []).map((s) => {
            // Provenance: who holds this symbol. A strategy's claim is
            // strategy-managed (the ✕ below removes only the dashboard's
            // claim — a strategy keeps its own until it releases it).
            const holders = (status?.claims ?? {})[s] ?? [];
            const strategyHolders = holders.filter((c) => c !== "dashboard");
            return (
              <span key={s} className="text-xs mono px-2 py-1 rounded bg-black/30
                                       border border-border flex items-center gap-2">
                {s}
                {strategyHolders.length > 0 && (
                  <span className="text-[10px] text-sky-400"
                        title={`Also claimed by: ${strategyHolders.join(", ")}`}>
                    ⛁ {strategyHolders.join(", ")}
                  </span>
                )}
                <button
                  className="text-slate-500 hover:text-red-400"
                  disabled={busy}
                  title={strategyHolders.length > 0
                    ? `Unsubscribe ${s} for the dashboard only (strategy claims stay)`
                    : `Unsubscribe ${s}`}
                  onClick={() => unsubscribe(s)}
                >
                  ×
                </button>
              </span>
            );
          })}
          {(status?.subscriptions ?? []).length === 0 && (
            <span className="text-xs text-slate-500">
              No symbols subscribed yet.
            </span>
          )}
        </div>
      </div>

      {/* The live tick panel (mirrors the Logs tab) */}
      <div className="panel flex flex-col flex-1 min-h-[300px]">
        <div className="flex items-center gap-2 p-2 border-b border-border flex-wrap">
          <span className={`text-xs px-2 py-1 rounded ${
            wsRef.current ? "text-emerald-400" : "text-slate-500"
          }`}>
            {live ? "● live" : "○ disconnected"}
          </span>
          <input
            className="flex-1 min-w-[200px] mono text-xs"
            placeholder="filter…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <label className="flex items-center gap-1 text-xs text-slate-400">
            <input type="checkbox" checked={autoscroll}
                   onChange={(e) => setAutoscroll(e.target.checked)} />
            auto-scroll
          </label>
          <button className="btn-ghost text-xs" onClick={() => setLines([])}>
            Clear
          </button>
          <span className="text-xs text-slate-500 ml-auto">
            {visible.length} / {VIEW_CAP} lines shown · live only, no history
          </span>
        </div>
        <div ref={containerRef}
             className="flex-1 overflow-auto p-2 mono text-xs bg-black/30 whitespace-pre">
          {visible.length === 0 ? (
            <div className="text-slate-500 p-2">
              {enabled
                ? "Waiting for ticks — subscribe to a liquid symbol while the market is open."
                : "Enable tick data, then subscribe to symbols."}
            </div>
          ) : (
            visible.map((l) => (
              <div key={l.id} className={l.tone}>{l.text}</div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
