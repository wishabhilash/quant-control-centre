import { useApp } from "./store";
import { Sidebar } from "./components/Sidebar";
import { Dashboard } from "./components/Dashboard";
import { StrategyList } from "./components/StrategyList";
import { StrategyWorkspace } from "./components/StrategyWorkspace";
import { SystemSettings } from "./pages/SystemSettings";

export default function App() {
  const route = useApp((s) => s.route);

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
        {route.name === "settings" && <SystemSettings />}
      </main>
    </div>
  );
}
