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
  code: CodeBlock;
  configuration: Configuration;
  schedules: Schedule[];
  deployment: DeploymentStatus;
  created_at: string | null;
  updated_at: string | null;
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
}

export interface StrategyListResponse {
  strategies: StrategySummary[];
  count: number;
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
  cron_file: string | null;
  detail: string | null;
  /** Effective paths used in the generated cron lines (PLATFORM_ROOT-derived). */
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
