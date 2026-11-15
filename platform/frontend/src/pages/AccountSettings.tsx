// Account Settings — a user's own surface: their account information plus a
// self password change. (System Settings, the infrastructure view, is operator
// only — see Sidebar.)
//
// No email or SMS is ever sent, so a user changes their own password here:
// they supply their *current* password (the proof of identity) and a new one.
// On success the backend returns a fresh session token (this tab stays signed
// in) and every *other* session for the account is retired at once.
import { useEffect, useState, type ReactNode } from "react";
import { authApi, setSession, type AuthResponse } from "../api/client";
import type { AccountInfo } from "../types/strategy";

export function AccountSettings() {
  const [info, setInfo] = useState<AccountInfo | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  // --- change-password form state ---
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [formErr, setFormErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  const load = async () => {
    setLoadErr(null);
    try {
      setInfo(await authApi.account());
    } catch (e) {
      setLoadErr((e as Error)?.message ?? "failed to load your account");
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const mismatch = next !== confirm;
  const empty = !current || !next;

  const changePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormErr(null);
    setOk(null);
    if (empty) {
      setFormErr("Enter your current password and the new one.");
      return;
    }
    if (mismatch) {
      setFormErr("The new password and its confirmation do not match.");
      return;
    }
    setBusy(true);
    try {
      const res: AuthResponse = await authApi.changePassword({
        current_password: current,
        new_password: next,
      });
      // The old token is now retired — swap in the fresh one so this tab
      // stays signed in while the others are signed out.
      setSession(res.token, res.user, false);
      setCurrent("");
      setNext("");
      setConfirm("");
      setOk("Password changed. Your other devices are now signed out.");
    } catch (e) {
      // A wrong current password answers 400 (the request is bad, not the
      // session), so it is surfaced here without kicking the user to sign-in.
      setFormErr((e as Error)?.message ?? "could not change the password");
    } finally {
      setBusy(false);
    }
  };

  const input =
    "w-full mt-1 rounded border border-border bg-black/40 px-3 py-2 text-sm text-slate-200 outline-none focus:border-accent";

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-xl font-semibold">Account Settings</h1>
        <p className="text-sm text-slate-400">Your account and password.</p>
      </div>

      {loadErr && <div className="badge badge-err">{loadErr}</div>}

      <div className="panel p-4 space-y-2">
        <h3 className="text-sm font-medium">Your account</h3>
        <Row k="Name" v={info?.full_name || "—"} />
        <Row k="Mobile (username)" v={info?.mobile || "—"} mono />
        <Row k="User ID" v={info?.user_id || "—"} mono />
        <Row
          k="Status"
          v={
            info ? (
              <span className={info.status === "blocked" ? "text-red-400" : "text-emerald-400"}>
                {info.status}
              </span>
            ) : (
              "—"
            )
          }
        />
        <Row k="Strategies" v={info ? String(info.strategy_count) : "—"} />
        <Row k="Created" v={info?.created_at ? new Date(info.created_at).toLocaleString() : "—"} />
        <Row
          k="Last sign-in"
          v={info?.last_login_at ? new Date(info.last_login_at).toLocaleString() : "never"}
        />
      </div>

      <form onSubmit={changePassword} className="panel p-4 space-y-3">
        <h3 className="text-sm font-medium">Change password</h3>
        <p className="text-xs text-slate-400">
          No email or SMS is involved: prove it is you with your current password, then choose a new
          one. Changing it signs out every other device you are signed in on.
        </p>

        {ok && <div className="badge badge-ok">{ok}</div>}
        {formErr && <div className="badge badge-err">{formErr}</div>}

        <label className="block text-sm">
          <span className="text-xs text-slate-400">Current password</span>
          <input
            type="password"
            className={input}
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
            placeholder="Your current password"
          />
        </label>
        <label className="block text-sm">
          <span className="text-xs text-slate-400">New password</span>
          <input
            type="password"
            className={input}
            value={next}
            onChange={(e) => setNext(e.target.value)}
            autoComplete="new-password"
            placeholder="Choose a new password"
          />
        </label>
        <label className="block text-sm">
          <span className="text-xs text-slate-400">Confirm new password</span>
          <input
            type="password"
            className={input}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            placeholder="Repeat the new password"
          />
        </label>

        <div className="flex items-center gap-2">
          <button type="submit" className="btn-primary" disabled={busy || mismatch || empty}>
            {busy ? "Please wait…" : "Change password"}
          </button>
        </div>
      </form>
    </div>
  );
}

function Row({ k, v, mono }: { k: string; v: ReactNode; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-2 text-sm border-b border-border/40 pb-1">
      <span className="text-slate-400 shrink-0">{k}</span>
      <span className={`text-right ${mono ? "mono truncate" : ""}`} title={typeof v === "string" ? v : undefined}>
        {v}
      </span>
    </div>
  );
}
