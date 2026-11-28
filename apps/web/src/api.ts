/**
 * Minimal API client. The access token is passed in explicitly and lives only in
 * React state (memory). It is never written to localStorage, sessionStorage or
 * cookies; a reload requires signing in again (ADR 0101).
 */

import { fetchCredentials } from "./fetchPolicy.js";

// `?.`: node:test loads this module without Vite, where import.meta.env is undefined.
export const API_BASE_URL: string = (import.meta.env?.VITE_API_BASE_URL ?? "http://localhost:8000")
  .replace(/\/+$/, "");

/** Automated UI tests only: let the chat use the labelled fake model as a test double
 * (modelStatusView.ts). Normal builds leave it unset and are real-model only. */
export const TEST_ALLOW_FAKE_MODEL: boolean = import.meta.env?.VITE_TEST_ALLOW_FAKE_MODEL === "true";

/** "omit" everywhere except the private Colab preview build (see fetchPolicy.ts). */
export const FETCH_CREDENTIALS: RequestCredentials = fetchCredentials(
  import.meta.env?.VITE_FETCH_CREDENTIALS,
  API_BASE_URL,
);

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
    credentials: FETCH_CREDENTIALS,
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

export interface ModelStatus {
  mode: "fake" | "gateway";
  verified: boolean;
  authenticated?: boolean;
  chat: { model: string | null; revision: string | null } | null;
  engine: Record<string, unknown> | null;
  error: string | null;
  label: string | null;
  checked_at: number;
}

/** Plain-language reason for a model failure class (backend ModelError.code). */
export function modelFailureText(detail: string | null | undefined): string | null {
  switch (detail) {
    case "not_configured":
      return "no model gateway is configured on the server";
    case "unavailable":
      return "the model gateway could not be reached";
    case "timeout":
      return "the model gateway did not answer in time";
    case "auth_failed":
      return "the model gateway rejected the server's credentials";
    case "revision_mismatch":
      return "the gateway is not serving the pinned model and revision";
    case "chat_server_down":
      return "the gateway is up but its Qwen chat server is not ready";
    case "protocol_error":
      return "the model gateway sent an unexpected response";
    case "empty_reply":
      return "the model returned an empty reply";
    default:
      return null;
  }
}

export const api = {
  login: (tenant: string, email: string, password: string) =>
    request<TokenResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ tenant, email, password }),
    }),
  me: (token: string) => request<Identity>("/api/v1/me", {}, token),
  modelStatus: (token: string, refresh = false) =>
    request<ModelStatus>(`/api/v1/model/status${refresh ? "?refresh=true" : ""}`, {}, token),
  logout: (token: string) => request<undefined>("/api/v1/auth/logout", { method: "POST" }, token),
  live: () => request<{ status: string }>("/health"),
  ready: async (): Promise<Readiness> => {
    const response = await fetch(`${API_BASE_URL}/ready`, { cache: "no-store", credentials: FETCH_CREDENTIALS });
    return (await response.json()) as Readiness;
  },
};
