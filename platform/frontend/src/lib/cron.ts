// Live preview of what the platform schedules for a strategy schedule, plus the
// second-stage launch command.
//
// Mirrors `StrategyScheduler.run_schedule` (stage 1) and `process_service._launch`
// (stage 2) in platform/backend. Schedules are registered with an in-process
// APScheduler whose jobs invoke the generic runner — the exact command is
// previewed here. Runner/strategy paths come from the settings-derived values
// exposed by GET /system/scheduler, with the spec §4 EC2 layout
// (PLATFORM_ROOT=/opt/quant) as fallback. KEEP THIS FILE IN SYNC if the backend
// job body changes.

export const DEFAULT_TZ = "Asia/Kolkata";
export const DEFAULT_CRON = "20 9 * * 1-5";

// Default runner paths (spec §4 EC2 layout, PLATFORM_ROOT=/opt/quant). The
// workspace passes the settings-derived paths from GET /system/scheduler so the
// preview matches the backend's actually invoked command in every environment.
const VENV_PYTHON = "/opt/quant/platform/backend/venv/bin/python";
const RUNNER = "/opt/quant/platform/backend/scripts/runner.py";
const STRATEGIES_DIR = "/opt/quant/strategies";

export interface CronPreview {
  /** Stage 1: the command the scheduler's job body executes (the runner). */
  line: string;
  /** Stage 2: the command the runner then launches — the strategy's OWN venv
   *  interpreter running main.py with the schedule's args (process_service._launch). */
  runs: string;
}

export function buildCronPreview(input: {
  /** Server-assigned STR-NNN id; null/undefined while the strategy is unsaved. */
  strategyId?: string | null;
  /** Server-assigned SCH-NNN id; null/undefined while the schedule is unsaved. */
  scheduleId?: string | null;
  cronExpression: string;
  timezone: string;
  command?: string;
  /** Effective backend paths (from /system/scheduler); falls back to the
   *  spec §4 EC2 layout when unavailable. */
  paths?: {
    runner_python?: string | null;
    runner_script?: string | null;
    strategies_dir?: string | null;
  };
}): CronPreview {
  const cron = input.cronExpression.trim() || DEFAULT_CRON;
  const strategyId = input.strategyId || "STR-???"; // assigned on save
  const scheduleId = input.scheduleId || "SCH-???"; // assigned on save
  const venvPython = input.paths?.runner_python || VENV_PYTHON;
  const runner = input.paths?.runner_script || RUNNER;
  const strategiesDir = input.paths?.strategies_dir || STRATEGIES_DIR;
  // The runner's stdout/stderr lands in the strategy's own directory so the
  // output of different strategies never mixes.
  const logPath = `${strategiesDir}/${strategyId}/runner.log`;
  // Stage 1: what the scheduler's job body runs (mirrors run_schedule). The
  // schedule's timezone is applied by the APScheduler trigger, not on the line.
  const runnerCmd = `${venvPython} ${runner} --strategy-id ${strategyId} --schedule-id ${scheduleId} >> ${logPath} 2>&1`;
  // The schedule's args are NOT part of the command: the runner reads them
  // from S3 at fire time and appends them to the fixed main.py entry point.
  const args = (input.command ?? "").trim();
  // Stage 2 mirrors process_service._launch exactly: the strategy runs on its
  // OWN venv interpreter ({strategies}/{id}/.venv/bin/python) with main.py and
  // the schedule's args — the platform venv only ever runs the runner itself.
  const strategyDir = `${strategiesDir}/${strategyId}`;
  const launchCmd =
    `${strategyDir}/.venv/bin/python ${strategyDir}/main.py${args ? ` ${args}` : ""}`;
  return {
    line: `${cron} ${runnerCmd}`,
    runs: launchCmd,
  };
}
