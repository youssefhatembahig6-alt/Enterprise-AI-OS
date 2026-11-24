/**
 * Local-folder sync engine on the real filesystem (and the real recursive watcher),
 * against an in-process HTTP double of the backend's event route that applies the
 * same idempotency rules (event ids once, stale sequences ignored). The backend's own
 * implementation is tested in backend/tests/integration/test_m5_local_folder.py.
 *
 * Runs on Debian and Windows in CI (``desktop`` job).
 */

import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { mkdir, mkdtemp, rm, symlink, unlink, writeFile } from "node:fs/promises";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import os from "node:os";
import path from "node:path";
import { after, before, beforeEach, describe, it } from "node:test";
import { FolderSync, backoff } from "../src/sync/engine";
import { excludedFile, validRelative } from "../src/sync/rules";
import { scanFolder } from "../src/sync/scan";
import { rootIdentity } from "../src/sync/boundary";
import { loadState } from "../src/sync/state";
import { HttpTransport, type WireEvent } from "../src/sync/transport";

interface Applied {
  path: string;
  op: string;
  sha256?: string;
  seq: number;
}

class FakeBackend {
  server!: Server;
  url = "";
  token = "test-session";
  down = false; // refuse with 503
  dropResponse = 0; // apply the batch, then fail the response (lost reply)
  seen = new Set<string>();
  lastSeq = new Map<string, number>();
  files = new Map<string, string>(); // path -> sha256 currently on the server
  batches: WireEvent[][] = [];
  applied: Applied[] = [];

  async start(): Promise<void> {
    this.server = createServer((req, res) => void this.handle(req, res));
    await new Promise<void>((r) => this.server.listen(0, "127.0.0.1", () => r()));
    this.url = `http://127.0.0.1:${(this.server.address() as AddressInfo).port}`;
  }

  async stop(): Promise<void> {
    await new Promise<void>((r) => this.server.close(() => r()));
  }

  reset(): void {
    this.down = false;
    this.dropResponse = 0;
    this.seen.clear();
    this.lastSeq.clear();
    this.files.clear();
    this.batches = [];
    this.applied = [];
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(chunk as Buffer);
    if (req.headers.authorization !== `Bearer ${this.token}`) {
      res.writeHead(401).end("{}");
      return;
    }
    if (this.down) {
      res.writeHead(503).end("{}");
      return;
    }
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8")) as { events: WireEvent[] };
    this.batches.push(body.events);
    const results = body.events.map((e) => {
      if (this.seen.has(e.event_id)) return { event_id: e.event_id, path: e.path, result: "duplicate" };
      this.seen.add(e.event_id);
      const last = this.lastSeq.get(e.path);
      if (last !== undefined && e.seq <= last) return { event_id: e.event_id, path: e.path, result: "stale" };
      this.lastSeq.set(e.path, e.seq);
      if (e.op === "delete") {
        const had = this.files.delete(e.path);
        this.applied.push({ path: e.path, op: e.op, seq: e.seq });
        return { event_id: e.event_id, path: e.path, result: had ? "removed" : "noop" };
      }
      const data = Buffer.from(e.content_base64 ?? "", "base64");
      const sha = createHash("sha256").update(data).digest("hex");
      assert.equal(sha, e.sha256, "content matches the declared hash");
      assert.equal(data.length, e.size);
      const result = this.files.get(e.path) === sha ? "unchanged" : this.files.has(e.path) ? "updated" : "created";
      this.files.set(e.path, sha);
      this.applied.push({ path: e.path, op: e.op, sha256: sha, seq: e.seq });
      return { event_id: e.event_id, path: e.path, result };
    });
    if (this.dropResponse > 0) {
      this.dropResponse -= 1;
      req.socket.destroy(); // the server applied the batch but the reply is lost
      return;
    }
    res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ results }));
  }
}

const backend = new FakeBackend();
let work: string;
let root: string;
let clock = 1_000_000;

function engine(extra: Partial<ConstructorParameters<typeof FolderSync>[0]> = {}): FolderSync {
  return new FolderSync({
    folderId: "f1",
    root,
    stateFile: path.join(work, "state", "f1.json"),
    transport: new HttpTransport(backend.url, () => backend.token, 2000),
    now: () => clock,
    debounceMs: 100,
    rescanMs: 60_000,
    ...extra,
  });
}

async function ready(sync: FolderSync): Promise<FolderSync> {
  await sync.load();
  await sync.setSource("source-1");
  return sync;
}

