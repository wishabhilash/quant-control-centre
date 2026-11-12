import { useApp } from "./store";
import { Sidebar } from "./components/Sidebar";
import { Dashboard } from "./components/Dashboard";
import { StrategyList } from "./components/StrategyList";
import { StrategyDetail } from "./components/StrategyDetail";
import { StrategyWorkspace } from "./components/StrategyWorkspace";
import { SystemSettings } from "./pages/SystemSettings";

export default function App() {
  const route = useApp((s) => s.route);

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar />
      {route.name === "new-strategy" ? (
        <StrategyWorkspace />
      ) : (
        <main className="flex-1 overflow-auto p-6">
          {route.name === "dashboard" && <Dashboard />}
          {route.name === "strategies" && <StrategyList />}
          {route.name === "strategy" && <StrategyDetail id={route.id} />}
          {route.name === "settings" && <SystemSettings />}
        </main>
      )}
    </div>
  );
}
