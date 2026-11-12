// File-explorer tree helpers shared by the editor and ZIP import (spec 7).

import type { ProjectFile, TreeNode } from "../types/strategy";

const EXT_LANGUAGE: Record<string, string> = {
  py: "python",
  js: "javascript",
  jsx: "javascript",
  ts: "typescript",
  tsx: "typescript",
  json: "json",
  csv: "csv",
  txt: "plaintext",
  md: "markdown",
  yaml: "yaml",
  yml: "yaml",
  sh: "shell",
  ini: "ini",
  toml: "ini",
  sql: "sql",
  html: "html",
  css: "css",
};

export function languageFor(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return EXT_LANGUAGE[ext] ?? "plaintext";
}

export function filesToTree(files: Record<string, string>): TreeNode {
  const root: TreeNode = { name: "project", path: "", type: "folder", children: [] };
  for (const path of Object.keys(files).sort()) {
    const content = files[path] ?? "";
    const parts = path.split("/");
    let node = root;
    for (let i = 0; i < parts.length; i++) {
      const name = parts[i];
      const isFile = i === parts.length - 1;
      let child = node.children?.find((c) => c.name === name);
      if (!child) {
        child = {
          name,
          path: parts.slice(0, i + 1).join("/"),
          type: isFile ? "file" : "folder",
          children: isFile ? undefined : [],
        };
        node.children = node.children ?? [];
        node.children.push(child);
      }
      if (isFile) {
        child.file = { path, content, language: languageFor(path) };
      }
      node = child;
    }
  }
  sortTree(root);
  return root;
}

function sortTree(node: TreeNode): void {
  if (!node.children) return;
  node.children.sort((a, b) => {
    if (a.type !== b.type) return a.type === "folder" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  node.children.forEach(sortTree);
}

// Flatten tree to a path -> content map (used on save).
export function treeToFiles(node: TreeNode): Record<string, string> {
  const out: Record<string, string> = {};
  const walk = (n: TreeNode) => {
    if (n.type === "file" && n.file) out[n.file.path] = n.file.content;
    n.children?.forEach(walk);
  };
  walk(node);
  return out;
}

// Deep-clone a node (and its subtree) so the helpers below can mutate a throwaway
// copy and return a *fresh* root reference. Returning the same object is exactly
// what breaks React state updates: `setTree(root)` bails out when the reference is
// unchanged, so created files/folders never re-render into the explorer.
function cloneTree(node: TreeNode): TreeNode {
  const copy: TreeNode = { ...node };
  if (node.children) copy.children = node.children.map(cloneTree);
  if (node.file) copy.file = { ...node.file };
  return copy;
}

// Create a nested folder path in the tree (returns a new tree; the input is
// left untouched).
export function addFolder(root: TreeNode, folderPath: string): TreeNode {
  const parts = folderPath.split("/").filter(Boolean);
  if (parts.length === 0) return root;
  const tree = cloneTree(root);
  let node = tree;
  for (const name of parts) {
    let child = node.children?.find((c) => c.name === name && c.type === "folder");
    if (!child) {
      child = { name, path: node.path ? node.path + "/" + name : name, type: "folder", children: [] };
      node.children = node.children ?? [];
      node.children.push(child);
    }
    node = child;
  }
  return tree;
}

// Add a file at a relative path (returns a new tree; the input is left untouched).
export function addFile(root: TreeNode, path: string, content = ""): TreeNode {
  const parts = path.split("/").filter(Boolean);
  const fileName = parts[parts.length - 1];
  const dirParts = parts.slice(0, -1);
  const tree = cloneTree(root);
  let node = tree;
  for (const name of dirParts) {
    let child = node.children?.find((c) => c.name === name && c.type === "folder");
    if (!child) {
      child = { name, path: node.path ? node.path + "/" + name : name, type: "folder", children: [] };
      node.children = node.children ?? [];
      node.children.push(child);
    }
    node = child;
  }
  const full = [tree.path, path].filter(Boolean).join("/").replace(/\/+/g, "/");
  node.children = node.children ?? [];
  node.children.push({ name: fileName, path: full, type: "file", file: { path: full, content, language: languageFor(full) } });
  return tree;
}

// Delete a node by path (returns a new tree; the input is left untouched).
export function deleteNode(root: TreeNode, path: string): TreeNode {
  const tree = cloneTree(root);
  const rm = (node: TreeNode): boolean => {
    if (!node.children) return false;
    node.children = node.children.filter((c) => c.path !== path);
    node.children?.forEach(rm);
    return true;
  };
  rm(tree);
  return tree;
}

// Rename a node (folder rename cascades to children; returns a new tree).
export function renameNode(root: TreeNode, oldPath: string, newPath: string): TreeNode {
  const tree = cloneTree(root);
  const rename = (node: TreeNode): boolean => {
    if (node.path === oldPath) {
      const newParts = newPath.split("/");
      node.name = newParts[newParts.length - 1];
      node.path = newPath;
      if (node.children) {
        for (const child of node.children) {
          child.path = newPath + "/" + child.path.slice(oldPath.length + 1);
        }
      }
      return true;
    }
    let done = false;
    node.children?.forEach((c) => {
      if (rename(c)) done = true;
    });
    return done;
  };
  rename(tree);
  return tree;
}

// Content of a single file by path (used by the editor and dirty-tracking).
export function getContent(node: TreeNode, path: string): string | undefined {
  let found: string | undefined;
  const walk = (n: TreeNode): void => {
    if (n.file?.path === path) found = n.file.content;
    n.children?.forEach(walk);
  };
  walk(node);
  return found;
}
