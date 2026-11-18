// Tutorial — a guided walkthrough for signed-in users: create an example
// strategy (the platform's own starter project), see how configuration,
// schedules and deploy fit together, and jump straight into the workspace.
//
// There is NO sign-up step here: this page only renders for an authenticated
// session (App.tsx gates every route on the session), so the flow starts from
// the example strategy itself. Re-opening the tutorial never creates a second
// example — it finds the one it created before and offers to open it.
import { useEffect, useState } from "react";
import { useApp } from "../store";
import { getCurrentUser, strategiesApi } from "../api/client";
import { STARTER_FILES } from "../components/StrategyWorkspace";
import type { PublicUser } from "../types/strategy";

// The name the tutorial's example strategy is created with (and re-found by).
const EXAMPLE_NAME = "My first strategy";

// The one-by-one walkthrough: how values reach the strategy's `config` dict.
// Shape verified against process_service._write_runtime_config (the runtime
// config JSON) and the starter's load_config() argv parsing.
const PARTS: { title: string; story: string; runtime: string; code: string }[] = [
  {
    title: "Parameters (Configuration tab)",
    story:
      "Anything you type in the Configuration tab's Parameters (a JSON object, " +
      "e.g. {\"symbol\": \"RELIANCE\", \"qty\": 5}) is stored in the strategy " +
      "document and written into the runtime config under configuration.parameters " +
      "on every launch. The starter's load_config() copies it into config — these " +
      "are your strategy's settings, the same on every run until you edit them.",
    runtime: [
    "{",
    "  \"configuration\": {",
    "    \"parameters\": { \"symbol\": \"RELIANCE\", \"qty\": 5 }",
    "  },",
    "  ...",
    "}",
    ].join("\n"),
    code: [
    "def run(config):",
    "    symbol = config[\"symbol\"]      # \"RELIANCE\"",
    "    qty = config[\"qty\"]            # 5",
    ].join("\n"),
  },
  {
    title: "Cron arguments (a schedule's command)",
    story:
      "Each schedule has an optional command — a plain string of CLI arguments " +
      "split into an argument array (no shell). The scheduled run executes " +
      "python main.py <those args>, and the starter's load_config() parses them " +
      "AFTER the parameters — so a schedule arg with the same key wins. Use it " +
      "to vary one strategy per schedule: same code, different instructions.",
    runtime: [
    "# schedule's command field:",
    "--symbol TCS --qty 10 --dry-run",
    "",
    "# becomes the process argv: [\"main.py\", \"--symbol\", \"TCS\",",
    "#                             \"--qty\", \"10\", \"--dry-run\"]",
    ].join("\n"),
    code: [
    "def run(config):",
    "    config[\"symbol\"]   # \"TCS\"   (arg over the parameter \"RELIANCE\")",
    "    config[\"qty\"]      # 10",
    "    config[\"dry_run\"]  # True    (a flag with no value → True)",
    "",
    "# so: --key value → config[key] = value",
    "#     --flag      → config[flag] = True",
    ].join("\n"),
  },
  {
    title: "Broker tokens (Brokers tab)",
    story:
      "Connect a broker once on the Brokers tab — the platform keeps the " +
      "credentials in SSM Parameter Store (never in your project) and regenerates " +
      "the access token daily. The fresh token is injected into the runtime " +
      "config's brokers section on EVERY launch, so a run always picks up the " +
      "latest one. Never hard-code a token — it expires within a day.",
    runtime: [
    "{",
    "  \"brokers\": {",
    "    \"dhan\": {",
    "      \"client_id\": \"1101234567\",",
    "      \"access_token\": \"eyJhbGciOi...\",   # refreshed daily at 08:00 IST",
    "      \"generated_at\": \"2026-11-16T02:30:00+00:00\"",
    "    }",
    "  }",
    "}",
    "",
    "# {} when no broker is connected — always read it defensively.",
    ].join("\n"),
    code: [
    "def run(config):",
    "    dhan = config.get(\"brokers\", {}).get(\"dhan\")",
    "    if not dhan:",
    "        print(\"no broker connected — connect one on the Brokers tab\")",
    "        return",
    "    token = dhan[\"access_token\"]   # fresh, injected at launch",
    "    client_id = dhan[\"client_id\"]",
    ].join("\n"),
  },
  {
    title: "Live tick data (quant_client)",
    story:
      "Brokers with tick data enabled (Brokers tab → the Fyers card) give your " +
      "strategy a live market-data feed — the platform runs ONE websocket per " +
      "account and your code consumes it through the quant_client library, " +
      "which the platform installs into your venv automatically (never list it " +
      "in requirements.txt). Ensure subscribes (idempotent), stream yields " +
      "ticks with staleness enforcement, and exiting the with-block " +
      "UNSUBSCRIBES what you ensured — subscriptions are claimed per strategy, " +
      "so the dashboard shows who holds each symbol and nobody drops yours.",
    runtime: [
    "{",
    "  \"strategy_id\": \"STR-002\",",
    "  \"data\": {",
    "    \"gateway\": {",
    "      \"fyers\": {",
    "        \"transport\": \"redis-pubsub\",",
    "        \"url\": \"redis://localhost:6379/0\",",
    "        \"channel\": \"ticks:<your-user>:fyers\",",
    "        \"subscriptions\": [\"NSE:SBIN-EQ\"]",
    "      }",
    "    }",
    "  }",
    "}",
    "",
    "# {} when the feed is not enabled — always read it defensively.",
    ].join("\n"),
    code: [
    "from quant_client import TickFeed, TickStaleError",
    "",
    "def run(config):",
    "    try:",
    "        with TickFeed(config) as feed:   # finds config[\"data\"]",
    "            feed.ensure([\"NSE:SBIN-EQ\"])  # subscribe (idempotent)",
    "            for tick in feed.stream(max_age=10):",
    "                symbol = tick[\"instrument\"][\"symbol\"]",
    "                ltp = tick[\"ltp\"]",
    "                # every tick carries ts_exchange + ts_gateway",
    "    except TickStaleError:",
    "        # feed older than 10s (or silent) → do NOT trade on it.",
    "        # The context exit also unsubscribes what you ensured.",
    "        print(\"tick feed stale — standing down\")",
    ].join("\n"),
  },
  {
    title: "See it run: a schedule every minute",
    story:
      "Now watch it work. (1) Open the example in the workspace → Schedules tab " +
      "→ add a schedule named \"every-minute\" with the cron expression " +
      "* * * * * (five fields = every minute) and the default timezone " +
      "Asia/Kolkata. Give it the command --note hello — that lands in the " +
      "printed config, so the log proves the arg reached your code. (2) The " +
      "runner SKIPS a disabled or not-deployed strategy, so first flip the " +
      "strategy's enabled toggle ON and click Deploy. (3) Wait up to a minute, " +
      "then open the Logs tab — it polls every 3 s, so the lines appear live.",
    runtime: [
    "# cron: * * * * *   → every minute (preview shows the next runs)",
    "",
    "# runner.log — the scheduler's own record each tick:",
    "STARTED pid=12345 strategy=STR-00N args='--note hello'",
    "",
    "# strategy.log — your own print(), once a minute:",
    "strategy started {'symbol': 'RELIANCE', 'qty': 5, 'note': 'hello', ...}",
    "",
    "# No ticks at all? The scheduler may be in dry-run (SCHEDULER_DRY_RUN=",
    "# true in the backend env stops all firing). This install has it false;",
    "# System Settings shows the scheduler's live status.",
    ].join("\n"),
    code: [
    "# Run-time state: write inside your own strategy folder (open(\"state.json\")).",
    "# Caveat: deploy wipes files that aren't part of your saved project — for",
    "# state that must survive deploys use .venv/share/ (per-strategy, excluded",
    "# from sync). Never write with ../ — it lands in the shared strategies root",
    "# where every other strategy can read and clobber it.",
    "",
    "# In the workspace's Schedules tab you'll also see the next-runs preview",
    "# (server-side croniter) — the same list EventBridge shows, so a wrong",
    "# expression is visible before it ever fires.",
    "",
    "# To stop the every-minute run: toggle the schedule off (or Stop the",
    "# strategy). Stop never kills a run in progress — it just deregisters",
    "# the schedule's future ticks.",
    ].join("\n"),
  },
];

