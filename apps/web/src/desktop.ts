/**
 * The desktop shell's preload bridge (apps/desktop/src/preload.ts). Present only when
 * this React app runs inside Electron; the web build never has it. The session token
 * is handed to the main process in memory, never persisted.
 */

import { API_BASE_URL, ApiError, type ApiErrorBody } from "./api";

export interface DesktopSyncStatus {
  folderId: string;
  phase: "idle" | "syncing" | "offline" | "signed_out" | "folder_gone" | "error";
  pending: number;
  skipped: number;
  rejected: number;
  lastSyncAt: number | null;
  detail: string | null;
}

export interface DesktopFolder {
  folderId: string;
  sourceId: string | null;
  name: string;
  root: string;
  status: DesktopSyncStatus | null;
}

export interface DesktopBridge {
  version: number;
  setSession(token: string | null): Promise<void>;
  chooseFolder(): Promise<string | null>;
  addFolder(root: string): Promise<DesktopFolder>;
  removeFolder(folderId: string): Promise<void>;
  listFolders(): Promise<DesktopFolder[]>;
  syncNow(folderId: string): Promise<void>;
  onStatus(listener: (status: DesktopSyncStatus) => void): () => void;
}

declare global {
  interface Window {
    eaiosDesktop?: DesktopBridge;
  }
}

export function desktop(): DesktopBridge | null {
  return typeof window !== "undefined" && window.eaiosDesktop ? window.eaiosDesktop : null;
}

export interface ServerFolder {
  source_id: string;
  folder_id: string;
  name: string;
  device: string | null;
  department: string;
  classification: string;
  shared_groups: string[];
  active_items: number;
  skipped_items: number;
  access: string;
}

async function call<T>(token: string, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}/api/v1/local-folders${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    credentials: "omit",
    cache: "no-store",
  });
  if (response.status === 204) return undefined as T;
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error =
      body && typeof body === "object" && "error" in body
        ? (body as { error: ApiErrorBody }).error
        : { code: "http_error", message: `Request failed (${response.status})`, request_id: null };
    throw new ApiError(response.status, error);
  }
  return body as T;
}

export const localFolders = {
  list: (t: string) => call<ServerFolder[]>(t, ""),
  share: (t: string, id: string, groups: string[]) =>
    call<ServerFolder>(t, `/${id}/sharing`, { method: "PUT", body: JSON.stringify({ groups }) }),
  remove: (t: string, id: string) => call<undefined>(t, `/${id}`, { method: "DELETE" }),
};
