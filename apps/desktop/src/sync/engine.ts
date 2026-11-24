/**
 * Local-folder sync engine (M5, ADR 0503). One instance per watched folder.
 *
 * reconcile(): scan -> compare with the durable state -> enqueue upsert/delete events.
 *   A file is hashed only when its size or mtime changed, and enqueued only when its
 *   SHA-256 changed, so duplicate or spurious watcher events (editors that write
 *   twice, touch, rename-then-write) produce no event. A newer event for a path
 *   replaces an unsent older one; sequence numbers only increase.
 * flush(): send queued events in batches. The backend applies each event id once, so
 *   a batch whose response was lost is safely re-sent. Network errors, timeouts, 429
 *   and 5xx keep the events queued with exponential backoff (offline retry); 401
 *   pauses until the user signs in again; 404 means the folder is gone.
 * start(): recursive fs.watch where the platform supports it, debounced, plus a
 *   periodic full rescan (watchers can drop events, and deletes made while the app
 *   was closed are found by the first rescan).
 */

import { createHash, randomUUID } from "node:crypto";
import { watch, type FSWatcher } from "node:fs";
import { MAX_FILE_BYTES } from "./rules";
import { ApprovedRootError, assertApprovedRoot, OutsideFolder, readInside, rootIdentity, sameRoot, type RootIdentity } from "./boundary";
import { isGone, scanFolder, under, type ScanResult } from "./scan";
import { loadState, saveState, type FileRecord, type FolderState, type OutboxEvent } from "./state";
import type { SendOutcome, Transport, WireEvent } from "./transport";

export type SyncPhase = "idle" | "syncing" | "offline" | "signed_out" | "folder_gone" | "error";

export interface SyncStatus {
  folderId: string;
  phase: SyncPhase;
  pending: number;
  skipped: number;
  /** Directories or files that exist but could not be read in the last scan (retried). */
  unreadable: number;
  rejected: number;
  lastSyncAt: number | null;
  detail: string | null;
}

export interface EngineOptions {
  folderId: string;
  root: string;
  stateFile: string;
  transport: Transport;
  now?: () => number;
  debounceMs?: number;
  rescanMs?: number;
  batchEvents?: number;
  batchBytes?: number;
  onStatus?: (status: SyncStatus) => void;
  /** Test seam: replaces the file-system scan. */
  scan?: (root: RootIdentity) => Promise<ScanResult>;
}

const BACKOFF_BASE_MS = 1000;
// A file modified this recently is always re-hashed: a second write of the same size
// inside the file system's timestamp granularity would otherwise look unchanged
// (the "racy timestamp" problem; Git handles it the same way).
const RACY_WINDOW_MS = 2000;
const BACKOFF_MAX_MS = 5 * 60 * 1000;

export function backoff(attempts: number): number {
  return Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** Math.max(0, attempts - 1));
}

export class FolderSync {
  private state!: FolderState;
  private readonly now: () => number;
  private skipped = 0;
  private unreadable = 0;
  private phase: SyncPhase = "idle";
  private detail: string | null = null;
  private lastSyncAt: number | null = null;
  private chain: Promise<unknown> = Promise.resolve();
  private watcher: FSWatcher | null = null;
  private debounce: NodeJS.Timeout | null = null;
  private timer: NodeJS.Timeout | null = null;
  private stopped = false;
  readonly watcherMode: { recursive: boolean } = { recursive: false };

  constructor(private readonly opts: EngineOptions) {
    this.now = opts.now ?? Date.now;
  }

  get snapshot(): FolderState {
    return this.state;
  }

  async load(): Promise<void> {
    this.state = await loadState(this.opts.stateFile, this.opts.folderId);
  }

  setSource(sourceId: string): Promise<void> {
    return this.serial(async () => {
      this.state.sourceId = sourceId;
      await this.persist();
    });
  }

