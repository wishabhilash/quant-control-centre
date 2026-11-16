// File explorer supporting create/rename/delete for files and nested folders.
//
// Every created/renamed path goes through assertSafeProjectPath (the frontend
// mirror of the backend's path jail): files may live in the project root or
// any folder inside it — never outside it (`..`, absolute paths) and never on
// platform-managed resources (`.venv`, `strategy.log`, …). The backend
// re-validates on save/import regardless; this just fails early and friendly.
import { useState } from "react";
import type { RuntimeFileInfo, TreeNode } from "../types/strategy";
import {
  addFile as addFileNode,
  addFolder as addFolderNode,
  assertSafeProjectPath,
  deleteNode,
  renameNode,
} from "../lib/project";

interface Props {
  root: TreeNode;
  onChange: (root: TreeNode) => void;
  openPath: string | null;
  onOpen: (path: string) => void;
  // Optional, so callers that don't track open tabs are unaffected.
  onFileCreated?: (path: string) => void;
  onFileRenamed?: (oldPath: string, newPath: string) => void;
  onFileDeleted?: (path: string) => void;
  // --- Runtime files (read-only) --------------------------------------------
  // Files the strategy wrote into its own directory at run time. They are NOT
  // part of the project (S3 stays the source of truth) — shown in their own
  // section, opened read-only, never created/renamed/deleted here.
  runtimeFiles?: RuntimeFileInfo[];
  onOpenRuntime?: (path: string) => void;
  onRefreshRuntime?: () => void;
}

export function FileExplorer({
  root,
  onChange,
  openPath,
  onOpen,
  onFileCreated,
  onFileRenamed,
  onFileDeleted,
  runtimeFiles,
  onOpenRuntime,
  onRefreshRuntime,
}: Props) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [runtimeOpen, setRuntimeOpen] = useState(false);
  const toggle = (p: string) => setCollapsed((c) => ({ ...c, [p]: !c[p] }));

  const addFile = (dirPath: string) => {
    const name = prompt("New file name (relative to current folder):");
    if (!name) return;
    const full = dirPath ? `${dirPath}/${name}` : name;
    try {
      const norm = assertSafeProjectPath(full);
      onChange(addFileNode(root, norm));
      onFileCreated?.(norm);
    } catch (e) {
      alert((e as Error).message);
    }
  };
  const addFolder = (dirPath: string) => {
    const name = prompt("New folder name (relative to current folder):");
    if (!name) return;
    const full = dirPath ? `${dirPath}/${name}` : name;
    try {
      const norm = assertSafeProjectPath(full);
      onChange(addFolderNode(root, norm));
    } catch (e) {
      alert((e as Error).message);
    }
  };
  const remove = (node: TreeNode) => {
    if (!confirm(`Delete ${node.name}?`)) return;
    onChange(deleteNode(root, node.path));
    onFileDeleted?.(node.path);
  };
  const rename = (node: TreeNode) => {
    const base = node.path.split("/").slice(0, -1).join("/");
    const name = prompt("New name:", node.name);
    if (!name) return;
    const nextPath = `${base}/${name}`;
    try {
      const norm = assertSafeProjectPath(nextPath);
      onChange(renameNode(root, node.path, norm));
      onFileRenamed?.(node.path, norm);
    } catch (e) {
      alert((e as Error).message);
    }
  };

  const render = (node: TreeNode, depth: number): JSX.Element => {
    const pad = depth * 12 + 8;
    if (node.type === "folder") {
      const open = !collapsed[node.path];
      return (
        <div key={node.path}>
          <div
            className="flex items-center gap-1 px-1 py-0.5 hover:bg-white/5 cursor-pointer text-sm"
            style={{ paddingLeft: pad }}
            onClick={() => toggle(node.path)}
          >
            <span className="w-3">{open ? "▾" : "▸"}</span>
            <span>📁 {node.name}</span>
            <span className="ml-auto flex gap-1 opacity-0 group-hover:opacity-100">
              <button className="text-xs text-slate-400" onClick={(e) => { e.stopPropagation(); addFolder(node.path || ""); }}>＋📁</button>
              <button className="text-xs text-slate-400" onClick={(e) => { e.stopPropagation(); rename(node); }}>✎</button>
              <button className="text-xs text-red-400" onClick={(e) => { e.stopPropagation(); remove(node); }}>🗑</button>
            </span>
          </div>
          {open && node.children?.map((c) => render(c, depth + 1))}
        </div>
      );
    }
    return (
      <div
        key={node.path}
        className={`flex items-center gap-1 px-1 py-0.5 text-sm cursor-pointer hover:bg-white/5 ${
          openPath === node.path ? "bg-accent/15 text-accent" : ""
        }`}
        style={{ paddingLeft: pad }}
        onClick={() => onOpen(node.path)}
      >
        <span>📄 {node.name}</span>
        <span className="ml-auto flex gap-1 opacity-0 group-hover:opacity-100">
          <button className="text-xs text-slate-400" onClick={(e) => { e.stopPropagation(); rename(node); }}>✎</button>
          <button className="text-xs text-red-400" onClick={(e) => { e.stopPropagation(); remove(node); }}>🗑</button>
        </span>
      </div>
    );
  };

  return (
    <div className="h-full overflow-auto text-sm group">
      <div className="flex items-center justify-between px-2 py-1 border-b border-border">
        <span className="text-xs text-slate-400">Explorer</span>
        <div className="flex gap-1">
          <button className="text-xs text-slate-400 hover:text-white" onClick={() => addFolder("")} title="New folder">＋📁</button>
          <button className="text-xs text-slate-400 hover:text-white" onClick={() => addFile("")} title="New file">＋📄</button>
        </div>
      </div>
      {root.children?.map((c) => render(c, 0))}
      {/* Runtime files — read-only; the strategy writes these at run time.
          Never merged into the project tree above (S3 = source of truth). */}
      {runtimeFiles && onOpenRuntime && (
        <div className="border-t border-border mt-1">
          <div
            className="flex items-center gap-1 px-2 py-1 cursor-pointer hover:bg-white/5"
            onClick={() => setRuntimeOpen((o) => !o)}
          >
            <span className="w-3">{runtimeOpen ? "▾" : "▸"}</span>
            <span className="text-xs text-slate-400">Runtime files ({runtimeFiles.length})</span>
            <button
              className="ml-auto text-xs text-slate-400 hover:text-white"
              title="Reload the runtime file list"
              onClick={(e) => { e.stopPropagation(); onRefreshRuntime?.(); }}
            >
              ⟳
            </button>
          </div>
          {runtimeOpen && (runtimeFiles.length === 0 ? (
            <div className="px-2 py-1 text-xs text-slate-500">No runtime files yet — a run writes them here.</div>
          ) : runtimeFiles.map((f) => (
            <div
              key={f.path}
              className={`flex items-center gap-1 px-2 py-0.5 text-sm cursor-pointer hover:bg-white/5 ${
                openPath === f.path ? "bg-accent/15 text-accent" : ""
              }`}
              onClick={() => onOpenRuntime(f.path)}
              title={`${f.path} — read-only (created by a run)`}
            >
              <span>📄 {f.path}</span>
              <span className="ml-auto text-[10px] text-slate-500 pr-1">
                {f.size_bytes >= 1024 ? `${(f.size_bytes / 1024).toFixed(1)} kB` : `${f.size_bytes} B`}
              </span>
            </div>
          )))}
        </div>
      )}
    </div>
  );
}
