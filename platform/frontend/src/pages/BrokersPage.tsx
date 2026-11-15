// Brokers — where a user connects their trading brokers (Dhan, Fyers).
//
// Enabling a broker hands the platform the credentials it needs to generate a
// daily access token (Dhan and Fyers tokens expire every day, so the platform
// regenerates them at 08:00 IST). The credentials go straight to the backend,
// which stores them in AWS Parameter Store (SSM SecureString) — the UI never
// persists them and the API never echoes them back. Connecting can
// legitimately take ~90 s (the TOTP retries wait for fresh 30-second cycles),
// so the page polls the backend for the outcome instead of blocking.

import { useCallback, useEffect, useRef, useState } from "react";
import { brokersApi } from "../api/client";
import type { BrokerInfo, BrokerName, BrokerStatus, DhanEnableInput,
              FyersEnableInput } from "../types/strategy";

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

/** Stand-in Fyers mark (rounded tile + geometric "F"). */
function FyersLogo() {
  return (
    <svg width="40" height="40" viewBox="0 0 40 40" aria-hidden="true">
      <defs>
        <linearGradient id="fyers-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#0b1f3a" />
          <stop offset="100%" stopColor="#1f7a8c" />
        </linearGradient>
      </defs>
      <rect width="40" height="40" rx="9" fill="url(#fyers-g)" />
      <path
        d="M13 11h14v4.4H18.6v4h7.2v4.4h-7.2V29H13V11z"
        fill="none"
        stroke="#fff"
        strokeWidth="3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function StatusBadge({ status }: { status: BrokerStatus }) {
  const tone = status === "connected" ? "ok" : status === "failed" ? "err" : "idle";
  return <span className={`badge badge-${tone}`}>{status}</span>;
}

/** A broker's *enable* form fields, in display order. */
interface FieldSpec {
  key: string;
  label: string;
  secret?: boolean;
}

interface BrokerSpec {
  name: BrokerName;
  label: string;
  Logo: () => JSX.Element;
  blurb: string;
  connectingNote: string;
  fields: FieldSpec[];
  emptyForm: Record<string, string>;
}

const BROKER_SPECS: BrokerSpec[] = [
  {
    name: "dhan",
    label: "Dhan",
    Logo: DhanLogo,
    blurb: "Generates an access token via TOTP and refreshes it daily at 08:00 IST.",
    connectingNote:
      "Connecting — the backend keeps retrying while Dhan accepts a fresh TOTP " +
      "window. This usually resolves within two minutes.",
    fields: [
      { key: "totp_secret", label: "TOTP secret (from the Dhan authenticator setup)", secret: true },
      { key: "client_id", label: "Dhan client ID" },
      { key: "pin", label: "PIN", secret: true },
    ],
    emptyForm: { totp_secret: "", client_id: "", pin: "" },
  },
  {
    name: "fyers",
    label: "Fyers",
    Logo: FyersLogo,
    blurb: "Logs in headlessly (OTP → TOTP → PIN), exchanges the app auth code " +
      "and refreshes the token daily at 08:00 IST.",
    connectingNote:
      "Connecting — the backend walks Fyers's login chain and retries while a " +
      "fresh TOTP window arrives. This usually resolves within two minutes.",
    fields: [
      { key: "username", label: "Fyers ID (username)" },
      { key: "client_id", label: "App ID (e.g. AB12CD-100)" },
      { key: "secret_key", label: "App secret key", secret: true },
      { key: "totp_key", label: "TOTP key (base32, from the authenticator setup)", secret: true },
      { key: "pin", label: "PIN", secret: true },
    ],
    emptyForm: { username: "", client_id: "", secret_key: "", totp_key: "", pin: "" },
  },
];

/** One broker's connection card: status, enable form, retry, disconnect. */
function BrokerCard({ spec, info, busy, onEnable, onRetry, onDisable }: {
  spec: BrokerSpec;
  info?: BrokerInfo;
  busy: boolean;
  onEnable: (broker: BrokerName, form: Record<string, string>) => Promise<void>;
  onRetry: (broker: BrokerName) => Promise<void>;
  onDisable: (broker: BrokerName) => Promise<void>;
}) {
  const { name, label, Logo, blurb, connectingNote, fields, emptyForm } = spec;
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<Record<string, string>>(emptyForm);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    await onEnable(name, form);
    setFormOpen(false);
    setForm(emptyForm);
  };

  return (
    <div className="panel p-4 flex items-start gap-4">
      <Logo />
      <div className="flex-1">
        <div className="flex items-center gap-2">
          <span className="font-medium">{label}</span>
          {info && <StatusBadge status={info.status} />}
        </div>
        <p className="text-xs text-slate-500 mt-1">{blurb}</p>

        {!info && !formOpen && (
          <button className="btn-ghost mt-3" onClick={() => setFormOpen(true)}>
            Enable
          </button>
        )}

        {formOpen && (
          <form className="mt-3 space-y-2 max-w-sm" onSubmit={submit}>
            {fields.map((f) => (
              <label key={f.key} className="block text-xs text-slate-400">
                {f.label}
                <input
                  className="mono text-xs w-full mt-1"
                  type={f.secret ? "password" : "text"}
                  autoComplete="off"
                  value={form[f.key]}
                  onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
                  required
                />
              </label>
            ))}
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

        {info?.status === "connecting" && (
          <p className="text-xs text-amber-400 mt-3">{connectingNote}</p>
        )}

        {info?.status === "connected" && (
          <div className="text-xs text-slate-500 mt-3 space-y-0.5">
            <div>
              Token generated:{" "}
              <span className="mono">
                {info.last_token_at
                  ? new Date(info.last_token_at).toLocaleString()
                  : "—"}
              </span>
            </div>
            <div>
              Next refresh:{" "}
              <span className="mono">
                {info.next_refresh_at
                  ? new Date(info.next_refresh_at).toLocaleString()
                  : "—"}
              </span>
            </div>
          </div>
        )}

        {info?.status === "failed" && (
          <div className="mt-3 space-y-2">
            <div className="text-xs text-red-400">{info.last_error}</div>
            <button className="btn-ghost" disabled={busy}
                    onClick={() => onRetry(name)}>
              Try again
            </button>
          </div>
        )}

        {info && info.status !== "connecting" && (
          <button
            className="btn-ghost text-slate-400 mt-3"
            disabled={busy}
            onClick={() => onDisable(name)}
          >
            Disconnect
          </button>
        )}
      </div>
    </div>
  );
}

export function BrokersPage() {
  const [brokers, setBrokers] = useState<BrokerInfo[]>([]);
  const [err, setErr] = useState<string | null>(null);
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

  const enable = async (broker: BrokerName, form: Record<string, string>) => {
    setBusy(true);
    setMsg(null);
    try {
      // The form matches the selected broker's field spec (the card renders
      // only that broker's fields), so the cast is safe.
      const res = await brokersApi.enable(broker,
                                          form as unknown as DhanEnableInput | FyersEnableInput);
      setConnectNotice(res.status === "connecting");
      setMsg("Connecting… this can take up to two minutes while the TOTP " +
        "retries wait for a fresh window.");
      await load();
    } catch (e) {
      setMsg((e as Error)?.message ?? `could not enable ${broker}`);
    } finally {
      setBusy(false);
    }
  };

  const disable = async (broker: BrokerName) => {
    if (!window.confirm(
      `Disconnect ${broker}? The stored credentials and token are deleted.`)) {
      return;
    }
    setBusy(true);
    try {
      await brokersApi.disable(broker);
      setMsg(`${broker} disconnected.`);
      await load();
    } catch (e) {
      setMsg((e as Error)?.message ?? `could not disable ${broker}`);
    } finally {
      setBusy(false);
    }
  };

  const retry = async (broker: BrokerName) => {
    setBusy(true);
    setMsg("Retrying connection…");
    try {
      await brokersApi.retry(broker);
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
          Connect a broker so the platform can keep an access token fresh.
          Credentials are stored encrypted in AWS Parameter Store (SSM
          SecureString) — never in a strategy, never logged.
        </p>
      </div>

      {err && <div className="badge badge-err">{err}</div>}
      {msg && <div className="text-xs text-slate-400">{msg}</div>}

      <div className="space-y-4">
        {BROKER_SPECS.map((spec) => (
          <BrokerCard
            key={spec.name}
            spec={spec}
            info={brokers.find((b) => b.broker === spec.name)}
            busy={busy}
            onEnable={enable}
            onRetry={retry}
            onDisable={disable}
          />
        ))}
      </div>
    </div>
  );
}
