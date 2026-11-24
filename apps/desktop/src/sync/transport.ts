/**
 * HTTP client for the backend's local-folder routes. The session token is supplied
 * by the signed-in renderer and kept in memory only (ADR 0101).
 */

export interface WireEvent {
  event_id: string;
  seq: number;
  op: "upsert" | "delete";
  path: string;
  sha256?: string;
  size?: number;
  content_base64?: string;
}

export interface EventResult {
  event_id: string;
  path: string;
  result: string;
  reason?: string;
  document_id?: string;
}

export type SendOutcome =
  | { kind: "ok"; results: EventResult[] }
  | { kind: "retry"; reason: string } // offline, timeout, 5xx, 429
  | { kind: "auth" } // no or expired session
  | { kind: "gone" } // folder deleted or not ours
  | { kind: "invalid"; code: string }; // the batch itself was refused

export interface FolderInfo {
  source_id: string;
  folder_id: string;
  name: string;
  shared_groups: string[];
}

export interface Transport {
  send(sourceId: string, events: WireEvent[]): Promise<SendOutcome>;
  register(folderId: string, name: string, device: string): Promise<FolderInfo>;
}

export class HttpTransport implements Transport {
  constructor(
    private readonly apiBase: string,
    private readonly token: () => string | null,
    private readonly timeoutMs = 30_000,
  ) {}

  private async call(path: string, init: RequestInit): Promise<Response> {
    const token = this.token();
    if (!token) throw new AuthMissing();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      return await fetch(`${this.apiBase.replace(/\/+$/, "")}${path}`, {
        ...init,
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  }

  async send(sourceId: string, events: WireEvent[]): Promise<SendOutcome> {
    let response: Response;
    try {
      response = await this.call(`/api/v1/local-folders/${sourceId}/events`, {
        method: "POST",
        body: JSON.stringify({ events }),
      });
    } catch (err) {
      if (err instanceof AuthMissing) return { kind: "auth" };
      return { kind: "retry", reason: err instanceof Error ? err.name : "network" };
    }
    if (response.status === 200) {
      const body = (await response.json()) as { results: EventResult[] };
      return { kind: "ok", results: body.results };
    }
    if (response.status === 401) return { kind: "auth" };
    if (response.status === 404) return { kind: "gone" };
    if (response.status === 429 || response.status >= 500) {
      return { kind: "retry", reason: String(response.status) };
    }
    const body = (await response.json().catch(() => null)) as { error?: { code?: string } } | null;
    return { kind: "invalid", code: body?.error?.code ?? String(response.status) };
  }

  async register(folderId: string, name: string, device: string): Promise<FolderInfo> {
    const response = await this.call("/api/v1/local-folders", {
      method: "POST",
      body: JSON.stringify({ folder_id: folderId, name, device }),
    });
    if (response.status !== 201) {
      const body = (await response.json().catch(() => null)) as { error?: { code?: string } } | null;
      throw new Error(body?.error?.code ?? `register failed (${response.status})`);
    }
    return (await response.json()) as FolderInfo;
  }
}

export class AuthMissing extends Error {}
