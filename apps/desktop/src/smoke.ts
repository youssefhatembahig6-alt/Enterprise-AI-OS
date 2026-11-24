/**
 * Desktop smoke test (``electron . --smoke``). Runs inside the real Electron main
 * process against a real backend and writes a JSON report:
 *
 *  1. the shared React app loads from ``app://eaios`` with the CSP, and the sandboxed
 *     renderer sees exactly the preload bridge (no Node);
 *  2. sign in, register a temporary folder (the backend assigns the owner), and watch
 *     it with the real recursive watcher;
 *  3. initial sync with recursion, an excluded file, a hidden folder, an unsupported
 *     file, an oversized file and (where the OS allows it) a symlink;
 *  4. edit, duplicate writes with identical content, add and delete;
 *  5. a simulated network outage: the first batch fails, events stay queued with
 *     backoff and are delivered afterwards with the same event ids;
 *  6. the server's view (``GET /local-folders/{id}/items``) matches the folder.
 *
 *  7. folder boundary: a link or junction to a folder outside the approved one is never
 *     synced (Windows creates a junction, which needs no privilege);
 *  8. account isolation: with a second account signed in, the first account's folders
 *     are invisible and refuse actions, and a file written meanwhile is not uploaded;
 *     when the first account signs back in, its queue resumes and the file arrives;
 *  9. an unreadable subtree keeps its files on the server (POSIX permissions; on Windows
 *     the engine tests cover this with an injected scan);
 * 10. "remove from organization" survives a failed request and an app restart, and
 *     completes once the server confirms.
 *
 * Environment: EAIOS_API_BASE_URL, EAIOS_SMOKE_TENANT, EAIOS_SMOKE_EMAIL,
 * EAIOS_SMOKE_PASSWORD, EAIOS_SMOKE_EMAIL2, EAIOS_SMOKE_PASSWORD2 (development or test
 * accounts), EAIOS_SMOKE_REPORT (output).
 * The password is read from the environment and never written to the report.
 */

