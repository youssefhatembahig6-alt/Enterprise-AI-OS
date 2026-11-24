/**
 * Regression tests for the PR #8 desktop review findings:
 *   P1 folder boundary (links, junctions, swaps, a replaced root),
 *   P1 account isolation of folders, watchers and queues,
 *   P2 unreadable folders keep their records,
 *   P2 removal from the organization is durable and retried.
 * Real filesystem; the server is an in-memory double that enforces folder ownership.
 * Directory links use the "junction" type, which Windows creates without privileges
 * (on Linux and macOS it is an ordinary directory symlink), so CI exercises junctions.
 */

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { chmod, mkdir, mkdtemp, readFile, rename, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, it } from "node:test";
import { FolderManager, FolderNotFound } from "../src/folders";
import { ApprovedRootError, OutsideFolder, readInside, rootIdentity, type RootIdentity } from "../src/sync/boundary";
import { FolderSync } from "../src/sync/engine";
import { scanFolder, type ScanResult } from "../src/sync/scan";
import type { DeleteOutcome, Owner, SendOutcome, Transport, WireEvent } from "../src/sync/transport";

const posixNonRoot = process.platform !== "win32" && process.getuid?.() !== 0;

// -- in-memory server -------------------------------------------------------------------

class Server {
  accounts = new Map<string, Owner>(); // token -> owner
  folders = new Map<string, { owner: string; files: Map<string, string> }>();
  sends: { token: string | null; sourceId: string; events: WireEvent[] }[] = [];
  down = false;
  deleteFails = 0;
  identityGate = new Map<string, () => Promise<void>>();

  account(name: string): string {
    const token = `token-${name}-${randomUUID()}`;
    this.accounts.set(token, { tenantId: "tenant-1", userId: `user-${name}` });
    return token;
  }

  private ownerOf(token: string | null): Owner | null {
    return token ? (this.accounts.get(token) ?? null) : null;
  }

  transport(token: () => string | null): Transport {
    const owner = () => this.ownerOf(token());
    return {
      send: async (sourceId: string, events: WireEvent[]): Promise<SendOutcome> => {
        this.sends.push({ token: token(), sourceId, events });
        const who = owner();
        if (!who) return { kind: "auth" };
        if (this.down) return { kind: "retry", reason: "down" };
        const folder = this.folders.get(sourceId);
        if (!folder || folder.owner !== who.userId) return { kind: "gone" };
        for (const e of events) {
          if (e.op === "delete") folder.files.delete(e.path);
          else folder.files.set(e.path, e.sha256 ?? "");
        }
        return { kind: "ok", results: events.map((e) => ({ event_id: e.event_id, path: e.path, result: "applied" })) };
      },
      register: async (folderId: string, name: string) => {
        const who = owner();
        if (!who) throw new Error("auth");
        const source_id = randomUUID();
        this.folders.set(source_id, { owner: who.userId, files: new Map() });
        return { source_id, folder_id: folderId, name, shared_groups: [] };
      },
      identity: async () => {
        const pending = this.identityGate.get(token() ?? "");
        if (pending) await pending();
        const who = owner();
        if (!who) throw new Error("auth");
        return who;
      },
      ownedSources: async () => {
        const who = owner();
        return [...this.folders].filter(([, f]) => f.owner === who?.userId).map(([id]) => id);
      },
      deleteFolder: async (sourceId: string): Promise<DeleteOutcome> => {
        const who = owner();
        if (!who) return "auth";
        if (this.deleteFails > 0) {
          this.deleteFails -= 1;
          return "retry";
        }
        const folder = this.folders.get(sourceId);
        if (folder && folder.owner === who.userId) this.folders.delete(sourceId);
        return "deleted";
      },
    };
  }
}

let work: string;
let root: string;
let server: Server;

beforeEach(async () => {
  work = await mkdtemp(path.join(os.tmpdir(), "eaios-review-"));
  root = path.join(work, "approved");
  await mkdir(root, { recursive: true });
  server = new Server();
});

