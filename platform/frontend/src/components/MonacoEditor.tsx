// Thin wrapper around @monaco-editor/react with graceful fallback if the
// Monaco loader fails (e.g. offline dev).
import { useEffect, useRef, useState } from "react";
import Editor, { loader, type OnMount } from "@monaco-editor/react";

interface Props {
  path: string;
  language: string;
  value: string;
  onChange: (v: string) => void;
}

export function MonacoEditor({ path, language, value, onChange }: Props) {
  const [fallback, setFallback] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const onMount: OnMount = () => {
    /* no-op: controlled via value */
  };

  // Graceful fallback: if the Monaco loader fails to resolve (e.g. offline dev),
  // switch to a plain <textarea> so the editor stays usable. @monaco-editor/react
  // has no "onDidFailMount" hook, so detect loader failure via loader.init().
  useEffect(() => {
    let alive = true;
    loader.init().catch(() => {
      if (alive) setFallback(true);
    });
    return () => {
      alive = false;
    };
  }, []);

  if (fallback) {
    return (
      <textarea
        ref={taRef}
        className="w-full h-full bg-black/40 text-slate-100 mono p-2 outline-none"
        value={value}
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  return (
    <Editor
      height="100%"
      language={language}
      value={value}
      theme="vs-dark"
      onMount={onMount}
      onChange={(v) => onChange(v ?? "")}
      path={path}
      options={{
        minimap: { enabled: true },
        fontSize: 13,
        automaticLayout: true,
        tabSize: 4,
      }}
    />
  );
}