  /** Serialise reconcile and flush so the outbox is never mutated concurrently. */
  private serial<T>(task: () => Promise<T>): Promise<T> {
    const next = this.chain.then(task, task);
    this.chain = next.catch(() => undefined);
    return next;
  }

  private persist(): Promise<void> {
    return saveState(this.opts.stateFile, this.state);
  }

  private emit(): void {
    this.opts.onStatus?.({
      folderId: this.opts.folderId,
      phase: this.phase,
      pending: this.state?.outbox.length ?? 0,
      skipped: this.skipped,
      unreadable: this.unreadable,
      rejected: this.state?.rejected.length ?? 0,
      lastSyncAt: this.lastSyncAt,
      detail: this.detail,
    });
  }

  private enqueue(event: Omit<OutboxEvent, "event_id" | "seq" | "attempts" | "next_at">): void {
    // A newer state of the same path supersedes an unsent older event.
    this.state.outbox = this.state.outbox.filter((e) => e.path !== event.path);
    this.state.seq += 1;
    this.state.outbox.push({
      ...event,
      event_id: randomUUID(),
      seq: this.state.seq,
      attempts: 0,
      next_at: 0,
    });
  }

  /** The approved root, if it still resolves to the folder first synced. Sets the
   * phase and returns null when it is unavailable or has changed (nothing is read or
   * deleted then). */
  private async checkedRoot(): Promise<RootIdentity | null> {
    let current: RootIdentity;
    try {
      current = await rootIdentity(this.opts.root);
    } catch (err) {
      this.phase = "error";
      this.detail = err instanceof OutsideFolder ? "folder_changed" : isGone(err) ? "folder_unavailable" : "folder_unreadable";
      return null;
    }
    if (!this.state.root) {
      this.state.root = current;
      await this.persist();
    } else if (!sameRoot(this.state.root, current)) {
      this.phase = "error";
      this.detail = "folder_changed"; // replaced by a link/junction or moved: never followed
      return null;
    }
    return current;
  }

  private reportRootError(error: unknown): void {
    if (!(error instanceof ApprovedRootError)) throw error;
    this.phase = "error";
    this.detail = error.reason;
    this.emit();
  }

  reconcile(): Promise<{ upserts: number; deletes: number }> {
    return this.serial(async () => {
      const root = await this.checkedRoot();
      if (!root) {
        this.emit();
        return { upserts: 0, deletes: 0 };
      }
      let scan: ScanResult;
      const updates = new Map<string, { record: FileRecord; changed: boolean }>();
      const missing = new Set<string>();
      const unreadable: string[] = [];
      const wall = Date.now();
      try {
        scan = await (this.opts.scan ?? scanFolder)(root);
        unreadable.push(...scan.unreadable);
        for (const [rel, stat] of scan.files) {
          const known = this.state.files[rel];
          const racy = stat.mtimeMs > wall - RACY_WINDOW_MS;
          if (known && !racy && known.size === stat.size && known.mtime === stat.mtime) continue;
          try {
            const data = await readInside(root, rel, MAX_FILE_BYTES);
            const sha = createHash("sha256").update(data).digest("hex");
            updates.set(rel, {
              record: { sha256: sha, size: data.length, mtime: racy ? "" : stat.mtime },
              changed: known?.sha256 !== sha,
            });
          } catch (err) {
            if (err instanceof ApprovedRootError) throw err;
            if (err instanceof OutsideFolder || isGone(err)) missing.add(rel);
            else unreadable.push(rel);
          }
        }
        // Commit a scan as one unit. A changed root invalidates provisional file records
        // and absence decisions, including scans that have no files to open for hashing.
        await assertApprovedRoot(root);
      } catch (err) {
        this.reportRootError(err);
        return { upserts: 0, deletes: 0 };
      }
      this.skipped = scan.skipped.length;
      let upserts = 0;
      for (const [rel, { record, changed }] of updates) {
        this.state.files[rel] = record;
        if (changed) {
          this.enqueue({ op: "upsert", path: rel, sha256: record.sha256, size: record.size });
          upserts += 1;
        }
      }
      // A file is deleted only when the scan confirmed it is gone (or is no longer
      // inside the folder); anything under an unreadable directory keeps its record.
      const kept = (rel: string) =>
        (scan.files.has(rel) && !missing.has(rel)) || unreadable.some((u) => under(rel, u));
      const gone = Object.keys(this.state.files).filter((rel) => !kept(rel));
      this.state.files = Object.fromEntries(Object.entries(this.state.files).filter(([rel]) => kept(rel)));
      for (const rel of gone) {
        this.enqueue({ op: "delete", path: rel });
      }
      this.unreadable = unreadable.length;
      if (unreadable.length) {
        this.phase = "error";
        this.detail = `unreadable: ${unreadable.slice(0, 5).join(", ")}${unreadable.length > 5 ? ", ..." : ""}`;
      } else if (this.detail?.startsWith("unreadable") || this.detail?.startsWith("folder_")) {
        this.phase = "idle";
        this.detail = null;
      }
      await this.persist();
      this.emit();
      return { upserts, deletes: gone.length };
    });
  }

