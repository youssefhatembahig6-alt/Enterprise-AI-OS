/**
 * Electron main process (M5, ADR 0503, source ADR 0009).
 *
 * The window shows the same React app as the web (apps/web build), served from the
 * privileged ``app://eaios`` scheme with a strict Content-Security-Policy. The renderer
 * is sandboxed with context isolation; ``preload.ts`` exposes a handful of calls.
 * The main process owns the local-folder watchers (``FolderManager``). The session
 * token comes from the renderer after sign-in and is kept in memory only. Folders are
 * bound to the account that added them: only that account's watchers run while it is
 * signed in; after a restart or an account switch, a catch-up scan on its next sign-in
 * finds what changed meanwhile and its queued events resume.
 *
 * ``--smoke`` runs a scripted end-to-end check against a real backend and writes a
 * JSON report (see ``smoke.ts``).
 */

import { app, BrowserWindow, dialog, ipcMain, net, protocol, session } from "electron";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { FolderManager } from "./folders";
import { runSmoke } from "./smoke";
import { HttpTransport } from "./sync/transport";

const SCHEME = "app";
const HOST = "eaios";
const API_BASE = (process.env.EAIOS_API_BASE_URL ?? "http://localhost:8000").replace(/\/+$/, "");
const WEB_DIST = process.env.EAIOS_WEB_DIST ?? path.resolve(__dirname, "..", "..", "..", "web", "dist");
const SMOKE = process.argv.includes("--smoke");

protocol.registerSchemesAsPrivileged([
  { scheme: SCHEME, privileges: { standard: true, secure: true, supportFetchAPI: true } },
]);

let manager: FolderManager | null = null;
let window: BrowserWindow | null = null;

export const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  `connect-src 'self' ${API_BASE}`,
  "img-src 'self' data:",
  "font-src 'self' data:",
  "object-src 'none'",
  "base-uri 'none'",
  "frame-ancestors 'none'",
  "form-action 'none'",
].join("; ");

function serveWebApp(): void {
  protocol.handle(SCHEME, async (request) => {
    const url = new URL(request.url);
    if (url.host !== HOST) return new Response("Not found", { status: 404 });
    const relative = decodeURIComponent(url.pathname).replace(/^\/+/, "") || "index.html";
    const file = path.resolve(WEB_DIST, relative);
    const inside = file === WEB_DIST || file.startsWith(WEB_DIST + path.sep);
    const target = inside && existsSync(file) ? file : path.join(WEB_DIST, "index.html");
    const response = await net.fetch(pathToFileURL(target).toString());
    const headers = new Headers(response.headers);
    headers.set("Content-Security-Policy", CSP);
    headers.set("X-Content-Type-Options", "nosniff");
    return new Response(response.body, { status: response.status, headers });
  });
}

function createWindow(show: boolean): BrowserWindow {
  const win = new BrowserWindow({
    width: 1200,
    height: 820,
    show,
    title: "Enterprise AI OS",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: false,
    },
  });
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (event, target) => {
    if (!target.startsWith(`${SCHEME}://${HOST}/`)) event.preventDefault();
  });
  void win.loadURL(`${SCHEME}://${HOST}/index.html`);
  return win;
}

function broadcast(status: unknown): void {
  window?.webContents.send("eaios:status", status);
}

function registerIpc(folders: FolderManager): void {
  // Folders belong to the signed-in account (tenant and user): sign-in starts only its
  // watchers and resumes its queue; sign-out or another account stops them.
  ipcMain.handle("eaios:session", async (_e, value: unknown) => {
    const token = typeof value === "string" && value.length < 4096 ? value : null;
    if (token) await folders.signIn(token);
    else await folders.signOut();
  });
  ipcMain.handle("eaios:choose-folder", async () => {
    const result = await dialog.showOpenDialog({ properties: ["openDirectory"] });
    return result.canceled ? null : (result.filePaths[0] ?? null);
  });
  ipcMain.handle("eaios:add-folder", async (_e, root: unknown) => {
    if (typeof root !== "string" || !path.isAbsolute(root)) throw new Error("invalid_folder");
    return folders.add(root);
  });
  ipcMain.handle("eaios:remove-folder", async (_e, folderId: unknown, fromServer: unknown) => {
    if (typeof folderId !== "string") throw new Error("invalid_folder");
    return folders.remove(folderId, fromServer === true);
  });
  ipcMain.handle("eaios:list-folders", async () => folders.list());
  ipcMain.handle("eaios:sync-now", async (_e, folderId: unknown) => {
    if (typeof folderId === "string") await folders.syncNow(folderId);
  });
}

app.on("web-contents-created", (_event, contents) => {
  contents.on("will-attach-webview", (event) => event.preventDefault());
});

void app.whenReady().then(async () => {
  serveWebApp();
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  if (SMOKE) {
    // The smoke drives its own folder manager; the renderer's session call is a no-op.
    ipcMain.handle("eaios:session", () => undefined);
    const code = await runSmoke({ apiBase: API_BASE, createWindow: () => createWindow(false) });
    app.exit(code);
    return;
  }
  manager = new FolderManager({
    dataDir: path.join(app.getPath("userData"), "local-folders"),
    makeTransport: (token) => new HttpTransport(API_BASE, token),
    onStatus: broadcast,
  });
  registerIpc(manager);
  await manager.load();
  window = createWindow(true);
});

app.on("window-all-closed", () => {
  void manager?.stopAll().finally(() => app.quit());
});
