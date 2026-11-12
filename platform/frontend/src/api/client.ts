// Typed API client. The backend lives under /api/v1 (spec section 14).
// Incoming server responses are validated at runtime where it matters
// (spec section 15), since compile-time types alone do not validate data.

import type {
  CodeBlock,
  CreateStrategyInput,
  HealthResponse,
  RuntimeStatus,
  Schedule,
  SchedulerStatus,
  StrategyDocument,
  StrategyListResponse,
  StorageStatus,
} from "../types/strategy";

const BASE = "/api/v1";

function token(): string {
  return localStorage.getItem("api_token") ?? "";
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  const t = token();
  if (t) headers.set("Authorization", `Bearer ${t}`);
  const res = await fetch(BASE + path, { ...init, headers });
  if (res.status === 204) return undefined as unknown as T;
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

// --- Strategies -----------------------------------------------------------
export const strategiesApi = {
  list: () => request<StrategyListResponse>(`/strategies`),
  get: (id: string) => request<StrategyDocument>(`/strategies/${id}`),
  create: (body: CreateStrategyInput) =>
    request<StrategyDocument>(`/strategies`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  update: (id: string, body: Partial<StrategyDocument>) =>
    request<StrategyDocument>(`/strategies/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  remove: (id: string, forceStop = false) =>
    request(`/strategies/${id}?force_stop=${forceStop}`, { method: "DELETE" }),
};

// --- Code -----------------------------------------------------------------
export const codeApi = {
  get: (id: string) =>
    request<{ strategy_id: string; entrypoint: string; files: Record<string, string>; encoding: string }>(
      `/strategies/${id}/code`
    ),
  save: (id: string, entrypoint: string, files: Record<string, string>, ifMatchEtag?: string) =>
    request<{ entrypoint: string; files: string[] }>(`/strategies/${id}/code`, {
      method: "PUT",
      body: JSON.stringify({ entrypoint, files, if_match_etag: ifMatchEtag }),
    }),
  uploadZip: (id: string, file: File) =>
    fetch(BASE + `/strategies/${id}/upload`, {
      method: "POST",
      headers: token() ? { Authorization: `Bearer ${token()}` } : {},
      body: file,
    }).then((r) => {
      if (!r.ok) throw new ApiError(r.status, "upload failed");
      return r.json();
    }),
};

// --- Deployment / cron control --------------------------------------------
// start/stop enable/disable the strategy's crons; only the cron-triggered
// runner launches a strategy run (the platform never runs it directly).
export const deployApi = {
  deploy: (id: string, waitIfRunning = false) =>
    request(`/strategies/${id}/deploy?wait_if_running=${waitIfRunning}`, { method: "POST" }),
  status: (id: string) => request<RuntimeStatus>(`/strategies/${id}/status`),
  start: (id: string) => request(`/strategies/${id}/start`, { method: "POST" }),
  stop: (id: string) => request(`/strategies/${id}/stop`, { method: "POST" }),
  restart: (id: string) => request(`/strategies/${id}/restart`, { method: "POST" }),
};

// --- Schedules ------------------------------------------------------------
export const schedulesApi = {
  list: (id: string) => request<{ schedules: Schedule[] }>(`/strategies/${id}/schedules`),
  create: (id: string, body: Omit<Schedule, "schedule_id">) =>
    request<Schedule>(`/strategies/${id}/schedules`, { method: "POST", body: JSON.stringify(body) }),
  update: (id: string, sid: string, body: Partial<Schedule>) =>
    request<Schedule>(`/strategies/${id}/schedules/${sid}`, { method: "PUT", body: JSON.stringify(body) }),
  remove: (id: string, sid: string) =>
    request(`/strategies/${id}/schedules/${sid}`, { method: "DELETE" }),
  enable: (id: string, sid: string) =>
    request<Schedule>(`/strategies/${id}/schedules/${sid}/enable`, { method: "POST" }),
  disable: (id: string, sid: string) =>
    request(`/strategies/${id}/schedules/${sid}/disable`, { method: "POST" }),
};

// --- Logs -----------------------------------------------------------------
export const logsApi = {
  get: (id: string, opts: { tail?: number; since?: number; level?: string; search?: string } = {}) => {
    const q = new URLSearchParams();
    if (opts.tail) q.set("tail", String(opts.tail));
    if (opts.since !== undefined) q.set("since", String(opts.since));
    if (opts.level) q.set("level", opts.level);
    if (opts.search) q.set("search", opts.search);
    return request<{ lines: string[]; next_offset: number; total_lines: number }>(
      `/strategies/${id}/logs?${q.toString()}`
    );
  },
  downloadUrl: (id: string) => `${BASE}/strategies/${id}/logs/download`,
  streamUrl: (id: string) => {
    const base = (import.meta.env.VITE_WS_BASE as string) || `ws://${location.host}`;
    return `${base}/api/v1/strategies/${id}/logs/stream`;
  },
};

// --- System ---------------------------------------------------------------
export const systemApi = {
  health: () => request<HealthResponse>(`/system/health`),
  storage: () => request<StorageStatus>(`/system/storage`),
  scheduler: () => request<SchedulerStatus>(`/system/scheduler`),
};
