/**
 * Minimal API client. The access token is passed in explicitly and lives only in
 * React state (memory). It is never written to localStorage, sessionStorage or
 * cookies; a reload requires signing in again (ADR 0101).
 */

// `?.`: node:test loads this module without Vite, where import.meta.env is undefined.
export const API_BASE_URL: string = (import.meta.env?.VITE_API_BASE_URL ?? "http://localhost:8000")
  .replace(/\/+$/, "");

export interface ApiErrorBody {
  code: string;
  message: string;
  request_id: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.status = status;
    this.code = body.code;
    this.requestId = body.request_id;
  }
}

export interface Identity {
  user_id: string;
  email: string;
  display_name: string;
  tenant: { id: string; slug: string; name: string };
  roles: string[];
  capabilities: { capability: string; scope_department: string | null }[];
  groups: string[];
  attributes: { clearance: string | null; country: string | null; department: string | null };
  policy_epoch: number;
  session_expires_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_at: string;
}

export interface Readiness {
  status: "ready" | "not_ready";
  checks: Record<string, string>;
  model_inference: string;
}

async function request<T>(path: string, init: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
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

export const api = {
  login: (tenant: string, email: string, password: string) =>
    request<TokenResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ tenant, email, password }),
    }),
  me: (token: string) => request<Identity>("/api/v1/me", {}, token),
  logout: (token: string) => request<undefined>("/api/v1/auth/logout", { method: "POST" }, token),
  live: () => request<{ status: string }>("/health"),
  ready: async (): Promise<Readiness> => {
    const response = await fetch(`${API_BASE_URL}/ready`, { cache: "no-store", credentials: "omit" });
    return (await response.json()) as Readiness;
  },
};