before(async () => backend.start());
after(async () => backend.stop());

beforeEach(async () => {
  backend.reset();
  clock = 1_000_000;
  work = await mkdtemp(path.join(os.tmpdir(), "eaios-desktop-test-"));
  root = path.join(work, "folder");
  await mkdir(root, { recursive: true });
});

describe("rules and scan", () => {
  it("recurses, skips excluded, oversized, deep and symlinked entries, and uses / paths", async () => {
    await mkdir(path.join(root, "a", "b"), { recursive: true });
    await mkdir(path.join(root, ".git"), { recursive: true });
    await mkdir(path.join(root, "node_modules", "x"), { recursive: true });
    await writeFile(path.join(root, "a", "b", "deep.md"), "# deep\n");
    await writeFile(path.join(root, "top.txt"), "top\n");
    await writeFile(path.join(root, ".git", "config"), "x");
    await writeFile(path.join(root, "node_modules", "x", "i.js"), "x");
    await writeFile(path.join(root, "~$lock.md"), "x");
    await writeFile(path.join(root, "photo.png"), "x");
    await writeFile(path.join(root, "big.csv"), Buffer.alloc(10 * 1024 * 1024 + 1));
    let linked = true;
    try {
      await symlink(path.join(root, "top.txt"), path.join(root, "alias.txt"));
    } catch {
      linked = false; // Windows without symlink privilege
    }
    let deep = root;
    for (let i = 0; i < 21; i += 1) deep = path.join(deep, `d${i}`);
    await mkdir(deep, { recursive: true });
    await writeFile(path.join(deep, "too-deep.md"), "x");

    const scan = await scanFolder(await rootIdentity(root));
    assert.deepEqual([...scan.files.keys()].sort(), ["a/b/deep.md", "top.txt"]);
    const reasons = Object.fromEntries(scan.skipped.map((s) => [s.path, s.reason]));
    assert.equal(reasons[".git"], "hidden_folder");
    assert.equal(reasons["node_modules"], "dependency_or_build_folder");
    assert.equal(reasons["~$lock.md"], "temporary_or_generated_file");
    assert.equal(reasons["photo.png"], "unsupported_type");
    assert.equal(reasons["big.csv"], "too_large");
    assert.ok(Object.values(reasons).includes("too_deep"));
    if (linked) assert.equal(reasons["alias.txt"], "symlink_not_followed");
  });

  it("validates relative paths like the backend", () => {
    assert.ok(validRelative("a/b.md"));
    for (const bad of ["../x.md", "/abs.md", "C:/x.md", "a\\b.md", "a//b.md", ""]) {
      assert.equal(validRelative(bad), false, bad);
    }
    assert.equal(excludedFile("notes/report.md"), null);
    assert.equal(excludedFile("src/.env"), "hidden_file");
  });
});