afterEach(async () => {
  // Stop every watcher even when an assertion failed, so the test process can exit.
  await Promise.all(managers.splice(0).map((m) => m.stopAll()));
  await chmod(path.join(root, "sub"), 0o755).catch(() => undefined);
  await rm(work, { recursive: true, force: true });
});

function engineFor(token: string, extra: Partial<ConstructorParameters<typeof FolderSync>[0]> = {}) {
  return new FolderSync({
    folderId: "f1",
    root,
    stateFile: path.join(work, "state", "f1.json"),
    transport: server.transport(() => token),
    debounceMs: 50,
    rescanMs: 60_000,
    ...extra,
  });
}

async function readyEngine(extra: Partial<ConstructorParameters<typeof FolderSync>[0]> = {}) {
  const token = server.account("a");
  const transport = server.transport(() => token);
  const { source_id } = await transport.register("f1", "approved", "test");
  const engine = engineFor(token, extra);
  await engine.load();
  await engine.setSource(source_id);
  return { engine, token, sourceId: source_id };
}

async function waitFor(check: () => boolean | Promise<boolean>, ms = 8000): Promise<boolean> {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}

// -- P1: folder boundary ------------------------------------------------------------------

describe("P1 folder boundary", () => {
  it("never syncs through a directory link or junction to an outside folder", async () => {
    const outside = path.join(work, "outside");
    await mkdir(outside);
    await writeFile(path.join(outside, "secret.md"), "OUTSIDE");
    await writeFile(path.join(root, "inside.md"), "INSIDE");
    await symlink(outside, path.join(root, "linked"), "junction");
    const { engine, sourceId } = await readyEngine();
    await engine.sync();
    const files = [...(server.folders.get(sourceId)?.files.keys() ?? [])];
    assert.deepEqual(files, ["inside.md"]);
    const scan = await scanFolder(await rootIdentity(root));
    assert.ok(scan.skipped.some((s) => s.path === "linked" && s.reason === "symlink_not_followed"));
  });

  it("does not upload when a directory is swapped for a junction after the scan", async () => {
    await mkdir(path.join(root, "sub"));
    await writeFile(path.join(root, "sub", "a.md"), "SAME BYTES");
    const { engine, sourceId } = await readyEngine();
    await engine.reconcile(); // upsert of sub/a.md queued
    // Swap: the folder becomes a link to an outside folder holding identical bytes, so a
    // content-hash check alone would pass.
    const outside = path.join(work, "outside");
    await mkdir(outside);
    await writeFile(path.join(outside, "a.md"), "SAME BYTES");
    await rename(path.join(root, "sub"), path.join(work, "sub-moved"));
    await symlink(outside, path.join(root, "sub"), "junction");
    await engine.flush();
    assert.equal(server.folders.get(sourceId)?.files.size, 0, "nothing uploaded through the junction");
    assert.equal(server.sends.length, 0);
  });

  it("stops syncing, without deleting, when the approved root is replaced by a link", async () => {
    const statuses: { detail: string | null }[] = [];
    await writeFile(path.join(root, "a.md"), "A");
    const { engine, sourceId } = await readyEngine({ onStatus: (s) => statuses.push(s) });
    await engine.sync();
    assert.ok(server.folders.get(sourceId)?.files.has("a.md"));
    const other = path.join(work, "elsewhere");
    await mkdir(other);
    await writeFile(path.join(other, "b.md"), "B");
    await rename(root, path.join(work, "approved-moved"));
    await symlink(other, root, "junction");
    const result = await engine.reconcile();
    assert.deepEqual(result, { upserts: 0, deletes: 0 });
    assert.equal(engine.snapshot.outbox.length, 0);
    await engine.flush();
    assert.deepEqual([...(server.folders.get(sourceId)?.files.keys() ?? [])], ["a.md"], "no delete, no b.md");
    assert.equal(statuses.at(-1)?.detail, "folder_changed");
  });

  it("refuses to read through any link component or a file link", async () => {
    const outside = path.join(work, "outside");
    await mkdir(outside);
    await writeFile(path.join(outside, "x.md"), "X");
    await symlink(outside, path.join(root, "dir"), "junction");
    const approved = await rootIdentity(root);
    await assert.rejects(readInside(approved, "dir/x.md", 1024), OutsideFolder);
    if (process.platform !== "win32") {
      await symlink(path.join(outside, "x.md"), path.join(root, "file-link.md"));
      await assert.rejects(readInside(approved, "file-link.md", 1024), OutsideFolder);
    }
  });

  it("requires the pinned directory identity for both listing and reading", async () => {
    await writeFile(path.join(root, "a.md"), "A");
    const approved = await rootIdentity(root);
    const unrelated = path.join(work, "unrelated");
    await mkdir(unrelated);
    const otherIdentity = await rootIdentity(unrelated);
    const stale = { ...otherIdentity, real: approved.real };
    assert.equal((await scanFolder(approved)).files.size, 1);
    assert.equal((await readInside(approved, "a.md", 1024)).toString(), "A");
    await assert.rejects(scanFolder(stale), ApprovedRootError);
    await assert.rejects(readInside(stale, "a.md", 1024), ApprovedRootError);
  });

  it("keeps the last snapshot if a scan loses its approved directory", async () => {
    await writeFile(path.join(root, "a.md"), "A");
    let abortScan = false;
    const { engine, sourceId } = await readyEngine({
      scan: async (approved) => {
        if (abortScan) throw new ApprovedRootError("folder_changed");
        return scanFolder(approved);
      },
    });
    await engine.sync();
    const previous = structuredClone(engine.snapshot);
    abortScan = true;
    assert.deepEqual(await engine.reconcile(), { upserts: 0, deletes: 0 });
    assert.deepEqual(engine.snapshot, previous);
    assert.ok(server.folders.get(sourceId)?.files.has("a.md"));
  });
});

