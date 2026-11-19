// Copy Trading — the compliant "run the algo for the follower" model.
//
// NOT signal-based mirroring (restricted in India): on FOLLOW the platform
// creates a NEW strategy owned by the follower — a deep copy of the leader's
// project, disabled, with the leader's schedules as DISABLED drafts. The
// follower deploys, schedules and controls it like any strategy of their own
// (their broker tokens, their tick claims, their logs) — the leader's source
// code is never shown.
//
// Two tabs: All (strategies offered across accounts) and Mine (my copies).
import { useCallback, useEffect, useState } from "react";
import { useApp } from "../store";
import { copyApi } from "../api/client";
import type { CopyFollowInfo, UniverseEntry } from "../types/strategy";

type Tab = "all" | "mine";

function CopyBadge({ text, tone = "slate" }: { text: string; tone?: string }) {
  const tones: Record<string, string> = {
    slate: "border-border text-slate-400",
    ok: "border-emerald-700 text-emerald-400",
    warn: "border-amber-700 text-amber-400",
    err: "border-red-900 text-red-400",
  };
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded border ${tones[tone]}`}>
      {text}
    </span>
  );
}

export function CopyTradingPage() {
  const navigate = useApp((s) => s.navigate);
  const [tab, setTab] = useState<Tab>("all");
  const [universe, setUniverse] = useState<UniverseEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  // The Mine tab = FOLLOWED strategies (their copies), from the copy
  // registry — with leader attribution and live status.
  const [followed, setFollowed] = useState<CopyFollowInfo[]>([]);
  const [selected, setSelected] = useState<UniverseEntry | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const PAGE_SIZE = 10;

  const load = useCallback(async (q: string, p: number) => {
    try {
      const [uni, followedRes] = await Promise.all([
        copyApi.universe({ query: q, page: p, page_size: PAGE_SIZE }),
        copyApi.mine(),
      ]);
      setUniverse(uni.universe);
      setTotal(uni.total);
      setPages(uni.pages);
      setFollowed(followedRes.mine);
      setErr(null);
      setLoaded(true);
    } catch (e) {
      setErr((e as Error)?.message ?? "failed to load copy trading");
    }
  }, []);

  // Search + pagination drive the server-side query (debounced, so typing
  // does not spam the API).
  useEffect(() => {
    void load(query, page);
  }, [load, query, page]);

  const follow = async (entry: UniverseEntry) => {
    const ok = window.confirm(
      `Follow "${entry.name}" by ${entry.owner_name}?\n\n` +
        "The platform creates YOUR OWN copy of this strategy:\n" +
        "• it runs on YOUR broker with YOUR capital — gains and losses both " +
        "replicate\n" +
        "• it starts DISABLED: you deploy it, review the schedules and arm " +
        "them yourself\n" +
        "• the source code stays with the original author — you control the " +
        "runs, schedules and logs\n" +
        "• open mirrored positions after you stop are yours to manage",
    );
    if (!ok) return;
    setBusy(true);
    setErr(null);
    try {
      const res = await copyApi.follow(entry.strategy_id);
      setMsg(`Following — your copy is ${res.strategy_id} (disabled until you ` +
        "deploy it). Find it under Mine.");
      setTab("mine");
      await load(query, page);
    } catch (e) {
      setErr((e as Error)?.message ?? "could not follow");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Marketplace</h1>
        <p className="text-xs text-slate-500 mt-1">
          Run another user's strategy as YOUR OWN copy — on your broker, with
          your capital, under your control. The platform creates a private
          copy on follow (the source code stays with the author); you deploy
          it, arm its schedules and manage everything yourself.
        </p>
      </div>

      <div className="flex items-center gap-2">
        <button
          className={`text-xs px-3 py-1.5 rounded ${
            tab === "all"
              ? "bg-accent/20 text-accent"
              : "text-slate-400 hover:text-white"
          }`}
          onClick={() => setTab("all")}
        >
          All
        </button>
        <button
          className={`text-xs px-3 py-1.5 rounded ${
            tab === "mine" ? "bg-accent/20 text-accent" : "text-slate-400 hover:text-white"
          }`}
          onClick={() => setTab("mine")}
        >
          Mine
        </button>
      </div>

      {err && <div className="badge badge-err">{err}</div>}
      {msg && <div className="text-xs text-emerald-400">{msg}</div>}

      {tab === "all" && (
        <div className="space-y-5">
          <div className="flex gap-4 items-start">
          {/* --- the list (30%): search + page controls + rows --- */}
          <div className="w-[30%] shrink-0 space-y-2">
            <input
              className="w-full"
              placeholder="Search name, author, description…"
              value={query}
              onChange={(e) => { setQuery(e.target.value); setPage(1); }}
            />
            {!loaded && <div className="text-xs text-slate-500">loading…</div>}
            {loaded && universe.length === 0 && (
              <div className="text-xs text-slate-500 px-1">
                {query
                  ? `No strategies match “${query}”.`
                  : "Nothing is offered for copy trading yet — offer a strategy\n                    with the ○ copy toggle on the Strategies page."}
              </div>
            )}
            {universe.map((entry) => (
              <button
                key={entry.strategy_id}
                className={`w-full text-left px-3 py-2 rounded border transition-colors ${
                  selected?.strategy_id === entry.strategy_id
                    ? "border-accent bg-accent/10"
                    : "border-border hover:border-slate-600"
                }`}
                onClick={() => setSelected(entry)}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-medium truncate">{entry.name}</span>
                  {entry.is_mine && <CopyBadge text="yours" tone="ok" />}
                  {entry.is_followed && <CopyBadge text="following" tone="ok" />}
                </div>
                <div className="text-[11px] text-slate-500 truncate">
                  {entry.owner_name} · {entry.followers} follower
                  {entry.followers === 1 ? "" : "s"}
                </div>
              </button>
            ))}
          </div>

          {/* --- the detail (70%) --- */}
          <div className="flex-1 min-w-0">
            {selected ? (
              <div className="border border-border rounded-lg p-4 space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-sm font-medium">{selected.name}</div>
                    <div className="text-xs text-slate-500">
                      by {selected.owner_name} · {selected.followers} follower
                      {selected.followers === 1 ? "" : "s"} ·{" "}
                      <span className="mono">{selected.strategy_id}</span>{" "}
                      {/* disclosure: does the author run their own strategy? */}
                      <CopyBadge
                        text={selected.author_deployed
                          ? "✓ author-deployed"
                          : "⚠ never deployed by the author"}
                        tone={selected.author_deployed ? "ok" : "warn"}
                      />
                    </div>
                  </div>
                  {selected.is_mine ? (
                    <CopyBadge text="your strategy" tone="ok" />
                  ) : selected.is_followed ? (
                    <CopyBadge text="✓ already following" tone="ok" />
                  ) : (
                    <button
                      className="btn-primary text-xs shrink-0"
                      disabled={busy}
                      onClick={() => follow(selected)}
                    >
                      Follow
                    </button>
                  )}
                </div>
                {selected.description && (
                  <p className="text-xs text-slate-400 whitespace-pre-wrap">
                    {selected.description}
                  </p>
                )}
                <div className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-300">
                  Following creates a <strong>disabled copy</strong> of this
                  strategy under your account. It runs on your broker with your
                  capital. Review and arm the schedules after deploying —
                  nothing runs until you do. The source code is not shown.
                </div>
              </div>
            ) : (
              <div className="border border-border rounded-lg p-6 text-xs text-slate-500">
                Select a strategy on the left to see its details and follow it.
              </div>
            )}
          </div>
        </div>

        {/* pagination — below the master-detail split */}
        {loaded && total > 0 && (
          <div className="flex items-center gap-3 text-xs text-slate-500">
            <button className="btn-ghost text-xs"
                    disabled={page <= 1}
                    onClick={() => setPage(page - 1)}>
              ← Prev
            </button>
            <span>
              page {page} of {pages} · {total} strateg
              {total === 1 ? "y" : "ies"}
            </span>
            <button className="btn-ghost text-xs"
                    disabled={page >= pages}
                    onClick={() => setPage(page + 1)}>
              Next →
            </button>
          </div>
        )}
        </div>
      )}

      {tab === "mine" && (
        <div className="space-y-3">
          {followed.length === 0 && (
            <div className="text-xs text-slate-500">
              You don't follow any strategy yet — pick one from the Universe.
            </div>
          )}
          {followed.map((s) => (
            <div key={s.strategy_id}
                 className="border border-border rounded-lg p-3 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium truncate">{s.name}</span>
                  <CopyBadge text={s.deployment_status} tone={s.deployment_status === "deployed" ? "ok" : "slate"} />
                  {!s.enabled && <CopyBadge text="disabled" tone="warn" />}
                </div>
                <div className="text-[11px] text-slate-500">
                  copy of {s.source_name ?? "a strategy"} · {s.schedule_count}{" "}
                  schedule draft{s.schedule_count === 1 ? "" : "s"} (disabled —
                  arm them after deploy)
                </div>
              </div>
              <button
                className="btn-ghost text-xs shrink-0"
                onClick={() => navigate({ name: "strategy", id: s.strategy_id })}
              >
                Open
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
