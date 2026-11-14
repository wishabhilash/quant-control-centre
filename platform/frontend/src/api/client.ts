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

/**
 * Fired when the API answers 401/403 — the admin token is missing or wrong.
 * The sign-in gate listens for it and prompts for the token.
 */
export const AUTH_REQUIRED_EVENT = "api:auth-required";

function token(): string {
  return localStorage.getItem("api_token") ?? "";
}

export function getApiToken(): string {
  return token();
}

/** Store the admin token (sign in). */
export function setApiToken(value: string): void {
  const v = value.trim();
  if (v) localStorage.setItem("api_token", v);
  else localStorage.removeItem("api_token");
}

/** Forget the admin token (sign out). */
export function clearApiToken(): void {
  localStorage.removeItem("api_token");
}

function notifyAuthRequired(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(AUTH_REQUIRED_EVENT));
  }
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
    if (res.status === 401 || res.status === 403) notifyAuthRequired();
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
  // Load the document together with its S3 ETag so the workspace can use
  // conditional (If-Match) writes to detect concurrent edits (spec 8).
  getWithEtag: async (
    id: string
  ): Promise<{ doc: StrategyDocument; etag: string | null }> => {
    const headers = new Headers();
    const t = token();
    if (t) headers.set("Authorization", `Bearer ${t}`);
    const res = await fetch(BASE + `/strategies/${id}`, { headers });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
      } catch {
        /* ignore */
      }
      if (res.status === 401 || res.status === 403) notifyAuthRequired();
      throw new ApiError(res.status, detail);
    }
    const doc = (await res.json()) as StrategyDocument;
    return { doc, etag: res.headers.get("ETag") };
  },
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
  save: (id: string, files: Record<string, string>, ifMatchEtag?: string) =>
    request<{ entrypoint: string; files: string[] }>(`/strategies/${id}/code`, {
      method: "PUT",
      body: JSON.stringify({ files, if_match_etag: ifMatchEtag }),
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
  /**
   * Trigger a run now through the same path cron uses. Returns the runner's
   * verdict (STARTED / SKIP / ERROR) so a failed tick is visible immediately
   * instead of only in runner.log.
   */
  runNow: (id: string, scheduleId?: string) =>
    request<{ strategy_id: string; schedule_id: string; exit_code: number; output: string }>(
      `/strategies/${id}/run`,
      { method: "POST", body: JSON.stringify(scheduleId ? { schedule_id: scheduleId } : {}) },
    ),
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
  /** The upcoming fire times of a cron expression (before it is saved). */
  cronPreview: (cron: string, timezone: string, count = 5) =>
    request<{ cron: string; timezone: string; runs: string[] }>(
      `/system/scheduler/preview?cron=${encodeURIComponent(cron)}` +
      `&timezone=${encodeURIComponent(timezone)}&count=${count}`,
    ),
};
