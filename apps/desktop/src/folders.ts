/**
 * Watched folders of this desktop installation. The list (folder id, server source
 * id, local path, label) lives in the user's application-data directory; per-folder
 * sync state lives next to it. Neither file contains a session token.
 */

import { randomUUID } from "node:crypto";
import { readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { FolderSync, type SyncStatus } from "./sync/engine";
import { saveState, emptyState } from "./sync/state";
import type { Transport } from "./sync/transport";

export interface FolderEntry {
  folderId: string;
  sourceId: string | null;
  name: string;
  root: string;
}

export class FolderManager {
  private entries: FolderEntry[] = [];
  private readonly engines = new Map<string, FolderSync>();
  private readonly status = new Map<string, SyncStatus>();

  constructor(
    private readonly dataDir: string,
    private readonly transport: Transport,
    private readonly onStatus: (status: SyncStatus) => void,
    private readonly engineOptions: { debounceMs?: number; rescanMs?: number } = {},
  ) {}

  private get listFile(): string {
    return path.join(this.dataDir, "folders.json");
  }

  private stateFile(folderId: string): string {
    return path.join(this.dataDir, "sync", `${folderId}.json`);
  }

  async load(): Promise<void> {
    try {
      this.entries = JSON.parse(await readFile(this.listFile, "utf8")) as FolderEntry[];
    } catch {
      this.entries = [];
    }
    for (const entry of this.entries) await this.startEngine(entry);
  }

  private async save(): Promise<void> {
    // Reuse the atomic writer: the list is small JSON.
    const { mkdir, writeFile, rename } = await import("node:fs/promises");
    await mkdir(this.dataDir, { recursive: true });
    const tmp = `${this.listFile}.${randomUUID()}.tmp`;
    await writeFile(tmp, JSON.stringify(this.entries), { mode: 0o600 });
    await rename(tmp, this.listFile);
  }

  private async startEngine(entry: FolderEntry): Promise<FolderSync> {
    const engine = new FolderSync({
      folderId: entry.folderId,
      root: entry.root,
      stateFile: this.stateFile(entry.folderId),
      transport: this.transport,
      onStatus: (s) => {
        this.status.set(entry.folderId, s);
        this.onStatus(s);
      },
      ...this.engineOptions,
    });
    await engine.load();
    if (entry.sourceId && engine.snapshot.sourceId !== entry.sourceId) {
      await engine.setSource(entry.sourceId);
    }
    engine.start();
    this.engines.set(entry.folderId, engine);
    return engine;
  }

  list(): (FolderEntry & { status: SyncStatus | null })[] {
    return this.entries.map((e) => ({ ...e, status: this.status.get(e.folderId) ?? null }));
  }

  /** Register with the backend (which assigns ownership and sharing), then watch. */
  async add(root: string): Promise<FolderEntry> {
    const resolved = path.resolve(root);
    const existing = this.entries.find((e) => e.root === resolved);
    if (existing) return existing;
    const folderId = randomUUID();
    const name = path.basename(resolved) || resolved;
    const info = await this.transport.register(folderId, name, os.hostname());
    const entry: FolderEntry = { folderId, sourceId: info.source_id, name, root: resolved };
    await saveState(this.stateFile(folderId), { ...emptyState(folderId), sourceId: info.source_id });
    this.entries.push(entry);
    await this.save();
    await this.startEngine(entry);
    return entry;
  }

  async remove(folderId: string): Promise<void> {
    await this.engines.get(folderId)?.stop();
    this.engines.delete(folderId);
    this.entries = this.entries.filter((e) => e.folderId !== folderId);
    await this.save();
    await rm(this.stateFile(folderId), { force: true });
  }

  async syncAll(): Promise<void> {
    await Promise.all([...this.engines.values()].map((e) => e.sync().catch(() => undefined)));
  }

  async syncNow(folderId: string): Promise<void> {
    await this.engines.get(folderId)?.sync();
  }

  engine(folderId: string): FolderSync | undefined {
    return this.engines.get(folderId);
  }

  async stopAll(): Promise<void> {
    await Promise.all([...this.engines.values()].map((e) => e.stop()));
  }
}
