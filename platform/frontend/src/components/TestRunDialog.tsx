// "Test run" dialog — run the strategy once, on demand, with the arguments you
// type here.
//
// The run goes through exactly the same path as cron (the runner: state checks,
// lock, the strategy's own venv, the timeout), so what you see is what a
// scheduled run does — only the arguments are yours to choose. The args are
// passed to the entry point as an argument array (no shell), overriding the
// schedule's own args for this run only.

import { useState } from "react";
import { deployApi } from "../api/client";

export interface TestRunSchedule {
  schedule_id: string;
  name: string;
  command: string;
  enabled: boolean;
}

export function TestRunDialog({
  strategyId,
  schedules,
  initialScheduleId,
  onClose,
}: {
  strategyId: string;
  schedules: TestRunSchedule[];
  initialScheduleId?: string | null;
  onClose: () => void;
}) {
  const first = schedules.find((s) => s.enabled) ?? schedules[0];
  const [scheduleId, setScheduleId] = useState<string>(
    initialScheduleId ?? first?.schedule_id ?? "",
  );
  const selected = schedules.find((s) => s.schedule_id === scheduleId);
  // Prefill with the chosen schedule's args; the user can edit them.
  const [args, setArgs] = useState<string>(selected?.command ?? "");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<{ output: string; exitCode: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const pickSchedule = (id: string) => {
    setScheduleId(id);
    const s = schedules.find((x) => x.schedule_id === id);
    setArgs(s?.command ?? "");
    setResult(null);
  };

  const run = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const res = await deployApi.runNow(strategyId, {
        schedule_id: scheduleId || undefined,
        args: args.trim() || undefined,
      });
      setResult({ output: res.output?.trim() || `exit ${res.exit_code}`, exitCode: res.exit_code });
    } catch (e) {
      setError((e as Error)?.message ?? "the test run failed");
    } finally {
      setRunning(false);
    }
  };

  const input =
    "w-full mt-1 rounded border border-border bg-black/40 px-3 py-2 text-sm mono text-slate-200 outline-none focus:border-accent";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg rounded-lg border border-border bg-panel p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-base font-semibold text-slate-100">Test run</h2>
        <p className="mt-1 text-xs text-slate-400">
          Runs the strategy once, right now, through the same path cron uses
          (state checks, lock, the strategy&apos;s own venv, the timeout). The
          arguments below are passed to <span className="mono">main.py</span> for
          this run only.
        </p>

        {schedules.length > 0 && (
          <label className="block text-sm mt-4">
            <span className="text-xs text-slate-400">Schedule</span>
            <select
              className={input}
              value={scheduleId}
              onChange={(e) => pickSchedule(e.target.value)}
            >
              {schedules.map((s) => (
                <option key={s.schedule_id} value={s.schedule_id}>
                  {s.name || s.schedule_id}
                  {s.enabled ? "" : " (disabled)"}
                </option>
              ))}
            </select>
          </label>
        )}

        <label className="block text-sm mt-4">
          <span className="text-xs text-slate-400">Arguments</span>
          <input
            className={input}
            value={args}
            onChange={(e) => setArgs(e.target.value)}
            placeholder="--slot 0310 --mode live"
            spellCheck={false}
          />
          <span className="block mt-1 text-[11px] text-slate-500">
            Same syntax as the schedule&apos;s <span className="mono">Args</span> —{" "}
            <span className="mono">--key value</span> pairs (or a bare{" "}
            <span className="mono">--flag</span>). The strategy reads them from{" "}
            <span className="mono">sys.argv</span>; leave empty to run without
            arguments.
          </span>
        </label>

        {error && <p className="mt-3 text-xs text-red-400">{error}</p>}

        {result && (
          <div className="mt-4 rounded border border-border bg-black/40 p-3">
            <div className="text-[10px] uppercase tracking-wider text-slate-500">
              Runner output
            </div>
            <pre className="mono mt-1 text-[11px] leading-relaxed text-slate-300 whitespace-pre-wrap break-all">
              {result.output}
            </pre>
            <p className="mt-2 text-[11px] text-slate-500">
              The strategy keeps running in the background — watch its output in
              the Logs tab.
            </p>
          </div>
        )}

        <div className="mt-5 flex items-center justify-end gap-2">
          <button className="btn-ghost text-sm" onClick={onClose}>
            {result ? "Close" : "Cancel"}
          </button>
          <button className="btn text-sm" onClick={run} disabled={running}>
            {running ? "Running…" : "Run"}
          </button>
        </div>
      </div>
    </div>
  );
}
