/**
 * Recursive folder scan. Symbolic links (and Windows junctions, which Node reports
 * as symbolic links) are never followed: a link could leave the folder the user
 * chose or create a cycle. Excluded and dependency folders are not descended into.
 */

import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { lstat, readdir } from "node:fs/promises";
import path from "node:path";
import {
  MAX_DEPTH,
  MAX_FILE_BYTES,
  MAX_FILES,
  excludedDirectory,
  excludedFile,
  validRelative,
  type SkipReason,
} from "./rules";

export interface FileStat {
  size: number;
  mtime: string; // nanoseconds since the epoch (full precision, compared as text)
  mtimeMs: number;
}

export interface ScanResult {
  files: Map<string, FileStat>;
  skipped: { path: string; reason: SkipReason }[];
}

export function toRelative(root: string, full: string): string {
  return path.relative(root, full).split(path.sep).join("/");
}

export async function scanFolder(root: string): Promise<ScanResult> {
  const files = new Map<string, FileStat>();
  const skipped: ScanResult["skipped"] = [];
  const queue: { dir: string; depth: number }[] = [{ dir: root, depth: 0 }];
  while (queue.length) {
    const { dir, depth } = queue.shift() as { dir: string; depth: number };
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      continue; // vanished or unreadable while scanning; the next scan retries
    }
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      const rel = toRelative(root, full);
      if (entry.isSymbolicLink()) {
        skipped.push({ path: rel, reason: "symlink_not_followed" });
        continue;
      }
      if (entry.isDirectory()) {
        const reason = excludedDirectory(entry.name);
        if (reason) skipped.push({ path: rel, reason });
        else if (depth + 1 >= MAX_DEPTH) skipped.push({ path: rel, reason: "too_deep" });
        else queue.push({ dir: full, depth: depth + 1 });
        continue;
      }
      if (!entry.isFile()) continue;
      if (!validRelative(rel)) {
        skipped.push({ path: rel, reason: "invalid_path" });
        continue;
      }
      const reason = excludedFile(rel);
      if (reason) {
        skipped.push({ path: rel, reason });
        continue;
      }
      let info;
      try {
        info = await lstat(full, { bigint: true });
      } catch {
        continue;
      }
      if (info.size > BigInt(MAX_FILE_BYTES)) {
        skipped.push({ path: rel, reason: "too_large" });
        continue;
      }
      if (files.size >= MAX_FILES) {
        skipped.push({ path: rel, reason: "too_many_files" });
        continue;
      }
      files.set(rel, {
        size: Number(info.size),
        mtime: info.mtimeNs.toString(),
        mtimeMs: Number(info.mtimeMs),
      });
    }
  }
  return { files, skipped };
}

export async function sha256File(full: string): Promise<string> {
  const hash = createHash("sha256");
  await new Promise<void>((resolve, reject) => {
    const stream = createReadStream(full);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", reject);
    stream.on("end", () => resolve());
  });
  return hash.digest("hex");
}