import type { BrowserWindow } from "electron";
import { chmod, mkdtemp, mkdir, rm, symlink, unlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { FolderManager } from "./folders";
import type { SyncStatus } from "./sync/engine";
import { HttpTransport, type SendOutcome, type Transport, type WireEvent } from "./sync/transport";

interface Step {
  name: string;
  ok: boolean;
  detail?: unknown;
}

class FlakyTransport implements Transport {
  failures = 0;
  deleteFailures = 0;
  readonly sentIds: string[][] = [];

  constructor(private readonly inner: HttpTransport) {}

  async send(sourceId: string, events: WireEvent[]): Promise<SendOutcome> {
    this.sentIds.push(events.map((e) => e.event_id));
    if (this.failures > 0) {
      this.failures -= 1;
      return { kind: "retry", reason: "simulated_network_outage" };
    }
    return this.inner.send(sourceId, events);
  }

  register(folderId: string, name: string, device: string) {
    return this.inner.register(folderId, name, device);
  }

  identity() {
    return this.inner.identity();
  }

  ownedSources() {
    return this.inner.ownedSources();
  }

  async deleteFolder(sourceId: string) {
    if (this.deleteFailures > 0) {
      this.deleteFailures -= 1;
      return "retry" as const;
    }
    return this.inner.deleteFolder(sourceId);
  }
}

/** One shared failure switchboard; each engine gets its own token-bound inner transport. */
function flakyFactory(apiBase: string, shared: { failures: number; deleteFailures: number; sentIds: string[][] }) {
  return (token: () => string | null): Transport => {
    const t = new FlakyTransport(new HttpTransport(apiBase, token));
    return {
      send: async (id, events) => {
        t.failures = shared.failures;
        const out = await t.send(id, events);
        shared.failures = t.failures;
        shared.sentIds.push(...t.sentIds.splice(0));
        return out;
      },
      register: (a, b, c) => t.register(a, b, c),
      identity: () => t.identity(),
      ownedSources: () => t.ownedSources(),
      deleteFolder: async (id) => {
        t.deleteFailures = shared.deleteFailures;
        const out = await t.deleteFolder(id);
        shared.deleteFailures = t.deleteFailures;
        return out;
      },
    };
  };
}

async function login(apiBase: string, tenant: string, email: string, password: string): Promise<string | null> {
  const response = await fetch(`${apiBase}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tenant, email, password }),
  });
  return response.ok ? ((await response.json()) as { access_token: string }).access_token : null;
}

async function waitFor(check: () => boolean | Promise<boolean>, ms: number): Promise<boolean> {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await new Promise((r) => setTimeout(r, 100));
  }
  return false;
}

export async function runSmoke(opts: {
  apiBase: string;
  createWindow: () => BrowserWindow;
}): Promise<number> {
  const steps: Step[] = [];
  const started = new Date().toISOString();
  const record = (name: string, ok: boolean, detail?: unknown) => steps.push({ name, ok, detail });
  const reportPath = process.env.EAIOS_SMOKE_REPORT ?? path.join(os.tmpdir(), "eaios-desktop-smoke.json");
  const work = await mkdtemp(path.join(os.tmpdir(), "eaios-smoke-"));
  const folder = path.join(work, "Shared notes");
  let manager: FolderManager | null = null;
  try {
    // 1. Renderer: the shared React app, CSP, bridge only.
    const win = opts.createWindow();
    await new Promise<void>((resolve) => win.webContents.once("did-finish-load", () => resolve()));
    const probe = (await win.webContents.executeJavaScript(
      `({ bridge: typeof window.eaiosDesktop === "object" && Object.keys(window.eaiosDesktop).sort(),
          node: typeof require !== "undefined" || typeof process !== "undefined",
          text: document.body.innerText.slice(0, 400),
          origin: location.origin })`,
    )) as { bridge: string[] | false; node: boolean; text: string; origin: string };
    const csp = await win.webContents.executeJavaScript(
      `fetch("/index.html").then(r => r.headers.get("content-security-policy"))`,
    );
    record("renderer_loads_shared_react_app", /Enterprise AI OS|Sign in/i.test(probe.text), {
      origin: probe.origin,
      text_sample: probe.text.slice(0, 120),
    });
    record("renderer_has_bridge_without_node", Array.isArray(probe.bridge) && !probe.node, {
      bridge: probe.bridge,
    });
    record("content_security_policy", typeof csp === "string" && csp.includes("default-src 'self'"), {
      csp,
    });
    win.destroy();

    // 2. Sign in and register a folder; the backend assigns ownership.
    const tenant = process.env.EAIOS_SMOKE_TENANT ?? "";
    const email = process.env.EAIOS_SMOKE_EMAIL ?? "";
    const password = process.env.EAIOS_SMOKE_PASSWORD ?? "";
    const token = await login(opts.apiBase, tenant, email, password);
    record("sign_in", Boolean(token));
    if (!token) throw new Error("sign-in failed");

    await mkdir(path.join(folder, "projects", "2026"), { recursive: true });
    await mkdir(path.join(folder, ".private"), { recursive: true });
    await writeFile(path.join(folder, "travel.md"), "# Travel\n\nTrain tickets above 40 USD need approval.\n");
    await writeFile(
      path.join(folder, "projects", "2026", "plan.md"),
      "# Plan\n\nThe warehouse pilot starts in March.\n",
    );
    await writeFile(path.join(folder, ".private", "secret.md"), "# never uploaded\n");
    await writeFile(path.join(folder, "~$draft.md"), "lock file\n");
    await writeFile(path.join(folder, "tool.exe"), "MZ");
    await writeFile(path.join(folder, "huge.csv"), Buffer.alloc(10 * 1024 * 1024 + 1, 0x41));
    let symlinkMade = false;
    try {
      await symlink(path.join(folder, "travel.md"), path.join(folder, "link.md"));
      symlinkMade = true;
    } catch {
      symlinkMade = false; // Windows without Developer Mode cannot create symlinks
    }
    // A folder outside the approved one, reachable through a directory link (a junction on
    // Windows, which needs no special privilege).
    const outside = path.join(work, "outside-secret");
    await mkdir(outside, { recursive: true });
    await writeFile(path.join(outside, "secret.md"), "# Outside\n\nOUTSIDE-SECRET must never be uploaded.\n");
    let dirLinkMade = false;
    try {
      await symlink(outside, path.join(folder, "linked-dir"), "junction");
      dirLinkMade = true;
    } catch {
      dirLinkMade = false;
    }

    const statuses: SyncStatus[] = [];
    const shared = { failures: 0, deleteFailures: 0, sentIds: [] as string[][] };
    const flaky = shared;
    const dataDir = path.join(work, "app-data");
    const makeManager = () =>
      new FolderManager({
        dataDir,
        makeTransport: flakyFactory(opts.apiBase, shared),
        onStatus: (s) => statuses.push(s),
        engineOptions: { debounceMs: 300, rescanMs: 2000 },
      });
    manager = makeManager();
    await manager.load();
    await manager.signIn(token);
    const entry = await manager.add(folder);
    record("register_folder", Boolean(entry.sourceId), { source_id: entry.sourceId });
    let engine = manager.engine(entry.folderId);
    if (!engine) throw new Error("engine missing");
    record("recursive_watcher", engine.watcherMode.recursive, { platform: process.platform });

    const items = async () => {
      const r = await fetch(`${opts.apiBase}/api/v1/local-folders/${entry.sourceId}/items`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      return (await r.json()) as { path: string; state: string; reason: string | null }[];
    };
    const idle = () => (engine?.snapshot.outbox.length ?? 1) === 0;

    // 3. Initial sync.
    const synced = await waitFor(async () => idle() && (await items()).length >= 2, 20000);
    const first = await items();
    const uploaded = first.filter((i) => i.state === "active").map((i) => i.path).sort();
    record(
      "initial_sync_recursion_and_exclusions",
      synced && JSON.stringify(uploaded) === JSON.stringify(["projects/2026/plan.md", "travel.md"]),
      { server_items: first, skipped_locally: statuses.at(-1)?.skipped, symlink_created: symlinkMade },
    );
    record(
      "boundary_links_and_junctions_not_followed",
      dirLinkMade && !first.some((i) => i.path.startsWith("linked-dir")),
      { directory_link_created: dirLinkMade, link_type: process.platform === "win32" ? "junction" : "symlink" },
    );

    // 4. Edits, duplicate writes, add and delete through the watcher.
    const seqBefore = engine.snapshot.seq;
    const active0 = engine;
    await writeFile(path.join(folder, "travel.md"), "# Travel\n\nTrain tickets above 50 USD need approval.\n");
    await writeFile(path.join(folder, "travel.md"), "# Travel\n\nTrain tickets above 50 USD need approval.\n");
    await writeFile(path.join(folder, "projects", "notes.txt"), "Kick-off on Sunday.\n");
    await unlink(path.join(folder, "projects", "2026", "plan.md"));
    const changed = await waitFor(async () => {
      const now = await items();
      const plan = now.find((i) => i.path === "projects/2026/plan.md");
      const notes = now.find((i) => i.path === "projects/notes.txt");
      return idle() && plan?.state === "removed" && notes?.state === "active";
    }, 20000);
    const seqAfter = active0.snapshot.seq;
    record("watcher_edit_add_delete_and_dedup", changed && seqAfter - seqBefore === 3, {
      events_enqueued: seqAfter - seqBefore,
      expected: 3,
      note: "two identical writes of travel.md produce one upsert",
    });

    // 5. Offline retry with the same event ids.
    flaky.failures = 1;
    const sentBefore = flaky.sentIds.length;
    await writeFile(path.join(folder, "offline.md"), "# Offline\n\nWritten while the network was down.\n");
    await active0.sync();
    const queued = active0.snapshot.outbox.length === 1 && active0.snapshot.outbox[0]?.attempts === 1;
    await new Promise((r) => setTimeout(r, 1200)); // first backoff is 1 s
    await active0.flush();
    const delivered = await waitFor(async () => idle() && (await items()).some((i) => i.path === "offline.md" && i.state === "active"), 10000);
    const attempts = flaky.sentIds.slice(sentBefore).filter((ids) => ids.length);
    const sameIds = attempts.length >= 2 && attempts[0]?.[0] === attempts[1]?.[0];
    record("offline_retry_same_event_ids", queued && delivered && sameIds, {
      attempts: attempts.length,
      simulated: "first send returned a network failure (no request reached the server)",
    });

    // 6. Server view matches the folder.
    const last = await items();
    const active = last.filter((i) => i.state === "active").map((i) => i.path).sort();
    record(
      "server_view_matches_folder",
      JSON.stringify(active) === JSON.stringify(["offline.md", "projects/notes.txt", "travel.md"]),
      { active },
    );

    // 8. Account isolation: another account signs in on this desktop.
    const email2 = process.env.EAIOS_SMOKE_EMAIL2 ?? "";
    const token2 = email2 ? await login(opts.apiBase, tenant, email2, process.env.EAIOS_SMOKE_PASSWORD2 ?? "") : null;
    if (!token2) {
      record("account_switch_isolation", false, { reason: "second account not provided or sign-in failed" });
    } else {
      await manager.signIn(token2);
      const visible = manager.list().length;
      let refused = false;
      try {
        await manager.syncNow(entry.folderId);
      } catch {
        refused = true;
      }
      await writeFile(path.join(folder, "while-switched.md"), "# Switched\n\nWritten while another account was signed in.\n");
      await new Promise((r) => setTimeout(r, 3000));
      const notUploaded = !(await items()).some((i) => i.path === "while-switched.md");
      await manager.signIn(token); // back to the owner: catch-up scan and queue resume
      engine = manager.engine(entry.folderId);
      const resumed = await waitFor(
        async () => (await items()).some((i) => i.path === "while-switched.md" && i.state === "active"),
        20000,
      );
      record("account_switch_isolation", visible === 0 && refused && notUploaded && resumed, {
        folders_visible_to_other_account: visible,
        other_account_action_refused: refused,
        not_uploaded_while_switched: notUploaded,
        resumed_after_owner_signed_in: resumed,
      });
    }

    // 9. An unreadable subtree keeps its files (POSIX permissions only).
    if (process.platform === "win32") {
      record("unreadable_subtree_kept", true, {
        not_applicable: "POSIX permission bits; the engine tests cover Windows with an injected scan",
      });
    } else {
      const projects = path.join(folder, "projects");
      await chmod(projects, 0o000);
      try {
        await engine?.sync();
        const status = statuses.at(-1);
        const stillThere = (await items()).some((i) => i.path === "projects/notes.txt" && i.state === "active");
        const noDelete = !engine?.snapshot.outbox.some((e) => e.op === "delete");
        record("unreadable_subtree_kept", stillThere && noDelete && (status?.unreadable ?? 0) > 0, {
          unreadable_reported: status?.unreadable,
          detail: status?.detail,
        });
      } finally {
        await chmod(projects, 0o755);
      }
      await engine?.sync();
    }

    // 10. Remove-from-organization survives a failed request and a restart.
    const temp = path.join(work, "Temporary folder");
    await mkdir(temp, { recursive: true });
    await writeFile(path.join(temp, "temp.md"), "# Temporary\n\nRemoved again by the smoke run.\n");
    const tempEntry = await manager.add(temp);
    shared.deleteFailures = 1;
    const pending = await manager.remove(tempEntry.folderId, true);
    const keptVisible = Boolean(pending?.pendingRemoval && manager.list().some((f) => f.folderId === tempEntry.folderId));
    await manager.stopAll(); // "restart": a new manager over the same app-data folder
    manager = makeManager();
    await manager.load();
    await manager.signIn(token);
    engine = manager.engine(entry.folderId);
    const goneLocally = !manager.list().some((f) => f.folderId === tempEntry.folderId);
    const server = await fetch(`${opts.apiBase}/api/v1/local-folders/${tempEntry.sourceId}/items`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    record("removal_retried_after_failure_and_restart", keptVisible && goneLocally && server.status === 404, {
      visible_while_pending: keptVisible,
      removed_after_restart: goneLocally,
      server_status_after: server.status,
      simulated: "first delete request returned a network failure",
    });
  } catch (err) {
    record("unexpected_error", false, { message: err instanceof Error ? err.message : String(err) });
  } finally {
    await manager?.stopAll();
    await rm(work, { recursive: true, force: true }).catch(() => undefined);
  }
  const ok = steps.length > 0 && steps.every((s) => s.ok);
  const report = {
    kind: "desktop_smoke",
    result: ok ? "passed" : "failed",
    started_at: started,
    finished_at: new Date().toISOString(),
    platform: { os: process.platform, release: os.release(), arch: process.arch },
    versions: {
      electron: process.versions.electron,
      node: process.versions.node,
      chrome: process.versions.chrome,
    },
    api_base: opts.apiBase,
    steps,
  };
  await writeFile(reportPath, JSON.stringify(report, null, 2));
  process.stdout.write(`desktop smoke ${report.result}: ${reportPath}\n`);
  return ok ? 0 : 1;
}