  flush(): Promise<SendOutcome["kind"] | "nothing" | "waiting"> {
    return this.serial(async () => {
      const sourceId = this.state.sourceId;
      if (!sourceId) return "waiting";
      const root = await this.checkedRoot();
      if (!root) {
        this.emit();
        return "waiting";
      }
      let last: SendOutcome["kind"] | "nothing" | "waiting" = "nothing";
      for (;;) {
        const due = this.state.outbox.filter((e) => e.next_at <= this.now());
        if (!due.length) {
          if (this.state.outbox.length) last = "waiting";
          break;
        }
        let batch;
        try {
          batch = await this.batch(due, root);
          await assertApprovedRoot(root);
        } catch (err) {
          this.reportRootError(err);
          return "waiting";
        }
        if (this.stopped) return "waiting";
        const { wire, sent, dropped, deferred } = batch;
        for (const event of deferred) {
          event.attempts += 1; // unreadable right now (locked, denied): keep and retry
          event.next_at = this.now() + backoff(event.attempts);
        }
        for (const event of dropped) {
          this.state.outbox = this.state.outbox.filter((e) => e.event_id !== event.event_id);
          // The file changed after it was queued: forget its record so the next scan
          // re-hashes it and queues its current bytes (or a delete if it is gone).
          const record = this.state.files[event.path];
          if (record) this.state.files[event.path] = { ...record, mtime: "", sha256: "" };
        }
        if (!wire.length) {
          await this.persist();
          if (deferred.length) break;
          continue;
        }
        this.phase = "syncing";
        this.emit();
        const outcome = await this.opts.transport.send(sourceId, wire);
        last = outcome.kind;
        if (outcome.kind === "ok") {
          const acked = new Set(outcome.results.map((r) => r.event_id));
          for (const r of outcome.results) {
            if (r.result === "rejected") {
              this.state.rejected.push({ path: r.path, reason: r.reason ?? "rejected", at: this.now() });
              this.state.rejected = this.state.rejected.slice(-50);
            }
          }
          const before = this.state.outbox.length;
          this.state.outbox = this.state.outbox.filter((e) => !acked.has(e.event_id));
          if (this.state.outbox.length === before) {
            this.phase = "error"; // nothing acknowledged: do not spin
            this.detail = "no_events_acknowledged";
            await this.persist();
            break;
          }
          // An unreadable subtree stays reported (and retried) until a scan reads it.
          this.phase = this.unreadable ? "error" : "idle";
          if (!this.unreadable) this.detail = null;
          this.lastSyncAt = this.now();
          await this.persist();
          continue;
        }
        if (outcome.kind === "retry") {
          for (const event of sent) {
            event.attempts += 1;
            event.next_at = this.now() + backoff(event.attempts);
          }
          this.phase = "offline";
          this.detail = outcome.reason;
        } else if (outcome.kind === "auth") {
          this.phase = "signed_out";
          this.detail = "Sign in to resume syncing.";
        } else if (outcome.kind === "gone") {
          this.phase = "folder_gone";
          this.detail = "The folder was removed on the server.";
        } else {
          this.phase = "error";
          this.detail = outcome.code;
          for (const event of sent) {
            event.attempts += 1;
            event.next_at = this.now() + backoff(event.attempts);
          }
        }
        await this.persist();
        break;
      }
      this.emit();
      return last;
    });
  }

