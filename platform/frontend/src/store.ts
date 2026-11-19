// Minimal app state (navigation + current strategy) via zustand.
import { create } from "zustand";
import type { StrategyDocument } from "./types/strategy";

type Route =
  | { name: "dashboard" }
  | { name: "strategies" }
  | { name: "brokers" }
  | { name: "account" }
  | { name: "settings" }
  | { name: "users" }
  | { name: "tutorial" }
  | { name: "strategy"; id: string }
  | { name: "new-strategy" }
  | { name: "broker-ticks"; broker: "fyers" }
  | { name: "copy-trading" };

interface AppState {
  route: Route;
  strategy: StrategyDocument | null;
  navigate: (route: Route) => void;
  setStrategy: (s: StrategyDocument | null) => void;
  /** The sign-in / sign-up screen is open (shown on demand to switch accounts). */
  authOpen: boolean;
  openAuth: () => void;
  closeAuth: () => void;
}

export const useApp = create<AppState>((set) => ({
  route: { name: "dashboard" },
  strategy: null,
  navigate: (route) => set({ route }),
  setStrategy: (strategy) => set({ strategy }),
  authOpen: false,
  openAuth: () => set({ authOpen: true }),
  closeAuth: () => set({ authOpen: false }),
}));
