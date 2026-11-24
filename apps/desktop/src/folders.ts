/**
 * Watched folders of this desktop installation, bound to the account that registered
 * them (ADR 0503).
 *
 * - Every entry records its owner (tenant id and user id, from ``/api/v1/me``). Only the
 *   signed-in owner's folders are listed, watched, synced or removed. Another account's
 *   folders are invisible and refuse every action, even if their ids are guessed.
 * - Each engine's transport presents a session token only while its own owner is
 *   signed in, so one account's queue can never be sent with another account's session.
 *   On an account switch the previous owner's watchers stop; their outbox and file
 *   records stay on disk and resume, after a catch-up scan, when that owner signs in again.
 * - Entries saved before owners were recorded are claimed only by an account the server
 *   confirms owns their source (``GET /api/v1/local-folders``).
 * - "Remove from organization" is durable: the entry is marked ``pendingRemoval`` (on
 *   disk), its watcher stops, and the entry stays listed until the server confirms the
 *   deletion. Failures keep it retryable, also after an app restart.
 *
 * The list and per-folder state live in the user's application-data directory. Neither
 * contains a session token.
 */

import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { FolderSync, type SyncStatus } from "./sync/engine";
import { emptyState, saveState } from "./sync/state";
import type { Owner, Transport } from "./sync/transport";

export interface FolderEntry {
  folderId: string;
  sourceId: string | null;
  name: string;
  root: string;
  owner?: Owner; // absent only for entries saved before M5 review fixes (claimed on sign-in)
  pendingRemoval?: boolean;
  removalError?: string | null;
}

export type ListedFolder = Omit<FolderEntry, "owner"> & { status: SyncStatus | null };

export class FolderNotFound extends Error {
  constructor() {
    super("folder_not_found");
  }
}

export interface ManagerOptions {
  dataDir: string;
  /** Build a transport whose requests carry whatever ``token()`` returns (or none). */
  makeTransport: (token: () => string | null) => Transport;
  onStatus: (status: SyncStatus) => void;
  engineOptions?: { debounceMs?: number; rescanMs?: number };
}

const key = (o: Owner) => `${o.tenantId}/${o.userId}`;

export class FolderManager {
  private entries: FolderEntry[] = [];
  private readonly engines = new Map<string, FolderSync>();
  private readonly status = new Map<string, SyncStatus>();
  private owner: Owner | null = null;
  private token: string | null = null;
  private loaded = false;

  constructor(private readonly opts: ManagerOptions) {}

  private get listFile(): string {
    return path.join(this.opts.dataDir, "folders.json");
  }

  private stateFile(folderId: string): string {
    return path.join(this.opts.dataDir, "sync", `${folderId}.json`);
  }

  /** The token, only for requests made on behalf of ``owner`` while it is signed in. */
  private tokenFor(owner: Owner | undefined): string | null {
    return owner && this.owner && key(owner) === key(this.owner) ? this.token : null;
  }

  private mine(entry: FolderEntry): boolean {
    return Boolean(this.owner && entry.owner && key(entry.owner) === key(this.owner));
  }

  private find(folderId: string): FolderEntry {
    const entry = this.entries.find((e) => e.folderId === folderId);
    if (!entry || !this.mine(entry)) throw new FolderNotFound();
    return entry;
  }

  async load(): Promise<void> {
    try {
      this.entries = JSON.parse(await readFile(this.listFile, "utf8")) as FolderEntry[];
    } catch {
      this.entries = [];
    }
    this.loaded = true;
    // Nothing runs until an account signs in: folders belong to accounts, not to the app.
  }

  private async save(): Promise<void> {
    await mkdir(this.opts.dataDir, { recursive: true });
    const tmp = `${this.listFile}.${randomUUID()}.tmp`;
    await writeFile(tmp, JSON.stringify(this.entries), { mode: 0o600 });
    await rename(tmp, this.listFile);
  }

  /** Sign in (or refresh the token of) an account; start only its folders. */
  async signIn(token: string): Promise<Owner> {
    if (!this.loaded) await this.load();
    const owner = await this.opts.makeTransport(() => token).identity();
    if (this.owner && key(this.owner) === key(owner)) {
      this.token = token; // token refresh for the same account
      await this.retryRemovals();
      await this.syncAll();
      return owner;
    }
    await this.stopEngines(); // the previous owner's queue stays on disk for later
    this.owner = owner;
    this.token = token;
    await this.claimLegacy(token);
    for (const entry of this.entries.filter((e) => this.mine(e) && !e.pendingRemoval)) {
      await this.startEngine(entry);
    }
    await this.retryRemovals();
    return owner;
  }

  async signOut(): Promise<void> {
    await this.stopEngines();
    this.owner = null;
    this.token = null;
  }

