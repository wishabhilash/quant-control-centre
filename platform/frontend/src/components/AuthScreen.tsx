// Sign in / sign up — the dashboard's authentication screen.
//
// The username is a mobile number; a user token (a compact HMAC-signed token)
// is stored in localStorage and sent as a bearer token on every API call.
// The admin/operator token (DEV_API_TOKEN) can be pasted here as well — it
// bypasses per-user ownership checks.

import { useState } from "react";
import { authApi, AuthResponse, setApiToken } from "../api/client";

export function AuthScreen({
  onAuthenticated,
  onCancel,
}: {
  onAuthenticated: (res: AuthResponse) => void;
  /** Provided when the screen was opened on demand (switching accounts). */
  onCancel?: () => void;
}) {
  const [mode, setMode] = useState<"signin" | "signup" | "token">("signin");
  const [fullName, setFullName] = useState("");
  const [mobile, setMobile] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    // Operator path: the admin token (DEV_API_TOKEN) is a bearer credential
    // for the platform's administrative routes. An EMPTY token is the local
    // development case — but only where the server does not require
    // authentication, so verify it before signing in.
    if (mode === "token") {
      const value = password.trim();
      setBusy(true);
      try {
        await authApi.meWithToken(value);
      } catch {
        setError(
          value
            ? "That operator token was rejected."
            : "This deployment requires an operator token — paste DEV_API_TOKEN " +
              "(an empty token only works where authentication is disabled).",
        );
        setBusy(false);
        return;
      }
      setBusy(false);
      setApiToken(value);
      onAuthenticated({
        token: value,
        operator: true,
        user: { user_id: "admin", full_name: "Operator", mobile: "" },
      });
      return;
    }
    setBusy(true);
    try {
      const res =
        mode === "signup"
          ? await authApi.signup({ full_name: fullName, mobile, password })
          : await authApi.signin({ mobile, password });
      onAuthenticated(res);
    } catch (err) {
      setError((err as Error).message ?? "Authentication failed");
    } finally {
      setBusy(false);
    }
  };

  const input =
    "w-full mt-1 rounded border border-border bg-black/40 px-3 py-2 text-sm text-slate-200 outline-none focus:border-accent";

  return (
    <div className="flex min-h-screen w-screen items-center justify-center bg-panel/40 p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-lg border border-border bg-panel p-6 shadow-2xl"
      >
        <h1 className="text-lg font-semibold text-slate-100">Quant Control Centre</h1>
        <p className="mt-1 text-xs text-slate-400">
          {mode === "signin"
            ? "Sign in with your mobile number and password."
            : mode === "signup"
              ? "Create an account — your strategies belong to you."
              : "Sign in with the operator token."}
        </p>

        {mode === "signup" && (
          <label className="block text-sm mt-4">
            <span className="text-xs text-slate-400">Full name</span>
            <input
              className={input}
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Your name"
              autoComplete="name"
            />
          </label>
        )}

        {mode !== "token" && (
          <label className="block text-sm mt-4">
            <span className="text-xs text-slate-400">Mobile number (username)</span>
            <input
              className={input}
              value={mobile}
              onChange={(e) => setMobile(e.target.value)}
              placeholder="10-digit mobile number"
              inputMode="numeric"
              autoComplete="username"
            />
          </label>
        )}

        <label className="block text-sm mt-4">
          <span className="text-xs text-slate-400">
            {mode === "token" ? "Operator token" : "Password"}
          </span>
          <input
            type="password"
            className={input}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={mode === "token" ? "DEV_API_TOKEN (empty locally)" : "Password"}
            autoComplete={mode === "signup" ? "new-password" : "current-password"}
          />
          {mode === "token" && (
            <span className="block mt-1 text-[11px] text-slate-500">
              The operator token (<span className="mono">DEV_API_TOKEN</span>) bypasses the
              per-user ownership checks. Leave it <strong>empty</strong> for local development,
              where the server does not require authentication.
            </span>
          )}
        </label>

        {error && <p className="mt-3 text-xs text-red-400">{error}</p>}

        <div className="mt-5 flex items-center gap-2">
          {onCancel && (
            <button type="button" className="btn-ghost text-sm" onClick={onCancel}>
              Cancel
            </button>
          )}
          <button type="submit" className="btn-primary flex-1" disabled={busy}>
            {busy
              ? "Please wait…"
              : mode === "signin"
                ? "Sign in"
                : mode === "signup"
                  ? "Create account"
                  : "Sign in as operator"}
          </button>
        </div>

        <button
          type="button"
          className="btn-ghost w-full mt-2 text-xs"
          onClick={() => {
            setMode(mode === "signin" ? "signup" : "signin");
            setError(null);
          }}
        >
          {mode === "signin" ? "No account? Sign up" : "Have an account? Sign in"}
        </button>

        <button
          type="button"
          className="btn-ghost w-full mt-1 text-xs"
          onClick={() => {
            setMode(mode === "token" ? "signin" : "token");
            setError(null);
          }}
        >
          {mode === "token" ? "Back to sign in" : "Operator? Sign in with a token"}
        </button>
      </form>
    </div>
  );
}
