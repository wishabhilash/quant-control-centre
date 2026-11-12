// File explorer supporting create/rename/delete for files and nested folders.
import { useState } from "react";
import type { TreeNode } from "../types/strategy";
import { addFile as addFileNode, addFolder as addFolderNode, deleteNode, renameNode } from "../lib/project";

interface Props {
  root: TreeNode;
  onChange: (root: TreeNode) => void;
  openPath: string | null;
  onOpen: (path: string) => void;
  // Optional, so existing callers (e.g. CodeTab) are unaffected.
  onFileCreated?: (path: string) => void;
  onFileRenamed?: (oldPath: string, newPath: string) => void;
  onFileDeleted?: (path: string) => void;
}

export function FileExplorer({
  root,
  onChange,
  openPath,
  onOpen,
  onFileCreated,
  onFileRenamed,
  onFileDeleted,
}: Props) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const toggle = (p: string) => setCollapsed((c) => ({ ...c, [p]: !c[p] }));

  const addFile = (dirPath: string) => {
    const name = prompt("New file name (relative to current folder):");
    if (!name) return;
    const full = dirPath ? `${dirPath}/${name}` : name;
    onChange(addFileNode(root, full));
    onFileCreated?.(full);
  };
  const addFolder = (dirPath: string) => {
    const name = prompt("New folder name (relative to current folder):");
    if (!name) return;
    const full = dirPath ? `${dirPath}/${name}` : name;
    onChange(addFolderNode(root, full));
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
    onChange(renameNode(root, node.path, nextPath));
    onFileRenamed?.(node.path, nextPath);
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
    </div>
  );
}
