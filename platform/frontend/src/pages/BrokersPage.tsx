// Brokers — where a user connects their trading broker (currently Dhan).
//
// Enabling a broker hands the platform the credentials it needs to generate a
// daily access token (Dhan tokens expire every day, so the platform
// regenerates them at 08:00 IST). The credentials go straight to the backend,
// which stores them in AWS Parameter Store (SSM SecureString) — the UI never
// persists them and
// the API never echoes them back. Connecting can legitimately take ~90 s
// (the TOTP retries wait for fresh 30-second cycles), so the page polls the
// backend for the outcome instead of blocking on the request.

import { useCallback, useEffect, useRef, useState } from "react";
import { brokersApi } from "../api/client";
import type { BrokerInfo, BrokerStatus } from "../types/strategy";

/** Stand-in Dhan mark (rounded tile + geometric "D"). Swap in the official
 *  asset here if/when brand assets are available. */
function DhanLogo() {
  return (
    <svg width="40" height="40" viewBox="0 0 40 40" aria-hidden="true">
      <defs>
        <linearGradient id="dhan-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#1d2b64" />
          <stop offset="100%" stopColor="#3a5bd9" />
        </linearGradient>
      </defs>
      <rect width="40" height="40" rx="9" fill="url(#dhan-g)" />
      <path
        d="M14 11h6.5a8.5 8.5 0 0 1 0 17H14V11z"
        fill="none"
        stroke="#fff"
        strokeWidth="3.2"
        strokeLinejoin="round"
      />
      <circle cx="20" cy="30" r="2.1" fill="#7ef0c9" />
    </svg>
  );
}

function StatusBadge({ status }: { status: BrokerStatus }) {
  const tone = status === "connected" ? "ok" : status === "failed" ? "err" : "idle";
  return <span className={`badge badge-${tone}`}>{status}</span>;
}

export function BrokersPage() {
  const [brokers, setBrokers] = useState<BrokerInfo[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState({ totp_secret: "", client_id: "", pin: "" });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [connectNotice, setConnectNotice] = useState(false);
  const pollRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await brokersApi.list();
      setBrokers(res.brokers);
      setErr(null);
      setConnectNotice(res.brokers.some((b) => b.status === "connecting"));
    } catch (e) {
      setErr((e as Error)?.message ?? "failed to load broker connections");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // While a connection attempt is in flight (or just submitted), poll until it
  // resolves — the backend keeps trying for a few TOTP cycles (~90 s).
  useEffect(() => {
    if (!connectNotice) return;
    pollRef.current = window.setInterval(load, 3000);
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    };
  }, [connectNotice, load]);

  const dhan = brokers.find((b) => b.broker === "dhan");

  const enable = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const res = await brokersApi.enableDhan({
        totp_secret: form.totp_secret,
        client_id: form.client_id,
        pin: form.pin,
      });
      setFormOpen(false);
      setForm({ totp_secret: "", client_id: "", pin: "" });
      setConnectNotice(res.status === "connecting");
      setMsg("Connecting… this can take up to two minutes while the TOTP " +
        "retries wait for a fresh window.");
      await load();
    } catch (e2) {
      setMsg((e2 as Error)?.message ?? "could not enable Dhan");
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    if (!window.confirm("Disconnect Dhan? The stored credentials and token are deleted.")) return;
    setBusy(true);
    try {
      await brokersApi.disableDhan();
      setMsg("Dhan disconnected.");
      await load();
    } catch (e) {
      setMsg((e as Error)?.message ?? "could not disable Dhan");
    } finally {
      setBusy(false);
    }
  };

  const retry = async () => {
    setBusy(true);
    setMsg("Retrying connection…");
    try {
      await brokersApi.retryDhan();
      await load();
    } catch (e) {
      setMsg((e as Error)?.message ?? "retry failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Brokers</h1>
        <p className="text-xs text-slate-500 mt-1">
          Connect your broker so the platform can keep an access token fresh.
          Credentials are stored encrypted in AWS Parameter Store (SSM
          SecureString) — never in a strategy, never logged.
        </p>
      </div>

      {err && <div className="badge badge-err">{err}</div>}
      {msg && <div className="text-xs text-slate-400">{msg}</div>}

      <div className="panel p-4 flex items-start gap-4">
        <DhanLogo />
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium">Dhan</span>
            {dhan && <StatusBadge status={dhan.status} />}
          </div>
          <p className="text-xs text-slate-500 mt-1">
            Generates an access token via TOTP and refreshes it daily at
            08:00 IST.
          </p>

          {!dhan && !formOpen && (
            <button className="btn-ghost mt-3" onClick={() => setFormOpen(true)}>
              Enable
            </button>
          )}

          {formOpen && (
            <form className="mt-3 space-y-2 max-w-sm" onSubmit={enable}>
              <label className="block text-xs text-slate-400">
                TOTP secret (from the Dhan authenticator setup)
                <input
                  className="mono text-xs w-full mt-1"
                  type="password"
                  autoComplete="off"
                  value={form.totp_secret}
                  onChange={(e) => setForm({ ...form, totp_secret: e.target.value })}
                  required
                />
              </label>
              <label className="block text-xs text-slate-400">
                Dhan client ID
                <input
                  className="mono text-xs w-full mt-1"
                  autoComplete="off"
                  value={form.client_id}
                  onChange={(e) => setForm({ ...form, client_id: e.target.value })}
                  required
                />
              </label>
              <label className="block text-xs text-slate-400">
                PIN
                <input
                  className="mono text-xs w-full mt-1"
                  type="password"
                  autoComplete="off"
                  value={form.pin}
                  onChange={(e) => setForm({ ...form, pin: e.target.value })}
                  required
                />
              </label>
              <div className="flex gap-2 pt-1">
                <button className="btn-ghost" disabled={busy}>
                  {busy ? "Saving…" : "Connect"}
                </button>
                <button
                  type="button"
                  className="btn-ghost text-slate-400"
                  disabled={busy}
                  onClick={() => setFormOpen(false)}
                >
                  Cancel
                </button>
              </div>
            </form>
          )}

          {dhan?.status === "connecting" && (
            <p className="text-xs text-amber-400 mt-3">
              Connecting — the backend keeps retrying while Dhan accepts a fresh
              TOTP window. This usually resolves within two minutes.
            </p>
          )}

          {dhan?.status === "connected" && (
            <div className="text-xs text-slate-500 mt-3 space-y-0.5">
              <div>
                Token generated:{" "}
                <span className="mono">
                  {dhan.last_token_at
                    ? new Date(dhan.last_token_at).toLocaleString()
                    : "—"}
                </span>
              </div>
              <div>
                Next refresh:{" "}
                <span className="mono">
                  {dhan.next_refresh_at
                    ? new Date(dhan.next_refresh_at).toLocaleString()
                    : "—"}
                </span>
              </div>
            </div>
          )}

          {dhan?.status === "failed" && (
            <div className="mt-3 space-y-2">
              <div className="text-xs text-red-400">{dhan.last_error}</div>
              <button className="btn-ghost" disabled={busy} onClick={retry}>
                Try again
              </button>
            </div>
          )}

          {dhan && dhan.status !== "connecting" && (
            <button
              className="btn-ghost text-slate-400 mt-3"
              disabled={busy}
              onClick={disable}
            >
              Disconnect
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
