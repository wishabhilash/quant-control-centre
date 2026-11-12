/// <reference types="vite/client" />

// Runtime environment variables exposed by Vite (spec §17 dev/prod storage).
interface ImportMetaEnv {
  readonly VITE_WS_BASE?: string;
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
