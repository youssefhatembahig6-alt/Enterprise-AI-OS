/**
 * A minimal **mock** of the backend's login and local-folder routes, for running the
 * real Electron smoke (``electron . --smoke``) where the Python backend is not
 * available (the Windows CI runner). It applies the same event rules the smoke checks
 * (event ids once, stale sequences ignored, deletes) but none of the authorization,
 * ingestion or answering. A run against it is **mocked-backend evidence**; the live
 * Debian run uses the real backend (scripts/m5_desktop_smoke.py).
 *
 *   node dist/tools/mock-backend.js --run-smoke <report.json>
 */

import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";

interface Item {
  path: string;
  state: "active" | "removed";
  reason: string | null;
  sha256: string | null;
  last_seq: number;
}

export interface MockUser {
  email: string;
  password: string;
  token: string;
  userId: string;
}

export function startMock(): Promise<{ server: Server; url: string; users: MockUser[] }> {
  const users: MockUser[] = ["smoke@mock.example", "other@mock.example"].map((email) => ({
    email,
    password: randomUUID(),
    token: randomUUID(),
    userId: randomUUID(),
  }));
  const tenantId = randomUUID();
  const folders = new Map<
    string,
    { source_id: string; owner: string; items: Map<string, Item>; seen: Map<string, string> }
  >();
  const server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const send = (status: number, body?: unknown) => {
        if (status === 204) return void res.writeHead(204).end();
        res.writeHead(status, { "Content-Type": "application/json" }).end(JSON.stringify(body ?? {}));
      };
      const body = chunks.length ? (JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>) : {};
      const url = new URL(req.url ?? "/", "http://mock");
      if (req.method === "POST" && url.pathname === "/api/v1/auth/login") {
        const user = users.find((u) => u.email === body.email && u.password === body.password);
        return user ? send(200, { access_token: user.token }) : send(401);
      }
      const user = users.find((u) => req.headers.authorization === `Bearer ${u.token}`);
      if (!user) return send(401);
      if (req.method === "GET" && url.pathname === "/api/v1/me") {
        return send(200, { user_id: user.userId, tenant: { id: tenantId } });
      }
      if (url.pathname === "/api/v1/local-folders") {
        if (req.method === "GET") {
          return send(200, [...folders.values()].filter((f) => f.owner === user.userId).map((f) => ({ source_id: f.source_id })));
        }
        const source_id = randomUUID();
        folders.set(source_id, { source_id, owner: user.userId, items: new Map(), seen: new Map() });
        return send(201, { source_id, folder_id: body.folder_id, name: body.name, shared_groups: [] });
      }
      const match = /^\/api\/v1\/local-folders\/([^/]+)(?:\/(events|items))?$/.exec(url.pathname);
      const folder = match ? folders.get(match[1] ?? "") : undefined;
      if (!match || !folder || folder.owner !== user.userId) return send(404); // not yours: not found
      if (req.method === "DELETE" && !match[2]) {
        folders.delete(folder.source_id);
        return send(204);
      }
      if (match[2] === "items") return send(200, [...folder.items.values()]);
      const events = (body.events ?? []) as {
        event_id: string;
        seq: number;
        op: string;
        path: string;
        sha256?: string;
        size?: number;
        content_base64?: string;
      }[];
      const results = events.map((e) => {
        const first = folder.seen.get(e.event_id);
        if (first) return { event_id: e.event_id, path: e.path, result: "duplicate", first_result: first };
        const item = folder.items.get(e.path);
        let result: string;
        if (item && e.seq <= item.last_seq) result = "stale";
        else if (e.op === "delete") {
          result = item?.state === "active" ? "removed" : "noop";
          folder.items.set(e.path, { path: e.path, state: "removed", reason: null, sha256: null, last_seq: e.seq });
        } else {
          const data = Buffer.from(e.content_base64 ?? "", "base64");
          const sha = createHash("sha256").update(data).digest("hex");
          if (sha !== e.sha256 || data.length !== e.size) result = "rejected";
          else {
            result = item?.state === "active" ? (item.sha256 === sha ? "unchanged" : "updated") : "created";
            folder.items.set(e.path, { path: e.path, state: "active", reason: null, sha256: sha, last_seq: e.seq });
          }
        }
        folder.seen.set(e.event_id, result);
        return { event_id: e.event_id, path: e.path, result };
      });
      return send(200, { results });
    });
  });
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () =>
      resolve({ server, url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, users }),
    ),
  );
}

async function runSmoke(reportPath: string): Promise<number> {
  const { server, url, users } = await startMock();
  const [owner, other] = users as [MockUser, MockUser];
  // In plain Node, require("electron") returns the path of the Electron binary.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const electron = require("electron") as unknown as string;
  const appDir = path.resolve(__dirname, "..", "..");
  const electronReport = `${reportPath}.electron.json`;
  const code = await new Promise<number>((resolve) => {
    const child = spawn(electron, [appDir, "--smoke"], {
      stdio: "inherit",
      env: {
        ...process.env,
        EAIOS_API_BASE_URL: url,
        EAIOS_SMOKE_TENANT: "mock",
        EAIOS_SMOKE_EMAIL: owner.email,
        EAIOS_SMOKE_PASSWORD: owner.password,
        EAIOS_SMOKE_EMAIL2: other.email,
        EAIOS_SMOKE_PASSWORD2: other.password,
        EAIOS_SMOKE_REPORT: electronReport,
      },
    });
    child.on("exit", (c) => resolve(c ?? 1));
  });
  server.close();
  let electronResult: unknown = null;
  try {
    electronResult = JSON.parse(readFileSync(electronReport, "utf8"));
  } catch {
    electronResult = null;
  }
  writeFileSync(
    reportPath,
    JSON.stringify(
      {
        kind: "m5_desktop_smoke_mocked_backend",
        backend: "mock (apps/desktop/tools/mock-backend.ts): no authorization, ingestion or answers",
        exit_code: code,
        electron: electronResult,
      },
      null,
      2,
    ),
  );
  return code;
}

if (require.main === module) {
  const index = process.argv.indexOf("--run-smoke");
  const report = index > 0 ? process.argv[index + 1] : undefined;
  if (!report) {
    process.stderr.write("usage: node mock-backend.js --run-smoke <report.json>\n");
    process.exit(2);
  }
  void runSmoke(path.resolve(report)).then((code) => process.exit(code));
}
