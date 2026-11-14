// Sign-in gate for the admin API.
//
// The backend protects every admin route with a bearer token
// (`AUTH_ENABLED` + `DEV_API_TOKEN`); there is no user database yet. When the
// API answers 401/403 the gate prompts for that token, stores it in
// localStorage (`api_token`) and reloads so every view re-fetches with it.
//
// The gate is server-driven: it only appears after the API actually rejects a
// request, so a development setup with auth disabled never shows it.

import { useEffect, useState } from "react";
import { AUTH_REQUIRED_EVENT, setApiToken } from "../api/client";

export function SignInGate() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onRequired = () => {
      setOpen(true);
      setError(null);
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, onRequired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onRequired);
  }, []);

  if (!open) return null;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const v = value.trim();
    if (!v) {
      setError("Enter the admin token.");
      return;
    }
    setApiToken(v);
    // A reload is the simplest way to re-run every view's fetches with the
    // new token (and it re-checks the token against the API).
    window.location.reload();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-md rounded-lg border border-border bg-panel p-6 shadow-2xl"
      >
        <h2 className="text-base font-semibold text-slate-100">Admin token required</h2>
        <p className="mt-2 text-sm text-slate-400">
          The API rejected this request. Paste the platform&apos;s admin token
          (<span className="mono">DEV_API_TOKEN</span> from{" "}
          <span className="mono">/etc/quant/backend.env</span> on the server). It is
          stored in this browser&apos;s local storage and sent as a bearer token.
        </p>
        <input
          type="password"
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Admin token"
          className="mono mt-4 w-full rounded border border-border bg-black/40 px-3 py-2 text-sm text-slate-200 outline-none focus:border-accent"
        />
        {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            className="btn-ghost text-sm"
            onClick={() => {
              setOpen(false);
              setValue("");
            }}
          >
            Cancel
          </button>
          <button type="submit" className="btn text-sm">
            Sign in
          </button>
        </div>
      </form>
    </div>
  );
}