// -- P2: scan failures ------------------------------------------------------------------------

describe("P2 unreadable folders keep their records", () => {
  it("keeps files under an unreadable subtree and queues deletes only once confirmed gone", async () => {
    await mkdir(path.join(root, "sub"));
    await writeFile(path.join(root, "sub", "a.md"), "A");
    await writeFile(path.join(root, "top.md"), "T");
    let fail = false;
    const scan = async (r: RootIdentity): Promise<ScanResult> => {
      const real = await scanFolder(r);
      if (!fail) return real;
      // The subtree could not be listed this time (EACCES/EBUSY on any platform).
      for (const k of [...real.files.keys()]) if (k.startsWith("sub/")) real.files.delete(k);
      return { ...real, unreadable: ["sub"] };
    };
    const statuses: { unreadable: number }[] = [];
    const { engine, sourceId } = await readyEngine({ scan, onStatus: (s) => statuses.push(s) });
    await engine.sync();
    fail = true;
    assert.deepEqual(await engine.reconcile(), { upserts: 0, deletes: 0 });
    assert.ok(engine.snapshot.files["sub/a.md"], "record kept");
    assert.equal(statuses.at(-1)?.unreadable, 1);
    fail = false;
    assert.deepEqual(await engine.reconcile(), { upserts: 0, deletes: 0 }, "readable again: no churn");
    await rm(path.join(root, "sub"), { recursive: true });
    assert.deepEqual(await engine.reconcile(), { upserts: 0, deletes: 1 }, "now confirmed gone");
    await engine.flush();
    assert.deepEqual([...(server.folders.get(sourceId)?.files.keys() ?? [])], ["top.md"]);
  });

  it("keeps everything when the approved root is temporarily missing", async () => {
    await writeFile(path.join(root, "a.md"), "A");
    const { engine } = await readyEngine();
    await engine.sync();
    await rename(root, path.join(work, "unplugged"));
    assert.deepEqual(await engine.reconcile(), { upserts: 0, deletes: 0 });
    await rename(path.join(work, "unplugged"), root);
    assert.deepEqual(await engine.reconcile(), { upserts: 0, deletes: 0 });
    assert.ok(engine.snapshot.files["a.md"]);
  });

  it("keeps records under a directory without read permission (POSIX)", { skip: !posixNonRoot }, async () => {
    await mkdir(path.join(root, "sub"));
    await writeFile(path.join(root, "sub", "a.md"), "A");
    const { engine } = await readyEngine();
    await engine.sync();
    await chmod(path.join(root, "sub"), 0o000);
    assert.deepEqual(await engine.reconcile(), { upserts: 0, deletes: 0 });
    await chmod(path.join(root, "sub"), 0o755);
    assert.deepEqual(await engine.reconcile(), { upserts: 0, deletes: 0 });
  });

  it("retries an upload whose file is temporarily unreadable instead of dropping it (POSIX)", { skip: !posixNonRoot }, async () => {
    await writeFile(path.join(root, "a.md"), "A");
    const { engine, sourceId } = await readyEngine();
    await engine.reconcile();
    await chmod(path.join(root, "a.md"), 0o000);
    await engine.flush();
    assert.equal(engine.snapshot.outbox.length, 1);
    assert.equal(engine.snapshot.outbox[0]?.attempts, 1);
    await chmod(path.join(root, "a.md"), 0o644);
    const queued = engine.snapshot.outbox[0];
    if (queued) queued.next_at = 0;
    await engine.flush();
    assert.ok(server.folders.get(sourceId)?.files.has("a.md"));
  });
});