describe("reconcile and flush", () => {
  it("uploads once, survives a restart without re-sending, and sends deletes", async () => {
    await writeFile(path.join(root, "a.md"), "# A\n");
    await mkdir(path.join(root, "sub"));
    await writeFile(path.join(root, "sub", "b.md"), "# B\n");
    const first = await ready(engine());
    assert.deepEqual(await first.reconcile(), { upserts: 2, deletes: 0 });
    assert.equal(await first.flush(), "ok");
    assert.deepEqual([...backend.files.keys()].sort(), ["a.md", "sub/b.md"]);

    const restarted = await ready(engine()); // same state file
    assert.deepEqual(await restarted.reconcile(), { upserts: 0, deletes: 0 });
    assert.equal(await restarted.flush(), "nothing");

    await unlink(path.join(root, "sub", "b.md")); // deleted while the app was closed
    const third = await ready(engine());
    assert.deepEqual(await third.reconcile(), { upserts: 0, deletes: 1 });
    await third.flush();
    assert.deepEqual([...backend.files.keys()], ["a.md"]);
  });

  it("ignores duplicate and no-op changes and coalesces rapid edits", async () => {
    const sync = await ready(engine());
    await writeFile(path.join(root, "a.md"), "v1");
    await sync.reconcile();
    await writeFile(path.join(root, "a.md"), "v2"); // edited again before the flush
    await sync.reconcile();
    assert.equal(sync.snapshot.outbox.length, 1, "one pending event per path");
    await sync.flush();
    assert.equal(backend.applied.length, 1);
    assert.equal(backend.applied[0]?.sha256, createHash("sha256").update("v2").digest("hex"));

    // Same bytes rewritten (an editor saving twice) and a touch: nothing to send.
    await writeFile(path.join(root, "a.md"), "v2");
    const future = new Date(Date.now() + 5000);
    const { utimes } = await import("node:fs/promises");
    await utimes(path.join(root, "a.md"), future, future);
    assert.deepEqual(await sync.reconcile(), { upserts: 0, deletes: 0 });
    assert.equal(sync.snapshot.outbox.length, 0);
  });

  it("keeps events queued while offline and backs off, then delivers them", async () => {
    const sync = await ready(engine());
    await writeFile(path.join(root, "a.md"), "offline");
    await sync.reconcile();
    backend.down = true;
    assert.equal(await sync.flush(), "retry");
    const [queued] = sync.snapshot.outbox;
    assert.equal(queued?.attempts, 1);
    assert.equal(queued?.next_at, clock + backoff(1));
    assert.equal(await sync.flush(), "waiting"); // not due yet
    // Persisted: a restart keeps the queue.
    const saved = await loadState(path.join(work, "state", "f1.json"), "f1");
    assert.equal(saved.outbox[0]?.event_id, queued?.event_id);
    backend.down = false;
    clock += backoff(1);
    assert.equal(await sync.flush(), "ok");
    assert.equal(sync.snapshot.outbox.length, 0);
    assert.equal(backend.files.size, 1);
  });

  it("re-sends a batch whose reply was lost; the server applies it once", async () => {
    const sync = await ready(engine());
    await writeFile(path.join(root, "a.md"), "once");
    await sync.reconcile();
    backend.dropResponse = 1;
    assert.equal(await sync.flush(), "retry");
    clock += backoff(1);
    assert.equal(await sync.flush(), "ok");
    assert.equal(backend.batches.length, 2);
    assert.equal(backend.batches[0]?.[0]?.event_id, backend.batches[1]?.[0]?.event_id);
    assert.equal(backend.applied.length, 1, "applied once");
  });

  it("pauses without a session and resumes after sign-in", async () => {
    let token: string | null = null;
    const sync = await ready(
      engine({ transport: new HttpTransport(backend.url, () => token, 2000) }),
    );
    await writeFile(path.join(root, "a.md"), "x");
    await sync.reconcile();
    assert.equal(await sync.flush(), "auth");
    assert.equal(sync.snapshot.outbox.length, 1);
    token = backend.token;
    assert.equal(await sync.flush(), "ok");
  });

  it("drops a queued upsert whose file changed again; the next scan sends the new bytes", async () => {
    const sync = await ready(engine());
    await writeFile(path.join(root, "a.md"), "first");
    await sync.reconcile();
    await writeFile(path.join(root, "a.md"), "second, longer");
    await sync.flush(); // queued hash no longer matches: dropped, nothing sent
    assert.equal(backend.applied.length, 0);
    await sync.reconcile();
    await sync.flush();
    assert.equal(backend.applied[0]?.sha256, createHash("sha256").update("second, longer").digest("hex"));
  });

  it("sends a stale-proof sequence: a late upsert after a delete is ignored by the server", async () => {
    const sync = await ready(engine());
    await writeFile(path.join(root, "a.md"), "x");
    await sync.reconcile();
    await sync.flush();
    const upsertSeq = backend.applied[0]?.seq ?? 0;
    await unlink(path.join(root, "a.md"));
    await sync.reconcile();
    await sync.flush();
    assert.ok((backend.lastSeq.get("a.md") ?? 0) > upsertSeq);
    assert.equal(backend.files.has("a.md"), false);
  });
});

describe("watcher", () => {
  it("picks up changes through the recursive watcher (or the periodic rescan)", async () => {
    const sync = await ready(engine({ now: Date.now, rescanMs: 1500 }));
    sync.start();
    try {
      await mkdir(path.join(root, "nested", "deeper"), { recursive: true });
      await writeFile(path.join(root, "nested", "deeper", `${randomUUID()}.md`), "# watched\n");
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline && backend.files.size === 0) {
        await new Promise((r) => setTimeout(r, 100));
      }
      assert.equal(backend.files.size, 1, `watcher recursive=${sync.watcherMode.recursive}`);
    } finally {
      await sync.stop();
      await rm(work, { recursive: true, force: true });
    }
  });
});
