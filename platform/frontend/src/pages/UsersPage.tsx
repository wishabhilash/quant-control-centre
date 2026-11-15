// Users — the operator's administrative view of the platform's accounts.
//
// Only the operator (the break-glass token) reaches this page: it lists the
// accounts, when they last signed in and how many strategies each one owns.
// It does NOT expose strategy content — the operator has no ambient access to
// user data; assigning a strategy to a user is the explicit action that does.
//
// The operator can also SUSPEND an account (block) — a trading platform needs a
// fast "stop this account now" switch. Blocking is instant: the account can no
// longer sign in and every session token it holds stops verifying at once.

import { useEffect, useState } from "react";
import { adminApi } from "../api/client";
import type { AdminUser } from "../types/strategy";

export function UsersPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [msg, setMsg] = useState<string | null>(null);

  // --- reset-password dialog state ---
  // The operator sets a new password (they don't know the old one); it is
  // returned once so the operator can hand it over out-of-band (e.g. WhatsApp).
  const [resetFor, setResetFor] = useState<AdminUser | null>(null);
  const [newPw, setNewPw] = useState("");
  const [resetBusy, setResetBusy] = useState(false);
  const [resetErr, setResetErr] = useState<string | null>(null);
  const [handoff, setHandoff] = useState<string | null>(null); // the new password, shown once

  const load = async () => {
    setLoading(true);
    try {
      setUsers(await adminApi.users());
      setErr(null);
    } catch (e) {
      setErr((e as Error)?.message ?? "failed to load users");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const setBlocked = async (u: AdminUser, block: boolean) => {
    setBusy((b) => ({ ...b, [u.user_id]: true }));
    setMsg(null);
    try {
      const next = block ? await adminApi.block(u.user_id) : await adminApi.unblock(u.user_id);
      setUsers((list) => list.map((x) => (x.user_id === u.user_id ? next : x)));
      setMsg(`${u.mobile} ${block ? "blocked" : "unblocked"}`);
    } catch (e) {
      setMsg((e as Error)?.message ?? "action failed");
    } finally {
      setBusy((b) => ({ ...b, [u.user_id]: false }));
    }
  };

  const closeReset = () => {
    // Never keep the new password in the UI after the operator has seen it.
    setResetFor(null);
    setNewPw("");
    setHandoff(null);
    setResetErr(null);
    setResetBusy(false);
  };

  const doReset = async () => {
    if (!resetFor || !newPw) {
      setResetErr("Enter a new password for the user.");
      return;
    }
    setResetBusy(true);
    setResetErr(null);
    try {
      const res = await adminApi.resetPassword(resetFor.user_id, newPw);
      // The new password is disclosed exactly once, here, for the handoff.
      setHandoff(res.new_password);
      setNewPw("");
      // A reset retires the user's sessions — refresh so the list stays honest.
      load();
    } catch (e) {
      setResetErr((e as Error)?.message ?? "could not reset the password");
    } finally {
      setResetBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-xl font-semibold">Users</h1>
          <p className="text-xs text-slate-500 mt-1">
            The platform&apos;s accounts. Strategy content stays with its owner —
            an operator can assign or claim a strategy, not browse it.
          </p>
        </div>
        <button className="btn-ghost" onClick={load}>Refresh</button>
      </div>

      {err && <div className="badge badge-err">{err}</div>}
      {msg && <div className="text-xs text-slate-400">{msg}</div>}

      <div className="panel overflow-hidden">
        <table className="w-full text-sm">
          <thead className="text-slate-400 text-xs border-b border-border">
            <tr>
              <th className="text-left p-2">Name</th>
              <th className="text-left p-2">Mobile (username)</th>
              <th className="text-left p-2">User ID</th>
              <th className="text-left p-2">Strategies</th>
              <th className="text-left p-2">Status</th>
              <th className="text-left p-2">Created</th>
              <th className="text-left p-2">Last sign-in</th>
              <th className="text-right p-2">Action</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const blocked = u.status === "blocked";
              const inFlight = !!busy[u.user_id];
              return (
                <tr key={u.user_id} className="border-b border-border/50 hover:bg-white/5">
                  <td className="p-2">{u.full_name || "—"}</td>
                  <td className="p-2 mono">{u.mobile}</td>
                  <td className="p-2 mono text-slate-400">{u.user_id}</td>
                  <td className="p-2">{u.strategy_count}</td>
                  <td className="p-2">
                    <span className={`badge ${blocked ? "badge-err" : "badge-ok"}`}>
                      {blocked ? "blocked" : "active"}
                    </span>
                    {blocked && u.blocked_at && (
                      <div className="text-[11px] text-slate-500 mt-0.5">
                        {new Date(u.blocked_at).toLocaleString()}
                        {u.blocked_by ? ` · ${u.blocked_by}` : ""}
                      </div>
                    )}
                  </td>
                  <td className="p-2 mono text-slate-400">
                    {u.created_at ? new Date(u.created_at).toLocaleDateString() : "—"}
                  </td>
                  <td className="p-2 mono text-slate-400">
                    {u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "never"}
                  </td>
                  <td className="p-2 text-right whitespace-nowrap">
                    <button
                      className={blocked ? "btn-ghost text-xs" : "btn-ghost text-xs text-amber-400"}
                      disabled={inFlight}
                      onClick={() => setBlocked(u, !blocked)}
                    >
                      {inFlight ? "…" : blocked ? "Unblock" : "Block"}
                    </button>{" "}
                    <button
                      className="btn-ghost text-xs"
                      disabled={inFlight}
                      onClick={() => {
                        setResetFor(u);
                        setHandoff(null);
                        setResetErr(null);
                        setNewPw("");
                      }}
                    >
                      Reset password
                    </button>
                  </td>
                </tr>
              );
            })}
            {!loading && users.length === 0 && (
              <tr>
                <td colSpan={8} className="p-4 text-center text-slate-500">
                  No accounts yet
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {resetFor && (
        <div className="panel p-4 space-y-3" onClick={(e) => e.stopPropagation()}>
          <h3 className="text-sm font-medium">
            Reset password · {resetFor.full_name || resetFor.mobile}
          </h3>
          {!handoff ? (
            <>
              <p className="text-xs text-slate-400">
                No email or SMS is sent. Choose a new password; the user&apos;s existing sessions
                are signed out and the password is shown <strong>once</strong> — hand it over to
                them out-of-band (e.g. WhatsApp).
              </p>
              {resetErr && <div className="badge badge-err">{resetErr}</div>}
              <label className="block text-sm">
                <span className="text-xs text-slate-400">New password for {resetFor.mobile}</span>
                <input
                  type="text"
                  className="w-full mt-1 rounded border border-border bg-black/40 px-3 py-2 text-sm text-slate-200 outline-none focus:border-accent mono"
                  value={newPw}
                  onChange={(e) => setNewPw(e.target.value)}
                  placeholder="Enter the new password"
                  autoFocus
                />
              </label>
              <div className="flex items-center gap-2">
                <button className="btn-primary" disabled={resetBusy || !newPw} onClick={doReset}>
                  {resetBusy ? "Please wait…" : "Reset password"}
                </button>
                <button className="btn-ghost" disabled={resetBusy} onClick={closeReset}>
                  Cancel
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="text-xs text-slate-400">
                Password reset. This is the new password — it is shown <strong>once</strong>; copy
                it now and send it to the user out-of-band (e.g. WhatsApp). It is not stored and
                will be cleared when you close this dialog.
              </p>
              <div className="rounded border border-accent/40 bg-accent/10 p-3">
                <div className="text-[11px] uppercase tracking-wider text-slate-400">New password</div>
                <div className="mt-1 text-lg mono break-all text-emerald-300">{handoff}</div>
              </div>
              <button className="btn-ghost text-xs" onClick={closeReset}>
                Done
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
