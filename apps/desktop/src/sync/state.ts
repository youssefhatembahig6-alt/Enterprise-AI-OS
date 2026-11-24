/**
 * Durable per-folder state: what was last uploaded per path, the next sequence number
 * and the outbox of events not yet acknowledged by the backend. Written atomically
 * (temporary file + rename), so a crash leaves either the old or the new state.
 * No session token is ever written here.
 */

import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

export interface FileRecord {
  sha256: string;
  size: number;
  mtime: string; // nanoseconds; "" forces a re-hash on the next scan
}

export interface OutboxEvent {
  event_id: string;
  seq: number;
  op: "upsert" | "delete";
  path: string;
  sha256?: string;
  size?: number;
  attempts: number;
  next_at: number; // epoch ms; 0 = send now
}

export interface FolderState {
  version: 1;
  folderId: string;
  sourceId: string | null;
  seq: number;
  files: Record<string, FileRecord>;
  outbox: OutboxEvent[];
  rejected: { path: string; reason: string; at: number }[];
}

export function emptyState(folderId: string): FolderState {
  return { version: 1, folderId, sourceId: null, seq: 0, files: {}, outbox: [], rejected: [] };
}

export async function loadState(file: string, folderId: string): Promise<FolderState> {
  try {
    const raw = JSON.parse(await readFile(file, "utf8")) as FolderState;
    if (raw.version !== 1 || raw.folderId !== folderId) return emptyState(folderId);
    raw.rejected ??= [];
    return raw;
  } catch {
    return emptyState(folderId);
  }
}

export async function saveState(file: string, state: FolderState): Promise<void> {
  await mkdir(path.dirname(file), { recursive: true });
  const tmp = `${file}.${randomUUID()}.tmp`;
  await writeFile(tmp, JSON.stringify(state), { encoding: "utf8", mode: 0o600 });
  await rename(tmp, file);
}
