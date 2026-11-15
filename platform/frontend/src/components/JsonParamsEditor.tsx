// A compact JSON-object editor for the Configuration tab's strategy
// parameters. Monaco already ships in the workspace, so the parameters get
// the same treatment as code files: JSON syntax highlighting, inline squiggly
// validation from Monaco's JSON language service, a live status line (valid
// object / parse error / not-an-object) and a one-click Format.
//
// Same graceful fallback as MonacoEditor: if the Monaco loader fails (e.g.
// offline dev), it degrades to a plain monospace <textarea>.

import { useEffect, useMemo, useState } from "react";
import Editor, { loader } from "@monaco-editor/react";

interface Props {
  value: string;
  onChange: (v: string) => void;
}

/** Parse like the save path does (StrategyWorkspace.parseParams): empty text
 *  is `{}`, everything must be a JSON *object*. */
function check(text: string): { ok: boolean; message: string; keys: number } {
  const t = text.trim();
  if (!t) return { ok: true, message: "empty — the strategy gets an empty config", keys: 0 };
  let parsed: unknown;
  try {
    parsed = JSON.parse(t);
  } catch (e) {
    return { ok: false, message: (e as Error).message.replace(/^Unexpected token/, "Syntax error: unexpected token"), keys: 0 };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, message: "must be a JSON object, not " + (Array.isArray(parsed) ? "an array" : typeof parsed), keys: 0 };
  }
  const keys = Object.keys(parsed as Record<string, unknown>).length;
  return { ok: true, message: `valid — ${keys} parameter${keys === 1 ? "" : "s"}`, keys };
}

export function JsonParamsEditor({ value, onChange }: Props) {
  const [fallback, setFallback] = useState(false);
  const status = useMemo(() => check(value), [value]);

  // Same loader-failure detection as MonacoEditor.
  useEffect(() => {
    let alive = true;
    loader.init().catch(() => {
      if (alive) setFallback(true);
    });
    return () => {
      alive = false;
    };
  }, []);

  const format = () => {
    if (!status.ok) return;
    const t = value.trim();
    onChange(t ? JSON.stringify(JSON.parse(t), null, 2) : "{}");
  };

  return (
    <div>
      <div className="rounded border border-white/10 overflow-hidden bg-black/40">
        {fallback ? (
          <textarea
            className="w-full h-44 bg-transparent text-slate-100 mono p-2 outline-none resize-y"
            value={value}
            spellCheck={false}
            onChange={(e) => onChange(e.target.value)}
          />
        ) : (
          <Editor
            height="176px"
            language="json"
            value={value}
            theme="vs-dark"
            onChange={(v) => onChange(v ?? "")}
            options={{
              minimap: { enabled: false },
              fontSize: 12.5,
              automaticLayout: true,
              tabSize: 4,
              lineNumbers: "on",
              folding: true,
              wordWrap: "on",
              scrollBeyondLastLine: false,
              renderLineHighlight: "none",
              overviewRulerLanes: 0,
              scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
            }}
          />
        )}
      </div>
      <div className="flex items-center gap-3 mt-1">
        <span className={`text-[11px] ${status.ok ? "text-emerald-400" : "text-red-400"}`}>
          {status.ok ? "✓ " : "✕ "}{status.message}
        </span>
        <button
          type="button"
          className="btn-ghost text-[11px] ml-auto"
          disabled={!status.ok || fallback}
          title={fallback ? "formatting needs the Monaco editor" : "reformat as pretty-printed JSON"}
          onClick={format}
        >
          Format
        </button>
      </div>
    </div>
  );
}