export function TutorialPage() {
  const navigate = useApp((s) => s.navigate);
  // The session is guaranteed (App.tsx only renders pages when signed in);
  // getCurrentUser() reads it synchronously from localStorage.
  const [user] = useState<PublicUser | null>(() => getCurrentUser());

  // Which step of the walkthrough is on screen.
  //  1 = intro + create   2 = the created example + what to do next
  const [step, setStep] = useState<1 | 2>(1);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // The example strategy once it exists (found or just created).
  const [exampleId, setExampleId] = useState<string | null>(null);
  // Which walkthrough part (0..4) is showing:
  // parameters → cron args → broker tokens → tick data → see it run every
  // minute.
  const [part, setPart] = useState(0);

  // On entry: if the tutorial already created its example strategy before,
  // continue from step 2 with it instead of creating a duplicate.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const list = await strategiesApi.list();
        const found = list.strategies.find((s) => s.name === EXAMPLE_NAME);
        if (alive && found) {
          setExampleId(found.strategy_id);
          setStep(2);
        }
      } catch {
        /* the intro step's create button will surface any real problem */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // Create the example strategy from the platform's own starter project —
  // the exact same files a `+ New strategy` workspace starts from. It is
  // created disabled with no schedules: nothing runs until the user deploys
  // it and adds a schedule deliberately.
  const createExample = async () => {
    setBusy(true);
    setErr(null);
    try {
      const created = await strategiesApi.create({
        name: EXAMPLE_NAME,
        description:
          "Created by the Tutorial — the starter project, ready to edit, deploy and schedule.",
        enabled: false, // disabled until the user deploys + enables it themselves
        parameters: {},
        files: STARTER_FILES,
      });
      setExampleId(created.strategy_id);
      setStep(2);
    } catch (e) {
      setErr((e as Error)?.message ?? "could not create the example strategy");
    } finally {
      setBusy(false);
    }
  };

  if (!user) {
    // Practically unreachable (App.tsx gates on the session) — kept as a guard.
    return (
      <div className="max-w-3xl space-y-4">
        <div className="badge badge-err">
          You are not signed in. Use “Sign in” in the sidebar first.
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-xl font-semibold">Tutorial</h1>
        <p className="text-sm text-slate-400">
          Welcome{user.full_name ? `, ${user.full_name}` : ""} — let's set up your
          first example strategy.
        </p>
      </div>

      {step === 1 && (
        <>
          <div className="panel p-4 space-y-3">
            <h3 className="text-sm font-medium">What you'll build</h3>
            <p className="text-sm text-slate-400">
              One example strategy: a Python project with a{" "}
              <span className="mono">main.py</span> entry point. The platform stores
              it as a single JSON document in S3, runs it with its{" "}
              <em>own</em> Python virtual environment, and fires it on the cron
              schedules you give it (in-process APScheduler — nothing external to
              maintain).
            </p>
            <ul className="text-sm text-slate-400 list-disc pl-5 space-y-1">
              <li>
                <strong>Code</strong> — edit files in the built-in Monaco editor (the
                entry point is fixed to <span className="mono">main.py</span>).
              </li>
              <li>
                <strong>Configuration</strong> — name, description and the parameters
                your strategy reads at run time.
              </li>
              <li>
                <strong>Live tick data</strong> — enable your broker's feed on the
                Brokers tab and consume it with the built-in{" "}
                <span className="mono">quant_client</span> library (the
                walkthrough covers it).
              </li>
              <li>
                <strong>Schedules</strong> — cron expressions (default timezone Asia/
                Kolkata) with optional CLI arguments.
              </li>
              <li>
                <strong>Deploy</strong> — one click: the platform creates the
                strategy's own <span className="mono">.venv</span>, installs{" "}
                <span className="mono">requirements.txt</span>, smoke-tests{" "}
                <span className="mono">main.py</span> and syncs the project.
              </li>
            </ul>
            <p className="text-xs text-slate-500">
              The example is created <strong>disabled</strong> — nothing runs until
              you deploy it and enable a schedule yourself.
            </p>
          </div>

          {err && <div className="badge badge-err">{err}</div>}

          <button className="btn-primary" disabled={busy} onClick={createExample}>
            {busy ? "Creating…" : "Create the example strategy"}
          </button>
        </>
      )}

      {step === 2 && (
        <>
          <div className="panel p-4 space-y-3">
            <h3 className="text-sm font-medium">
              Done — your example strategy is ready
              {exampleId ? (
                <span className="mono text-slate-500"> · {exampleId}</span>
              ) : null}
            </h3>
            <p className="text-sm text-slate-400">
              It starts from the platform's starter project. On every scheduled run
              the platform writes a <em>runtime config</em> JSON into the strategy's
              own <span className="mono">.venv/share/</span> and points the process
              at it via the <span className="mono">QUANT_RUNTIME_CONFIG</span> env
              var. Three different things flow into your{" "}
              <span className="mono">config</span> dict from it — we'll take them one
              at a time, then schedule the example to run every minute and watch it
              happen in the logs.
            </p>
          </div>

          <div className="panel p-4 space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium">
                {part + 1} / {PARTS.length} · {PARTS[part].title}
              </h3>
              <div className="flex gap-1">
                {PARTS.map((p, i) => (
                  <button
                    key={p.title}
                    onClick={() => setPart(i)}
                    className={`w-2.5 h-2.5 rounded-full ${
                      i === part ? "bg-accent" : "bg-slate-600 hover:bg-slate-500"
                    }`}
                    title={p.title}
                  />
                ))}
              </div>
            </div>

            <p className="text-sm text-slate-400">{PARTS[part].story}</p>

            <div>
              <div className="text-[11px] uppercase tracking-wider text-slate-500 mb-1">
                What it looks like at run time
              </div>
              <pre className="bg-black/40 rounded p-3 text-xs text-slate-300 overflow-auto">
                {PARTS[part].runtime}
              </pre>
            </div>

            <div>
              <div className="text-[11px] uppercase tracking-wider text-slate-500 mb-1">
                {part === PARTS.length - 1
                  ? "Good to know"
                  : "How to expect it in your code"}
              </div>
              <pre className="bg-black/40 rounded p-3 text-xs text-slate-300 overflow-auto">
                {PARTS[part].code}
              </pre>
            </div>

            <div className="flex items-center gap-2 pt-1">
              <button
                className="btn-ghost text-xs"
                disabled={part === 0}
                onClick={() => setPart(part - 1)}
              >
                ← Back
              </button>
              {part < PARTS.length - 1 ? (
                <button className="btn-primary text-xs" onClick={() => setPart(part + 1)}>
                  Next: {PARTS[part + 1].title} →
                </button>
              ) : (
                <span className="text-xs text-slate-500">
                  That's the full loop — code → config → brokers → ticks →
                  schedule → live logs.
                </span>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              className="btn-primary"
              disabled={!exampleId}
              onClick={() =>
                exampleId && navigate({ name: "strategy", id: exampleId })
              }
            >
              Open it in the workspace
            </button>
            <button
              className="btn-ghost"
              onClick={() => navigate({ name: "strategies" })}
            >
              Go to Strategies
            </button>
          </div>
          <p className="text-xs text-slate-500">
            Re-opening this tutorial won't create a second example — it always
            finds this one.
          </p>
        </>
      )}
    </div>
  );
}
