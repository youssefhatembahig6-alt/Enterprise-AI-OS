/**
 * Folder boundary (ADR 0503): nothing outside the folder the user approved is ever
 * listed, hashed or uploaded.
 *
 * - The approved root is pinned to its real path (and device/inode) when the folder is
 *   first synced. If the root later resolves somewhere else (the folder was replaced by
 *   a link or junction, or moved), syncing stops with `folder_changed`; nothing is
 *   read and nothing is deleted.
 * - Below the root, no path component may be a symbolic link or a Windows junction (Node
 *   reports both as links; `realpath` resolves both). A link is never followed, whether
 *   it was there during the scan or was swapped in afterwards.
 * - Content is read through a handle opened with `O_NOFOLLOW` where the platform has
 *   it. The handle's identity must equal the file at the expected real path inside the
 *   root, so bytes come from the checked file even if a directory is swapped between
 *   the check and the read.
 */

import { constants } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import path from "node:path";

export class OutsideFolder extends Error {
  constructor(readonly reason: "link" | "outside" | "changed" | "not_file" | "too_large") {
    super(reason);
  }
}

export interface RootIdentity {
  real: string;
  dev: string;
  ino: string;
}

const caseless = process.platform === "win32" || process.platform === "darwin";

function same(a: string, b: string): boolean {
  return caseless ? a.toLowerCase() === b.toLowerCase() : a === b;
}

/** Whether `candidate` is `root` or inside it (both real, absolute paths). */
export function inside(root: string, candidate: string): boolean {
  const rel = path.relative(root, candidate); // case-insensitive on Windows; another drive -> absolute
  return rel === "" || (rel !== ".." && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel));
}

/** The root's real path and identity now. Throws if it is missing or not a directory. */
export async function rootIdentity(root: string): Promise<RootIdentity> {
  const real = await realpath(root);
  const info = await lstat(real, { bigint: true });
  if (!info.isDirectory()) throw new OutsideFolder("not_file");
  return { real, dev: info.dev.toString(), ino: info.ino.toString() };
}

export function sameRoot(a: RootIdentity, b: RootIdentity): boolean {
  return same(a.real, b.real) && a.dev === b.dev && a.ino === b.ino;
}

/** The real absolute path of `rel` under `rootReal`, refusing links in any component. */
export async function resolveInside(rootReal: string, rel: string): Promise<string> {
  const parts = rel.split("/");
  let current = rootReal;
  for (const part of parts) {
    current = path.join(current, part);
    const info = await lstat(current);
    if (info.isSymbolicLink()) throw new OutsideFolder("link");
  }
  const real = await realpath(current);
  // A junction or other reparse point not reported as a link still changes the real path.
  if (!same(real, current) || !inside(rootReal, real)) throw new OutsideFolder("outside");
  return current;
}

/**
 * Read `rel` under the approved root, at most `maxBytes`, only if every check holds at
 * the moment of reading (see the module comment). Errors other than `OutsideFolder` are
 * the file system's (missing, permission denied, locked) and leave the decision to the
 * caller.
 */
export async function readInside(rootReal: string, rel: string, maxBytes: number): Promise<Buffer> {
  const full = await resolveInside(rootReal, rel);
  const nofollow = (constants as Record<string, number>).O_NOFOLLOW ?? 0;
  const handle = await open(full, constants.O_RDONLY | nofollow);
  try {
    const opened = await handle.stat({ bigint: true });
    if (!opened.isFile()) throw new OutsideFolder("not_file");
    if (opened.size > BigInt(maxBytes)) throw new OutsideFolder("too_large");
    const real = await realpath(full);
    const now = await lstat(real, { bigint: true });
    if (!same(real, full) || !inside(rootReal, real) || now.ino !== opened.ino || now.dev !== opened.dev) {
      throw new OutsideFolder("changed");
    }
    return await handle.readFile();
  } finally {
    await handle.close();
  }
}