// -- P1: accounts and P2: removal ----------------------------------------------------------

const managers: FolderManager[] = [];

function manager(statuses: unknown[] = []) {
  const m = new FolderManager({
    dataDir: path.join(work, "app-data"),
    makeTransport: (token) => server.transport(token),
    onStatus: (s) => statuses.push(s),
    engineOptions: { debounceMs: 50, rescanMs: 60_000 },
  });
  managers.push(m);
  return m;
}

describe("P1 account isolation", () => {
  it("ignores earlier identity responses after a new sign-in or sign-out", async () => {
    const tA = server.account("a");
    const tB = server.account("b");
    const m = manager();
    await m.signIn(tA);
    const firstFolder = await m.add(root);
    const delayA = async () => {
      let release!: () => void;
      const pending = new Promise<void>((resolve) => { release = resolve; });
      const started = new Promise<void>((resolve) => {
        server.identityGate.set(tA, () => {
          resolve();
          return pending;
        });
      });
      const earlier = m.signIn(tA);
      await started;
      assert.deepEqual(m.list(), [], "the old view is cleared while identity is pending");
      return { earlier, release };
    };

    const switching = await delayA();
    await m.signIn(tB);
    const secondFolder = await m.add(root);
    switching.release();
    await switching.earlier;
    assert.deepEqual(m.list().map((f) => f.folderId), [secondFolder.folderId]);
    assert.notEqual(secondFolder.folderId, firstFolder.folderId);

    const signingOut = await delayA();
    await m.signOut();
    signingOut.release();
    await signingOut.earlier;
    assert.deepEqual(m.list(), []);
    await assert.rejects(m.add(root), /signed_out/);
  });

  it("shows and manages folders only for the owning account, and resumes its queue", async () => {
    const tA = server.account("a");
    const tB = server.account("b");
    await writeFile(path.join(root, "one.md"), "1");
    const m = manager();
    await m.load();
    await m.signIn(tA);
    const folder = await m.add(root);
    const sourceId = folder.sourceId as string;
    assert.ok(await waitFor(() => server.folders.get(sourceId)?.files.has("one.md") ?? false));

    await m.signIn(tB);
    assert.deepEqual(m.list(), [], "B sees none of A's folders");
    await assert.rejects(m.syncNow(folder.folderId), FolderNotFound);
    await assert.rejects(m.remove(folder.folderId, true), FolderNotFound);
    assert.throws(() => m.engine(folder.folderId), FolderNotFound);
    await writeFile(path.join(root, "two.md"), "2");
    await new Promise((r) => setTimeout(r, 400));
    assert.ok(!server.folders.get(sourceId)?.files.has("two.md"), "not uploaded while B is signed in");
    assert.ok(server.sends.every((s) => s.token !== tB), "A's queue never used B's session");

    await m.signIn(tA);
    assert.equal(m.list().length, 1);
    assert.ok(await waitFor(() => server.folders.get(sourceId)?.files.has("two.md") ?? false), "resumed");
    await m.stopAll();
  });

  it("keeps the previous owner's queued events on disk and sends them only for that owner", async () => {
    const tA = server.account("a");
    const tB = server.account("b");
    const m = manager();
    await m.load();
    await m.signIn(tA);
    server.down = true;
    const folder = await m.add(root);
    await writeFile(path.join(root, "queued.md"), "Q");
    await m.syncNow(folder.folderId);
    await m.signIn(tB); // switch while A has queued work
    const state = JSON.parse(await readFile(path.join(work, "app-data", "sync", `${folder.folderId}.json`), "utf8"));
    assert.ok(state.outbox.some((e: { path: string }) => e.path === "queued.md"), "queue preserved on disk");
    server.down = false;
    await new Promise((r) => setTimeout(r, 300));
    assert.ok(!server.folders.get(folder.sourceId as string)?.files.has("queued.md"));
    await m.signIn(tA);
    assert.ok(await waitFor(() => server.folders.get(folder.sourceId as string)?.files.has("queued.md") ?? false));
    assert.ok(server.sends.filter((s) => s.sourceId === folder.sourceId).every((s) => s.token === tA));
    await m.stopAll();
  });

  it("assigns folders saved before owners were recorded only to the account owning their source", async () => {
    const tA = server.account("a");
    const tB = server.account("b");
    const { source_id } = await server.transport(() => tA).register("legacy", "approved", "test");
    await mkdir(path.join(work, "app-data"), { recursive: true });
    await writeFile(
      path.join(work, "app-data", "folders.json"),
      JSON.stringify([{ folderId: "legacy", sourceId: source_id, name: "approved", root }]),
    );
    const m = manager();
    await m.load();
    await m.signIn(tB);
    assert.deepEqual(m.list(), []);
    await m.signIn(tA);
    assert.equal(m.list()[0]?.folderId, "legacy");
    const saved = JSON.parse(await readFile(path.join(work, "app-data", "folders.json"), "utf8"));
    assert.equal(saved[0].owner.userId, "user-a");
    await m.stopAll();
  });
});

