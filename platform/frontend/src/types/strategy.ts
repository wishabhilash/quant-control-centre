// Shared TypeScript interfaces mirroring the backend Pydantic models.
// Runtime validation of server responses is done in api/client.ts.

export interface CodeBlock {
  encoding: "base64";
  files: Record<string, string>; // decoded text for the editor
}

export interface Configuration {
  parameters: Record<string, unknown>;
}

export interface Schedule {
  schedule_id: string;
  name: string;
  cron_expression: string;
  timezone: string;
  enabled: boolean;
  // Arguments passed to the fixed entry point (main.py) when this schedule fires.
  command: string;
}

export interface DeploymentStatus {
  status: string;
  last_deployed_at: string | null;
  source_hash: string | null;
}

export interface StrategyDocument {
  schema_version: number;
  strategy_id: string;
  name: string;
  description: string;
  enabled: boolean;
  /** Copy trading: the leader opts in; a follower's copy carries copied_from. */
  copy_trading?: boolean;
  copied_from?: CopiedFrom | null;
  code: CodeBlock;
  configuration: Configuration;
  schedules: Schedule[];
  deployment: DeploymentStatus;
  created_at: string | null;
  updated_at: string | null;
}

// Copy trading: what the follow flow returns about the created copy.
export interface CopyFollowInfo {
  strategy_id: string;
  name: string;
  enabled: boolean;
  deployment_status: string;
  schedule_count: number;
  copied_from: CopiedFrom | null;
  source_name: string | null;
  copied_at: string | null;
}

export interface CopiedFrom {
  strategy_id: string;
  owner_id: string;
  source_hash: string | null;
  copied_at: string | null;
}

// Copy trading: the Universe entry (metadata only — source stays private).
export interface UniverseEntry {
  strategy_id: string;
  name: string;
  description: string;
  owner_name: string;
  followers: number;
  /** True when the author has deployed the strategy at least once —
   *  a never-deployed publish is allowed (the author may write FOR
   *  others) but disclosed. */
  author_deployed?: boolean;
  /** True for the viewer's own listing — visible, never followable. */
  is_mine?: boolean;
  /** True when the viewer already follows it — show 'following',
   *  never a second Follow button. */
  is_followed?: boolean;
}

export interface StrategySummary {
  strategy_id: string;
  name: string;
  description: string;
  enabled: boolean;
  deployment_status: string;
  schedule_count: number;
  next_scheduled_execution: string | null;
  last_deployed_at: string | null;
  updated_at: string | null;
  copy_trading?: boolean;
  copied?: boolean;
  source_name?: string | null;
}

export interface StrategyListResponse {
  strategies: StrategySummary[];
  count: number;
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

// Flat request body accepted by POST /strategies (mirrors backend StrategyCreate).
export interface CreateStrategyInput {
  name: string;
  description?: string;
  enabled?: boolean;
  parameters?: Record<string, unknown>;
  files?: Record<string, string>;
}

export interface RuntimeStatus {
  strategy_id: string;
  state: string;
  process: {
    strategy_id: string;
    schedule_id: string | null;
    state: string;
    pid: number | null;
    started_at: string | null;
    ended_at: string | null;
    exit_code: number | null;
  } | null;
  has_live_positions_guard: boolean;
  deployment_status: string;
  deployed_source_hash: string | null;
  updated_at: string | null;
}

export interface PublicUser {
  user_id: string;
  full_name: string;
  mobile: string;
}

/** A user's own account, as shown in Account Settings (never the hash). */
export interface AccountInfo extends PublicUser {
  status: "active" | "blocked";
  created_at: string | null;
  last_login_at: string | null;
  strategy_count: number;
}

/** The operator's one-time disclosure of a freshly reset password. */
export interface PasswordResetResponse {
  user: AdminUser;
  new_password: string;
}

/** A user as seen by the operator (administrative view). */
export interface AdminUser extends PublicUser {
  status: "active" | "blocked";
  created_at: string | null;
  last_login_at: string | null;
  blocked_at: string | null;
  blocked_by: string | null;
  strategy_count: number;
}

// --- Broker connections -----------------------------------------------------
export type BrokerName = "dhan" | "fyers";
export type BrokerStatus = "connecting" | "connected" | "failed";

/** What the API tells the UI about a connection (never a credential). */
export interface BrokerInfo {
  broker: BrokerName;
  status: BrokerStatus;
  enabled_at: string | null;
  last_token_at: string | null;
  last_error: string | null;
  next_refresh_at: string | null;
}

export interface BrokerListResponse {
  brokers: BrokerInfo[];
}

export interface DhanEnableInput {
  totp_secret: string;
  client_id: string;
  pin: string;
}

export interface FyersEnableInput {
  username: string;
  client_id: string;
  secret_key: string;
  totp_key: string;
  pin: string;
}

// --- Tick data (per-account market-data feed) --------------------------------
/** The feed's live state (derived from the worker, never persisted). */
export type TickFeedState = "starting" | "connected" | "reconnecting"
  | "stopped" | "error";

/** What the API tells the UI about a tick feed (never a credential). */
export interface TickFeedStatus {
  broker: BrokerName;
  enabled: boolean;
  subscriptions: string[];
  /** Provenance: symbol → consumers holding it ("dashboard", strategy ids).
   *  The page's ✕ removes only the dashboard's claim — never a strategy's. */
  claims: Record<string, string[]>;
  state: TickFeedState | null;
  last_error: string | null;
  ticks_published: number;
  last_tick_at: string | null;
  last_connect_at: string | null;
}

/** One line of the live tick stream (the unified tick contract, v1). */
export interface TickEvent {
  v: number;
  type: "tick";
  broker: string;
  instrument: { exchange: string | null; symbol: string };
  ltp: number | null;
  ltq: number | null;
  ohlcv: Record<string, number | null>;
  top: Record<string, number | null>;
  depth: unknown;
  oi: number | null;
  ts_exchange: number | null;
  ts_gateway: number;
  raw: Record<string, unknown>;
}

export interface HealthResponse {
  status: string;
  environment: string;
  version: string;
  checks: Record<string, string>;
}

export interface StorageStatus {
  reachable: boolean;
  bucket: string;
  region: string;
  endpoint_url: string | null;
  detail: string | null;
}

export interface SchedulerStatus {
  active: boolean;
  mode: string;
  managed_schedule_count: number;
  /** Schedule jobs currently registered with the in-process APScheduler. */
  scheduled_jobs: number;
  /** SQLite job store the scheduler persists its bookkeeping to (null in dry-run). */
  job_store: string | null;
  detail: string | null;
  /** Effective paths the scheduled jobs invoke the runner with (PLATFORM_ROOT-derived). */
  runner_python?: string | null;
  runner_script?: string | null;
  strategies_dir?: string | null;
}

// A local project file in the editor (path -> text).
export interface ProjectFile {
  path: string;
  content: string;
  language: string;
}

// A node in the file explorer tree.
export interface TreeNode {
  name: string;
  path: string;
  type: "file" | "folder";
  children?: TreeNode[];
  file?: ProjectFile;
}

// --- Runtime files -------------------------------------------------------------
// Files the strategy wrote into its own directory at run time. Mirrors the
// backend models; they are shown read-only and are NEVER part of the S3
// project (golden rule 1).
export interface RuntimeFileInfo {
  path: string;
  size_bytes: number;
  modified_at?: string | null;
}

export interface RuntimeFilesResponse {
  strategy_id: string;
  files: RuntimeFileInfo[];
}

export interface RuntimeFileContent {
  strategy_id: string;
  path: string;
  encoding: "utf-8" | "base64";
  truncated: boolean;
  content: string;
}