  /** Build one batch from due events. Content is read now and must still match the
   * queued SHA-256; otherwise the event is dropped (the file changed again or was
   * deleted, and the next reconcile enqueues its current state). */
  private async batch(
    due: OutboxEvent[],
    root: RootIdentity,
  ): Promise<{ wire: WireEvent[]; sent: OutboxEvent[]; dropped: OutboxEvent[]; deferred: OutboxEvent[] }> {
    const max = this.opts.batchEvents ?? 20;
    const budget = this.opts.batchBytes ?? 8 * 1024 * 1024;
    const wire: WireEvent[] = [];
    const sent: OutboxEvent[] = [];
    const dropped: OutboxEvent[] = [];
    const deferred: OutboxEvent[] = [];
    let bytes = 0;
    for (const event of due) {
      if (wire.length >= max) break;
      if (event.op === "delete") {
        wire.push({ event_id: event.event_id, seq: event.seq, op: "delete", path: event.path });
        sent.push(event);
        continue;
      }
      let data: Buffer;
      try {
        data = await readInside(root, event.path, MAX_FILE_BYTES);
      } catch (err) {
        if (err instanceof ApprovedRootError) throw err;
        if (err instanceof OutsideFolder || isGone(err)) dropped.push(event);
        else deferred.push(event);
        continue;
      }
      if (createHash("sha256").update(data).digest("hex") !== event.sha256) {
        dropped.push(event);
        continue;
      }
      if (wire.length && bytes + data.length > budget) break;
      bytes += data.length;
      wire.push({
        event_id: event.event_id,
        seq: event.seq,
        op: "upsert",
        path: event.path,
        sha256: event.sha256,
        size: data.length,
        content_base64: data.toString("base64"),
      });
      sent.push(event);
    }
    return { wire, sent, dropped, deferred };
  }

  /** Make every queued event due now (the owner signed in again: retry at once instead
   * of waiting out a backoff that started while signed out or offline). */
  retryNow(): Promise<void> {
    return this.serial(async () => {
      for (const event of this.state.outbox) event.next_at = 0;
      await this.persist();
    });
  }

  /** Reconcile then flush. */
  async sync(): Promise<void> {
    if (this.stopped) return;
    await this.reconcile();
    if (!this.stopped) await this.flush();
  }

  start(options: { retryNow?: boolean } = {}): void {
    this.stopped = false;
    if (options.retryNow) void this.retryNow();
    const trigger = () => {
      if (this.debounce) clearTimeout(this.debounce);
      this.debounce = setTimeout(() => void this.sync().catch(() => undefined), this.opts.debounceMs ?? 400);
    };
    try {
      this.watcher = watch(this.opts.root, { recursive: true }, trigger);
      this.watcherMode.recursive = true;
    } catch {
      // Recursive watching unavailable on this platform: rely on periodic rescans.
      this.watcher = null;
    }
    this.watcher?.on("error", () => undefined);
    const tick = () => void this.sync().catch(() => undefined);
    this.timer = setInterval(tick, this.opts.rescanMs ?? 60_000);
    tick();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.watcher?.close();
    this.watcher = null;
    if (this.debounce) clearTimeout(this.debounce);
    if (this.timer) clearInterval(this.timer);
    await this.chain;
  }
}
