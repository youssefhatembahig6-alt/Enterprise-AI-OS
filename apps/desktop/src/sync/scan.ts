/**
 * Recursive folder scan inside the approved root (``boundary.ts``). Symbolic links and
 * Windows junctions are never followed: a link could leave the folder the user chose
 * or create a cycle. A directory is descended into only if it is not a link and its
 * real path is exactly where the scan expects it, which also catches reparse points a
 * directory entry does not report as links. Excluded and dependency folders are not
 * descended into.
 *
 * A directory or file that exists but cannot be read (permission denied, locked, I/O
 * error) is reported in ``unreadable``; the engine then keeps the records under it and
 * retries, instead of treating its files as deleted. Only ``ENOENT``/``ENOTDIR`` mean a
 * path is gone.
 */

import { lstat, readdir, realpath } from "node:fs/promises";
import path from "node:path";
import { assertApprovedRoot, OutsideFolder, resolveInside, type RootIdentity } from "./boundary";
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
  /** Relative paths ("" = the root) whose contents could not be listed or stat'ed. */
  unreadable: string[];
}

export function toRelative(root: string, full: string): string {
  return path.relative(root, full).split(path.sep).join("/");
}

/** A missing path is gone; anything else (EACCES, EPERM, EBUSY, EIO, ...) is unreadable. */
export function isGone(err: unknown): boolean {
  const code = (err as NodeJS.ErrnoException | null)?.code;
  return code === "ENOENT" || code === "ENOTDIR";
}

/** Whether ``rel`` is ``prefix`` or below it ("" covers everything). */
export function under(rel: string, prefix: string): boolean {
  return prefix === "" || rel === prefix || rel.startsWith(`${prefix}/`);
}

const caseless = process.platform === "win32" || process.platform === "darwin";

async function realIsExpected(full: string): Promise<boolean> {
  const real = await realpath(full);
  return caseless ? real.toLowerCase() === full.toLowerCase() : real === full;
}

export async function scanFolder(approved: RootIdentity): Promise<ScanResult> {
  const files = new Map<string, FileStat>();
  const skipped: ScanResult["skipped"] = [];
  const unreadable: string[] = [];
  const rootReal = approved.real;
  const queue: { dir: string; depth: number }[] = [{ dir: rootReal, depth: 0 }];
  while (queue.length) {
    const { dir, depth } = queue.shift() as { dir: string; depth: number };
    const relDir = toRelative(rootReal, dir);
    await assertApprovedRoot(approved);
    let entries;
    try {
      if (relDir) await resolveInside(rootReal, relDir);
      entries = await readdir(dir, { withFileTypes: true });
      if (relDir) await resolveInside(rootReal, relDir);
    } catch (err) {
      if (err instanceof OutsideFolder) {
        skipped.push({ path: relDir, reason: "symlink_not_followed" });
        continue;
      }
      // Gone mid-scan: its files are gone too. Unreadable: keep what we know and retry.
      if (!isGone(err)) unreadable.push(relDir);
      continue;
    }
    await assertApprovedRoot(approved);
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      const rel = toRelative(rootReal, full);
      if (entry.isSymbolicLink()) {
        skipped.push({ path: rel, reason: "symlink_not_followed" });
        continue;
      }
      if (entry.isDirectory()) {
        const reason = excludedDirectory(entry.name);
        if (reason) {
          skipped.push({ path: rel, reason });
          continue;
        }
        if (depth + 1 >= MAX_DEPTH) {
          skipped.push({ path: rel, reason: "too_deep" });
          continue;
        }
        try {
          const info = await lstat(full);
          if (info.isSymbolicLink() || !(await realIsExpected(full))) {
            skipped.push({ path: rel, reason: "symlink_not_followed" }); // junction / reparse point
            continue;
          }
        } catch (err) {
          if (!isGone(err)) unreadable.push(rel);
          continue;
        }
        queue.push({ dir: full, depth: depth + 1 });
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
      } catch (err) {
        if (!isGone(err)) unreadable.push(rel);
        continue;
      }
      if (info.isSymbolicLink()) {
        skipped.push({ path: rel, reason: "symlink_not_followed" });
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
  await assertApprovedRoot(approved);
  return { files, skipped, unreadable };
}
