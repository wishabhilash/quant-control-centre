import { useEffect, useMemo, useRef, useState } from "react";
import { codeApi } from "../../api/client";
import type { StrategyDocument, TreeNode } from "../../types/strategy";
import { FileExplorer } from "../FileExplorer";
import { MonacoEditor } from "../MonacoEditor";
import { filesToTree, treeToFiles } from "../../lib/project";

export function CodeTab({ doc, onSaved }: { doc: StrategyDocument; onSaved: (d: StrategyDocument) => void }) {
  const [tree, setTree] = useState<TreeNode>(() => filesToTree(doc.code.files));
  const [openPath, setOpenPath] = useState<string | null>(doc.code.entrypoint);
  const [entrypoint, setEntrypoint] = useState(doc.code.entrypoint);
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  // Keep editor content synced with the tree's current file.
  const currentFile = useMemo(() => {
    let found: string | undefined;
    const walk = (n: TreeNode) => {
      if (n.file?.path === openPath) found = n.file.content;
      n.children?.forEach(walk);
    };
    walk(tree);
    return found ?? "";
  }, [tree, openPath]);

  const pyFiles = useMemo(() => {
    const out: string[] = [];
    const walk = (n: TreeNode) => {
      if (n.type === "file" && n.name.endsWith(".py")) out.push(n.path);
      n.children?.forEach(walk);
    };
    walk(tree);
    return out;
  }, [tree]);

  const setContent = (content: string) => {
    setTree((t) => {
      const walk = (n: TreeNode): void => {
        if (n.file?.path === openPath) n.file.content = content;
        n.children?.forEach(walk);
      };
      walk(t);
      return { ...t };
    });
    setDirty(true);
  };

  const save = async () => {
    setBusy(true);
    try {
      const files = treeToFiles(tree);
      await codeApi.save(doc.strategy_id, entrypoint, files);
      setDirty(false);
      setMsg(`Saved ${Object.keys(files).length} files`);
      // Refresh doc so downstream tabs see the new entrypoint.
      const code = await codeApi.get(doc.strategy_id);
      onSaved({ ...doc, code: { ...doc.code, entrypoint: code.entrypoint, files: code.files } });
    } catch (e: any) {
      setMsg(e?.message ?? "save failed");
    } finally {
      setBusy(false);
    }
  };

  const onUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    try {
      const res: any = await codeApi.uploadZip(doc.strategy_id, file);
      const code = await codeApi.get(doc.strategy_id);
      setTree(filesToTree(code.files));
      setEntrypoint(code.entrypoint);
      setOpenPath(code.entrypoint);
      setMsg(`Imported ${res.file_count} files from ZIP`);
    } catch (err: any) {
      setMsg(err?.message ?? "upload failed");
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  return (
    <div className="panel flex flex-col h-[70vh]">
      <div className="flex items-center gap-2 p-2 border-b border-border">
        <button className="btn-ghost text-xs" onClick={save} disabled={busy || !dirty}>
          {dirty ? "● Save all" : "Saved"}
        </button>
        <label className="btn-ghost text-xs flex items-center gap-1 cursor-pointer">
          ⬆ Upload ZIP<input type="file" accept=".zip" ref={fileInput} onChange={onUpload} className="hidden" />
        </label>
        <div className="flex items-center gap-2 text-sm">
          <span className="text-slate-400">Entry point</span>
          <select value={entrypoint} onChange={(e) => setEntrypoint(e.target.value)} className="text-xs">
            {pyFiles.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
        </div>
        <span className="ml-auto text-xs text-slate-500">{dirty ? "unsaved changes" : "no changes"}</span>
      </div>

      <div className="flex flex-1 min-h-0">
        <div className="w-64 border-r border-border">
          <FileExplorer root={tree} onChange={(t) => setTree(t)} openPath={openPath} onOpen={setOpenPath} />
        </div>
        <div className="flex-1 min-w-0">
          {openPath ? (
            <MonacoEditor
              path={openPath}
              language={currentFileLang(openPath)}
              value={currentFile}
              onChange={setContent}
            />
          ) : (
            <div className="p-6 text-slate-500">Select or create a file</div>
          )}
        </div>
      </div>
      {msg && <div className="p-2 border-t border-border"><span className="badge badge-ok">{msg}</span></div>}
    </div>
  );
}

function currentFileLang(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, string> = {
    py: "python", js: "javascript", ts: "typescript", tsx: "typescript",
    json: "json", yaml: "yaml", yml: "yaml", md: "markdown", csv: "csv",
  };
  return map[ext] ?? "plaintext";
}
