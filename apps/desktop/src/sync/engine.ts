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
import { readFile } from "node:fs/promises";
import path from "node:path";
import { MAX_FILE_BYTES } from "./rules";
import { scanFolder, sha256File } from "./scan";
import { loadState, saveState, type FolderState, type OutboxEvent } from "./state";
import type { SendOutcome, Transport, WireEvent } from "./transport";

export type SyncPhase = "idle" | "syncing" | "offline" | "signed_out" | "folder_gone" | "error";

export interface SyncStatus {
  folderId: string;
  phase: SyncPhase;
  pending: number;
  skipped: number;
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
  private phase: SyncPhase = "idle";
  private detail: string | null = null;
  private lastSyncAt: number | null = null;
  private chain: Promise<unknown> = Promise.resolve();
  private watcher: FSWatcher | null = null;
  private debounce: NodeJS.Timeout | null = null;
  private timer: NodeJS.Timeout | null = null;
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

  reconcile(): Promise<{ upserts: number; deletes: number }> {
    return this.serial(async () => {
      const scan = await scanFolder(this.opts.root);
      this.skipped = scan.skipped.length;
      let upserts = 0;
      let deletes = 0;
      const wall = Date.now();
      for (const [rel, stat] of scan.files) {
        const known = this.state.files[rel];
        const racy = stat.mtimeMs > wall - RACY_WINDOW_MS;
        if (known && !racy && known.size === stat.size && known.mtime === stat.mtime) continue;
        let sha: string;
        try {
          sha = await sha256File(path.join(this.opts.root, ...rel.split("/")));
        } catch {
          continue; // removed mid-scan; the next scan sees the delete
        }
        if (known && known.sha256 === sha) {
          this.state.files[rel] = { ...known, mtime: racy ? "" : stat.mtime }; // touch only
          continue;
        }
        this.state.files[rel] = { sha256: sha, size: stat.size, mtime: racy ? "" : stat.mtime };
        this.enqueue({ op: "upsert", path: rel, sha256: sha, size: stat.size });
        upserts += 1;
      }
      const gone = Object.keys(this.state.files).filter((rel) => !scan.files.has(rel));
      this.state.files = Object.fromEntries(
        Object.entries(this.state.files).filter(([rel]) => scan.files.has(rel)),
      );
      for (const rel of gone) {
        this.enqueue({ op: "delete", path: rel });
        deletes += 1;
      }
      await this.persist();
      this.emit();
      return { upserts, deletes };
    });
  }

  flush(): Promise<SendOutcome["kind"] | "nothing" | "waiting"> {
    return this.serial(async () => {
      const sourceId = this.state.sourceId;
      if (!sourceId) return "waiting";
      let last: SendOutcome["kind"] | "nothing" | "waiting" = "nothing";
      for (;;) {
        const due = this.state.outbox.filter((e) => e.next_at <= this.now());
        if (!due.length) {
          if (this.state.outbox.length) last = "waiting";
          break;
        }
        const { wire, sent, dropped } = await this.batch(due);
        for (const event of dropped) {
          this.state.outbox = this.state.outbox.filter((e) => e.event_id !== event.event_id);
          // The file changed after it was queued: forget its record so the next scan
          // re-hashes it and queues its current bytes (or a delete if it is gone).
          const record = this.state.files[event.path];
          if (record) this.state.files[event.path] = { ...record, mtime: "", sha256: "" };
        }
        if (!wire.length) {
          await this.persist();
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
          this.phase = "idle";
          this.detail = null;
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
  private async batch(due: OutboxEvent[]): Promise<{
    wire: WireEvent[];
    sent: OutboxEvent[];
    dropped: OutboxEvent[];
  }> {
    const max = this.opts.batchEvents ?? 20;
    const budget = this.opts.batchBytes ?? 8 * 1024 * 1024;
    const wire: WireEvent[] = [];
    const sent: OutboxEvent[] = [];
    const dropped: OutboxEvent[] = [];
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
        data = await readFile(path.join(this.opts.root, ...event.path.split("/")));
      } catch {
        dropped.push(event);
        continue;
      }
      if (data.length > MAX_FILE_BYTES || createHash("sha256").update(data).digest("hex") !== event.sha256) {
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
    return { wire, sent, dropped };
  }

  /** Reconcile then flush. */
  async sync(): Promise<void> {
    await this.reconcile();
    await this.flush();
  }

  start(): void {
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
    this.watcher?.close();
    this.watcher = null;
    if (this.debounce) clearTimeout(this.debounce);
    if (this.timer) clearInterval(this.timer);
    await this.chain;
  }
}
