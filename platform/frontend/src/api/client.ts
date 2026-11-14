// Typed API client. The backend lives under /api/v1 (spec section 14).
// Incoming server responses are validated at runtime where it matters
// (spec section 15), since compile-time types alone do not validate data.

import type {
  CodeBlock,
  CreateStrategyInput,
  HealthResponse,
  PublicUser,
  RuntimeStatus,
  Schedule,
  SchedulerStatus,
  StrategyDocument,
  StrategyListResponse,
  StrategySummary,
  StorageStatus,
} from "../types/strategy";

const BASE = "/api/v1";

/**
 * Fired when the API answers 401/403 — the session token is missing, expired
 * or wrong. The app listens for it and shows the sign-in screen.
 */
export const AUTH_REQUIRED_EVENT = "api:auth-required";

const SESSION_KEY = "api_session";
const TOKEN_KEY = "api_token"; // legacy key (operator token)

export interface Session {
  token: string;
  user: PublicUser;
  /** True for the operator session (an empty token — the local/dev case). */
  operator: boolean;
}

function readSession(): Session | null {
  const raw = localStorage.getItem(SESSION_KEY);
  if (raw) {
    try {
      return JSON.parse(raw) as Session;
    } catch {
      /* fall through to the legacy key */
    }
  }
  // A token stored by an earlier build → treat it as an operator session.
  const legacy = localStorage.getItem(TOKEN_KEY);
  if (legacy) {
    return {
      token: legacy,
      user: { user_id: "admin", full_name: "Operator", mobile: "" },
      operator: true,
    };
  }
  return null;
}

function token(): string {
  return readSession()?.token ?? "";
}

export function getApiToken(): string {
  return token();
}

/** The current session (null when signed out). */
export function getSession(): Session | null {
  return readSession();
}

export function isSignedIn(): boolean {
  return readSession() !== null;
}

/** The signed-in user (or null). */
export function getCurrentUser(): PublicUser | null {
  return readSession()?.user ?? null;
}

/** Store the session (sign in / sign up). An empty token = the operator. */
export function setSession(t: string, user: PublicUser, operator = false): void {
  localStorage.setItem(SESSION_KEY, JSON.stringify({ token: t, user, operator }));
}

/** Store the admin/operator token (sign in as operator). */
export function setApiToken(value: string): void {
  const v = value.trim();
  localStorage.setItem(
    SESSION_KEY,
    JSON.stringify({
      token: v,
      user: { user_id: "admin", full_name: "Operator", mobile: "" },
      operator: true,
    }),
  );
}

/** Forget the session (sign out). */
export function clearApiToken(): void {
  localStorage.removeItem(SESSION_KEY);
  localStorage.removeItem(TOKEN_KEY);
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

// --- Auth ------------------------------------------------------------------
export interface AuthResponse {
  token: string;
  user: PublicUser;
  /** The operator session (an empty token — the local development case). */
  operator?: boolean;
}

export const authApi = {
  signup: (body: { full_name: string; mobile: string; password: string }) =>
    request<AuthResponse>(`/auth/signup`, { method: "POST", body: JSON.stringify(body) }),
  signin: (body: { mobile: string; password: string }) =>
    request<AuthResponse>(`/auth/signin`, { method: "POST", body: JSON.stringify(body) }),
  me: () => request<PublicUser>(`/auth/me`),
  /** Verify a candidate operator token (used before signing in with it). */
  meWithToken: (token: string) => {
    const headers: Record<string, string> = {};
    if (token.trim()) headers.Authorization = `Bearer ${token.trim()}`;
    return request<PublicUser>(`/auth/me`, { headers });
  },
  /** Whether this deployment requires a session token. */
  status: () => request<{ auth_enabled: boolean; signup_enabled: boolean }>(`/auth/status`),
};

export const adminApi = {
  /** The platform's user accounts (operator token only). */
  users: () => request<PublicUser[]>(`/admin/users`),
  /** Strategies that belong to nobody (created before user accounts). */
  unassignedStrategies: () => request<StrategySummary[]>(`/admin/strategies/unassigned`),
  /** Assign a strategy to a user by mobile number (or null to unassign). */
  assign: (strategyId: string, mobile: string | null) =>
    request<{ strategy_id: string; owner_id: string | null }>(
      `/admin/strategies/${strategyId}/assign`,
      { method: "POST", body: JSON.stringify({ mobile }) },
    ),
};

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
  runNow: (id: string, opts: { schedule_id?: string; args?: string } = {}) =>
    request<{
      strategy_id: string;
      schedule_id: string | null;
      manual: boolean;
      args: string;
      exit_code: number;
      output: string;
    }>(
      `/strategies/${id}/run`,
      { method: "POST", body: JSON.stringify(opts) },
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
  downloadUrl: (id: string) => `${BASE}/strategies/${id}/logs/download`,  streamUrl: (id: string) => {
    const base = (import.meta.env.VITE_WS_BASE as string) || `ws://${location.host}`;
    // Browsers cannot set headers on a WebSocket handshake, so the session
    // token travels as a query parameter (the backend applies the same
    // ownership checks to the stream).
    const t = token();
    const qs = t ? `?token=${encodeURIComponent(t)}` : "";
    return `${base}/api/v1/strategies/${id}/logs/stream${qs}`;
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
