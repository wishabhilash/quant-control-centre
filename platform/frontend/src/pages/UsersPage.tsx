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
                  <td className="p-2 text-right">
                    <button
                      className={blocked ? "btn-ghost text-xs" : "btn-ghost text-xs text-amber-400"}
                      disabled={inFlight}
                      onClick={() => setBlocked(u, !blocked)}
                    >
                      {inFlight ? "…" : blocked ? "Unblock" : "Block"}
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
    </div>
  );
}
