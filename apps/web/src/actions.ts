/**
 * M6 actions API: email drafts from verified reports, approval, send, cancel and
 * review-only scheduled briefings. Approval tokens are kept in memory only.
 */

import { API_BASE_URL, ApiError, type ApiErrorBody } from "./api.js";

export interface Recipient {
  user_id: string;
  display_name: string;
  email: string;
}

export interface Delivery {
  execution_key: string;
  message_id: string;
  status: string;
  status_reason: string | null;
  attempts: number;
  smtp_accepted_at: string | null;
  confirmed_at: string | null;
  uncertain_since: string | null;
  mailpit_id: string | null;
}

export interface Draft {
  action_id: string;
  status: string;
  status_reason: string | null;
  origin: "user" | "briefing";
  source_run_id: string;
  version: number;
  draft_sha256: string;
  recipient: Recipient;
  subject: string | null;
  body: string | null;
  content_withheld: boolean;
  edited: boolean;
  approval_expires_at: string | null;
  delivery: Delivery | null;
  created_at: string;
  updated_at: string;
}

export interface Approval {
  approval_token: string;
  action_id: string;
  version: number;
  draft_sha256: string;
  expires_at: string;
}

export interface Occurrence {
  occurrence_id: string;
  due_at: string;
  status: string;
  status_reason: string | null;
  run_id: string | null;
  action_id: string | null;
}

export interface Schedule {
  schedule_id: string;
  question: string;
  recipient: Recipient;
  interval_seconds: number;
  next_run_at: string;
  active: boolean;
  created_at: string;
  occurrences: Occurrence[];
}

async function call<T>(token: string, path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}/api/v1/actions${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "omit",
    cache: "no-store",
  });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error =
      data && typeof data === "object" && "error" in data
        ? (data as { error: ApiErrorBody }).error
        : { code: "http_error", message: `Request failed (${response.status})`, request_id: null };
    throw new ApiError(response.status, error);
  }
  return data as T;
}

const ref = (d: Draft) => ({ version: d.version, draft_sha256: d.draft_sha256 });

export const actions = {
  recipients: (token: string) => call<Recipient[]>(token, "/recipients"),
  drafts: (token: string) => call<Draft[]>(token, "/email-drafts"),
  draft: (token: string, id: string) => call<Draft>(token, `/email-drafts/${id}`),
  create: (token: string, sourceRunId: string, recipientUserId: string) =>
    call<Draft>(token, "/email-drafts", "POST", {
      source_run_id: sourceRunId,
      recipient_user_id: recipientUserId,
    }),
  edit: (token: string, d: Draft, recipientUserId: string, subject: string, body: string) =>
    call<Draft>(token, `/email-drafts/${d.action_id}`, "PUT", {
      ...ref(d),
      recipient_user_id: recipientUserId,
      subject,
      body,
    }),
  approve: (token: string, d: Draft) =>
    call<Approval>(token, `/email-drafts/${d.action_id}/approve`, "POST", ref(d)),
  send: (token: string, d: Draft, approval: Approval) =>
    call<Draft>(token, `/email-drafts/${d.action_id}/send`, "POST", {
      ...ref(d),
      approval_token: approval.approval_token,
    }),
  cancel: (token: string, d: Draft) =>
    call<Draft>(token, `/email-drafts/${d.action_id}/cancel`, "POST"),
  schedules: (token: string) => call<Schedule[]>(token, "/briefings"),
  schedule: (
    token: string,
    question: string,
    recipientUserId: string,
    intervalSeconds: number,
    firstRunAt: Date,
  ) =>
    call<Schedule>(token, "/briefings", "POST", {
      question,
      recipient_user_id: recipientUserId,
      interval_seconds: intervalSeconds,
      first_run_at: firstRunAt.toISOString(),
    }),
  deactivate: (token: string, id: string) =>
    call<Schedule>(token, `/briefings/${id}/deactivate`, "POST"),
};

/** Plain-language delivery states, including the explicit uncertain state. */
export const STATUS_TEXT: Record<string, string> = {
  draft: "Draft — not approved",
  approved: "Approved — not sent yet",
  queued: "Queued for delivery",
  sending: "Sending",
  uncertain:
    "Delivery uncertain — the mail server may have accepted it. It is being checked against Mailpit and will not be re-sent blindly.",
  delivered: "Delivered to Mailpit",
  blocked: "Blocked — access or settings changed before sending",
  failed: "Failed — not delivered",
  cancelled: "Cancelled",
};
