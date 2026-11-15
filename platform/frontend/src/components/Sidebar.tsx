import { useState } from "react";
import { useApp } from "../store";
import {
  clearApiToken,
  getCurrentUser,
  getSession,
} from "../api/client";

const items: { key: string; label: string; route: any; operatorOnly?: boolean; userOnly?: boolean }[] = [
  { key: "dashboard", label: "Dashboard", route: { name: "dashboard" } },
  { key: "strategies", label: "Strategies", route: { name: "strategies" } },
  // Broker connections belong to the signed-in user (not the operator).
  { key: "brokers", label: "Brokers", route: { name: "brokers" }, userOnly: true },
  { key: "settings", label: "System Settings", route: { name: "settings" } },
  // Administrative view — only for the operator (break-glass) session.
  { key: "users", label: "Users", route: { name: "users" }, operatorOnly: true },
];

export function Sidebar() {
  const route = useApp((s) => s.route);
  const navigate = useApp((s) => s.navigate);
  const openAuth = useApp((s) => s.openAuth);
  const active = route.name;
  // A session (a user token, or the operator session with an empty token).
  const [session, setSessionState] = useState(() => getSession());
  const user = session?.user ?? null;

  const signOut = () => {
    clearApiToken();
    setSessionState(null);
    window.location.reload();
  };

  return (
    <nav className="w-56 shrink-0 border-r border-border bg-panel/50 p-4 flex flex-col gap-1">
      <div className="text-sm font-semibold text-slate-200 mb-4 px-2">
        ⚙ Quant Control Centre
      </div>
      {items
        .filter((it) => !it.operatorOnly || session?.operator)
        .filter((it) => !it.userOnly || !session?.operator)
        .map((it) => (
          <button
            key={it.key}
            onClick={() => navigate(it.route)}
            className={`text-left px-2 py-2 rounded text-sm ${
              active === it.key || ((active === "strategy" || active === "new-strategy") && it.key === "strategies")
                ? "bg-accent/20 text-accent"
                : "text-slate-400 hover:bg-white/5"
            }`}
          >
            {it.label}
          </button>
        ))}
      {session ? (
        <button
          className="mt-2 text-left px-2 py-1 text-xs text-slate-500 hover:text-slate-300"
          title="Forget the session stored in this browser"
          onClick={signOut}
        >
          Sign out{user ? ` (${user.full_name || user.mobile})` : ""}
        </button>
      ) : (
        <button
          className="mt-2 text-left px-2 py-1 text-xs text-slate-500 hover:text-slate-300"
          title="Sign in as a user (or as the operator)"
          onClick={openAuth}
        >
          Sign in
        </button>
      )}
      {session?.operator && (
        <span className="px-2 text-[10px] leading-tight text-slate-600">
          Operator — sees every strategy
        </span>
      )}
      <div className="mt-auto text-[10px] text-slate-600 p-2">
        v1.2 · S3 source of truth
      </div>
    </nav>
  );
}
