/**
 * Private chat history (M8): the signed-in employee's own conversations, stored by the
 * server. Answers are not stored here: each turn names its run, which is replayed through
 * GET /api/v1/runs/{id}/events, so the employee's current access is checked again.
 */

import { API_BASE_URL, ApiError, type ApiErrorBody, FETCH_CREDENTIALS } from "./api.js";

export interface ConversationSummary {
  conversation_id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface SavedTurn {
  turn_id: string;
  question: string;
  handler: string;
  handler_label: string;
  run_id: string | null;
  reply: string | null;
  created_at: string;
}

export interface ConversationDetail extends ConversationSummary {
  turns: SavedTurn[];
}

async function call<T>(token: string, path: string, method = "GET"): Promise<T> {
  const response = await fetch(`${API_BASE_URL}/api/v1/conversations${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}` },
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

export const history = {
  list: async (token: string): Promise<ConversationSummary[]> =>
    (await call<{ conversations: ConversationSummary[] }>(token, "")).conversations,
  get: (token: string, id: string) => call<ConversationDetail>(token, `/${encodeURIComponent(id)}`),
  remove: (token: string, id: string) => call<undefined>(token, `/${encodeURIComponent(id)}`, "DELETE"),
};