  private async claimLegacy(token: string): Promise<void> {
    const legacy = this.entries.filter((e) => !e.owner && e.sourceId);
    if (!legacy.length || !this.owner) return;
    let owned: Set<string>;
    try {
      owned = new Set(await this.opts.makeTransport(() => token).ownedSources());
    } catch {
      return; // try again at the next sign-in
    }
    let changed = false;
    for (const entry of legacy) {
      if (entry.sourceId && owned.has(entry.sourceId)) {
        entry.owner = { ...this.owner };
        changed = true;
      }
    }
    if (changed) await this.save();
  }

  private async startEngine(entry: FolderEntry): Promise<FolderSync> {
    const owner = entry.owner;
    const engine = new FolderSync({
      folderId: entry.folderId,
      root: entry.root,
      stateFile: this.stateFile(entry.folderId),
      transport: this.opts.makeTransport(() => this.tokenFor(owner)),
      onStatus: (s) => {
        this.status.set(entry.folderId, s);
        if (this.mine(entry)) this.opts.onStatus(s); // never show another account's status
      },
      ...this.opts.engineOptions,
    });
    await engine.load();
    if (entry.sourceId && engine.snapshot.sourceId !== entry.sourceId) {
      await engine.setSource(entry.sourceId);
    }
    engine.start({ retryNow: true }); // the owner is signed in: resume its queue at once
    this.engines.set(entry.folderId, engine);
    return engine;
  }

  private async stopEngines(): Promise<void> {
    const running = [...this.engines.values()];
    this.engines.clear();
    await Promise.all(running.map((e) => e.stop()));
  }

  /** The signed-in account's folders only (none while signed out). */
  list(): ListedFolder[] {
    return this.entries
      .filter((e) => this.mine(e))
      .map((e) => ({
        folderId: e.folderId,
        sourceId: e.sourceId,
        name: e.name,
        root: e.root,
        pendingRemoval: e.pendingRemoval,
        removalError: e.removalError,
        status: this.status.get(e.folderId) ?? null,
      }));
  }

  /** Register with the backend (which assigns ownership and sharing), then watch. */
  async add(root: string): Promise<ListedFolder> {
    const owner = this.owner;
    if (!owner) throw new Error("signed_out");
    const resolved = path.resolve(root);
    const existing = this.entries.find((e) => this.mine(e) && e.root === resolved && !e.pendingRemoval);
    if (existing) return this.list().find((e) => e.folderId === existing.folderId) as ListedFolder;
    const folderId = randomUUID();
    const name = path.basename(resolved) || resolved;
    const info = await this.opts.makeTransport(() => this.tokenFor(owner)).register(folderId, name, os.hostname());
    const entry: FolderEntry = { folderId, sourceId: info.source_id, name, root: resolved, owner: { ...owner } };
    await saveState(this.stateFile(folderId), { ...emptyState(folderId), sourceId: info.source_id });
    this.entries.push(entry);
    await this.save();
    await this.startEngine(entry);
    return this.list().find((e) => e.folderId === folderId) as ListedFolder;
  }

  /**
   * Stop syncing a folder. ``fromServer``: also remove it from the organization; the
   * entry is kept (marked pending, on disk) until the server confirms, and retried.
   */
  async remove(folderId: string, fromServer = false): Promise<ListedFolder | null> {
    const entry = this.find(folderId);
    await this.engines.get(folderId)?.stop();
    this.engines.delete(folderId);
    if (!fromServer || !entry.sourceId) {
      await this.forget(entry);
      return null;
    }
    entry.pendingRemoval = true;
    entry.removalError = null;
    await this.save();
    return this.tryRemoval(entry);
  }

  private async tryRemoval(entry: FolderEntry): Promise<ListedFolder | null> {
    const outcome = await this.opts
      .makeTransport(() => this.tokenFor(entry.owner))
      .deleteFolder(entry.sourceId as string);
    if (outcome === "deleted") {
      await this.forget(entry);
      return null;
    }
    entry.removalError = outcome === "auth" ? "sign_in_required" : "server_unreachable";
    await this.save();
    return this.list().find((e) => e.folderId === entry.folderId) ?? null;
  }

  private async retryRemovals(): Promise<void> {
    for (const entry of this.entries.filter((e) => this.mine(e) && e.pendingRemoval)) {
      await this.tryRemoval(entry);
    }
  }

  private async forget(entry: FolderEntry): Promise<void> {
    this.entries = this.entries.filter((e) => e.folderId !== entry.folderId);
    this.status.delete(entry.folderId);
    await this.save();
    await rm(this.stateFile(entry.folderId), { force: true });
  }

  async syncAll(): Promise<void> {
    await Promise.all([...this.engines.values()].map((e) => e.sync().catch(() => undefined)));
  }

  async syncNow(folderId: string): Promise<void> {
    this.find(folderId);
    await this.engines.get(folderId)?.sync();
  }

  /** The signed-in account's engine for a folder (tests and the smoke run). */
  engine(folderId: string): FolderSync | undefined {
    this.find(folderId);
    return this.engines.get(folderId);
  }

  async stopAll(): Promise<void> {
    await this.stopEngines();
  }
}
