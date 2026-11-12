import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `API_PROXY_TARGET` is a shell env var set by `make frontend`. Declare a minimal
// ambient type so this file type-checks without pulling in @types/node.
declare const process: { env: Record<string, string | undefined> };

// The backend base path (spec section 14). In dev the API is proxied to the
// FastAPI service; in production the built frontend is served by the same host.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.API_PROXY_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
      "/system": {
        target: process.env.API_PROXY_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