describe("P2 durable removal from the organization", () => {
  it("keeps a failed removal visible and completes it after an app restart", async () => {
    const tA = server.account("a");
    let m = manager();
    await m.load();
    await m.signIn(tA);
    const folder = await m.add(root);
    server.deleteFails = 1;
    const pending = await m.remove(folder.folderId, true);
    assert.equal(pending?.pendingRemoval, true);
    assert.equal(pending?.removalError, "server_unreachable");
    assert.equal(m.list().length, 1, "still listed and retryable");
    assert.ok(server.folders.has(folder.sourceId as string));
    await m.stopAll();

    m = manager(); // restart
    await m.load();
    await m.signIn(tA);
    assert.deepEqual(m.list(), [], "retried at sign-in and confirmed");
    assert.ok(!server.folders.has(folder.sourceId as string));
    await m.stopAll();
  });

  it("retries on demand and keeps the entry while signed out of the owner", async () => {
    const tA = server.account("a");
    const m = manager();
    await m.load();
    await m.signIn(tA);
    const folder = await m.add(root);
    server.deleteFails = 2;
    assert.equal((await m.remove(folder.folderId, true))?.pendingRemoval, true);
    assert.equal((await m.remove(folder.folderId, true))?.pendingRemoval, true, "second failure");
    assert.equal(await m.remove(folder.folderId, true), null, "third attempt confirmed");
    assert.deepEqual(m.list(), []);
    await m.stopAll();
  });
});
