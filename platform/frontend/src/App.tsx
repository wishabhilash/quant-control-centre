import { useEffect, useState } from "react";
import { useApp } from "./store";
import { Sidebar } from "./components/Sidebar";
import { Dashboard } from "./components/Dashboard";
import { StrategyList } from "./components/StrategyList";
import { StrategyWorkspace } from "./components/StrategyWorkspace";
import { AuthScreen } from "./components/AuthScreen";
import { BrokersPage } from "./pages/BrokersPage";
import { SystemSettings } from "./pages/SystemSettings";
import { UsersPage } from "./pages/UsersPage";
import {
  AUTH_REQUIRED_EVENT,
  AuthResponse,
  authApi,
  getApiToken,
  isSignedIn,
  setSession,
} from "./api/client";

export default function App() {
  const route = useApp((s) => s.route);
  const authOpen = useApp((s) => s.authOpen);
  const closeAuth = useApp((s) => s.closeAuth);
  // No session yet → the sign-in / sign-up screen. The session is kept in
  // localStorage, so a reload does not sign the user out. Where the server
  // does not require authentication (local development), sign in with an EMPTY
  // operator token.
  const [authed, setAuthed] = useState(() => isSignedIn());

  // A 401/403 from any API call means the session is gone (expired token,
  // or the account was removed) → back to the sign-in screen.
  useEffect(() => {
    const onAuthRequired = () => setAuthed(false);
    window.addEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
  }, []);

  if (!authed || authOpen) {
    return (
      <AuthScreen
        onAuthenticated={(res: AuthResponse) => {
          setSession(res.token, res.user, res.operator);
          setAuthed(true);
          closeAuth();
        }}
        onCancel={authOpen && authed ? closeAuth : undefined}
      />
    );
  }

  // Create and edit share ONE full-page workspace (spec amendment v1.3 §1).
  // A `strategy` route opens the same workspace pre-populated from S3; a
  // `new-strategy` route opens it empty. There is no separate edit page,
  // creation modal or scheduling page.
  if (route.name === "new-strategy" || route.name === "strategy") {
    return (
      <div className="flex h-screen w-screen overflow-hidden">
        <Sidebar />
        <StrategyWorkspace id={route.name === "strategy" ? route.id : undefined} />
      </div>
    );
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-auto p-6">
        {route.name === "dashboard" && <Dashboard />}
        {route.name === "strategies" && <StrategyList />}
        {route.name === "brokers" && <BrokersPage />}
        {route.name === "settings" && <SystemSettings />}
        {route.name === "users" && <UsersPage />}
      </main>
    </div>
  );
}
