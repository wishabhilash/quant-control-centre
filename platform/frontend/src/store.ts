// Minimal app state (navigation + current strategy) via zustand.
import { create } from "zustand";
import type { StrategyDocument } from "./types/strategy";

type Route =
  | { name: "dashboard" }
  | { name: "strategies" }
  | { name: "settings" }
  | { name: "strategy"; id: string }
  | { name: "new-strategy" };

interface AppState {
  route: Route;
  strategy: StrategyDocument | null;
  navigate: (route: Route) => void;
  setStrategy: (s: StrategyDocument | null) => void;
}

export const useApp = create<AppState>((set) => ({
  route: { name: "dashboard" },
  strategy: null,
  navigate: (route) => set({ route }),
  setStrategy: (strategy) => set({ strategy }),
}));
