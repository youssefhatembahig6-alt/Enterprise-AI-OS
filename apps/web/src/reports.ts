/**
 * Report runs over Server-Sent Events (docs/contracts/reports.md).
 *
 * POST /api/v1/chat is read with fetch (so the bearer token stays in a header) and a
 * small SSE parser. Fetch has no automatic reconnection: on a dropped stream the
 * caller replays from GET /api/v1/runs/{id}/events?after=<last id>.
 */

import { API_BASE_URL, ApiError, type ApiErrorBody } from "./api.js";

export type ReportEventType =
  | "status"
  | "token"
  | "citation"
  | "approval_required"
  | "error"
  | "done";

export interface ReportEvent {
  id: number | null;
  event: ReportEventType;
  data: Record<string, unknown>;
}

export interface TableCell {
  column: string;
  value: string | number;
  display: string;
  citation_id: string;
  ref: { evidence_id: string; row: number; column: string };
}

export interface ReportTable {
  columns: { key: string; label: string; type: "integer" | "money" }[];
  rows: TableCell[][];
}

export interface Citation {
  citation_id: string;
  // M2 report citations reference a SQL evidence cell.
  evidence_id?: string;
  row?: number;
  column?: string;
  label?: string;
  // M3 document citations reference an exact span of a document version.
  document_id?: string;
  title?: string;
  version?: number;
  page?: number | null;
  heading_path?: string[];
  quote?: string;
  // M5 code citations: repository, file, revision and exact lines.
  repository?: string;
  path?: string;
  commit?: string;
  line_start?: number;
  line_end?: number;
  url?: string;
}

export interface DocumentCitation {
  citation_id: string;
  document_id: string;
  title: string;
  version: number;
  current_version: number;
  is_current_version: boolean;
  filename: string;
  char_start: number;
  char_end: number;
  offset_unit: "unicode_code_point";
  text_sha256: string;
  before: string;
  quote: string;
  after: string;
  integrity: "verified" | "mismatch";
  repository?: string | null;
  path?: string | null;
  commit?: string | null;
  line_start?: number | null;
  line_end?: number | null;
  url?: string | null;
}

export type AskMode = "auto" | "documents" | "report" | "legal" | "code" | "general";

export interface Verification {
  label: string;
  narrative: string;
  inference_mode: "gateway" | "fake";
  inference_label?: string;
  /** M8 general replies: the served chat model that produced it (gateway mode). */
  model?: { name: string | null; revision: string | null };
  checker: null | {
    model: string;
    revision: string;
    status: string;
    threshold: number;
    threshold_status: string;
  };
}

export interface DonePayload {
  message_id: string | null;
  outcome: "published" | "abstained" | "failed" | "cancelled";
  verification: Verification | null;
  abstention: { code: string; message: string } | null;
  inference: string | null;
}

export interface Evidence {
  evidence_id: string;
  run_id: string;
  report_key: string;
  executed_sql: string;
  parameters: Record<string, unknown>;
  columns: { name: string; type: string }[];
  rows: (string | number | null)[][];
  row_count: number;
  semantics: Record<string, unknown>;
  dataset_fingerprint: string;
  policy_epoch: number;
  executed_at: string;
  result_sha256: string;
  integrity: "verified" | "mismatch";
}

/** Incremental SSE parser: feed text chunks, get complete events back. */
export class SseParser {
  private buffer = "";

  push(chunk: string): ReportEvent[] {
    this.buffer += chunk.replace(/\r\n/g, "\n");
    const events: ReportEvent[] = [];
    let index = this.buffer.indexOf("\n\n");
    while (index >= 0) {
      const block = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 2);
      const event = parseBlock(block);
      if (event) events.push(event);
      index = this.buffer.indexOf("\n\n");
    }
    return events;
  }
}

function parseBlock(block: string): ReportEvent | null {
  let id: number | null = null;
  let event: string | null = null;
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue; // comment = heartbeat
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "id") id = Number.parseInt(value, 10);
    else if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (!event || data.length === 0) return null;
  return { id, event: event as ReportEventType, data: JSON.parse(data.join("\n")) };
}

async function errorFrom(response: Response): Promise<ApiError> {
  const body: unknown = await response.json().catch(() => null);
  const error =
    body && typeof body === "object" && "error" in body
      ? (body as { error: ApiErrorBody }).error
      : { code: "http_error", message: `Request failed (${response.status})`, request_id: null };
  return new ApiError(response.status, error);
}

async function readStream(
  response: Response,
  onEvent: (event: ReportEvent) => void,
): Promise<"done" | "dropped"> {
  if (!response.ok) throw await errorFrom(response);
  if (!response.body) return "dropped";
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = new SseParser();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return "dropped";
    for (const event of parser.push(value)) {
      onEvent(event);
      if (event.event === "done") return "done";
      if (event.event === "error" && event.id === null) return "done"; // stream-level refusal
    }
  }
}

function headers(token: string): HeadersInit {
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

export async function startReport(
  token: string,
  question: string,
  clientRequestId: string,
  onEvent: (event: ReportEvent) => void,
  signal: AbortSignal,
  mode: AskMode = "auto",
): Promise<{ runId: string | null; ended: "done" | "dropped" }> {
  const response = await fetch(`${API_BASE_URL}/api/v1/chat`, {
    method: "POST",
    headers: headers(token),
    body: JSON.stringify({ question, client_request_id: clientRequestId, mode }),
    credentials: "omit",
    cache: "no-store",
    signal,
  });
  const runId = response.headers.get("X-Run-ID");
  return { runId, ended: await readStream(response, onEvent) };
}

export async function resumeReport(
  token: string,
  runId: string,
  after: number,
  onEvent: (event: ReportEvent) => void,
  signal: AbortSignal,
): Promise<"done" | "dropped"> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/runs/${encodeURIComponent(runId)}/events?after=${after}`,
    { headers: headers(token), credentials: "omit", cache: "no-store", signal },
  );
  return readStream(response, onEvent);
}

export async function cancelReport(token: string, runId: string): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/v1/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    headers: headers(token),
    credentials: "omit",
    cache: "no-store",
  });
  if (!response.ok) throw await errorFrom(response);
}

export async function fetchEvidence(token: string, evidenceId: string): Promise<Evidence> {
  const response = await fetch(`${API_BASE_URL}/api/v1/evidence/${encodeURIComponent(evidenceId)}`, {
    headers: headers(token),
    credentials: "omit",
    cache: "no-store",
  });
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as Evidence;
}

/** The cited passage, sliced by Unicode code points on the server (no UTF-16 math here). */
export async function fetchDocumentCitation(
  token: string,
  runId: string,
  citationId: string,
): Promise<DocumentCitation> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/runs/${encodeURIComponent(runId)}/citations/${encodeURIComponent(citationId)}`,
    { headers: headers(token), credentials: "omit", cache: "no-store" },
  );
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as DocumentCitation;
}

export interface DocumentSummary {
  document_id: string;
  title: string;
  department: string;
  classification: string;
  permission_state: "verified" | "unknown";
  current_version: number;
  searchable: boolean;
  status: "queued" | "parsing" | "indexing" | "ready" | "failed" | "needs_ocr" | "unknown";
  error: { code: string; message: string } | null;
  can_administer: boolean;
  can_read: boolean;
}

export async function listDocuments(token: string): Promise<DocumentSummary[]> {
  const response = await fetch(`${API_BASE_URL}/api/v1/documents`, {
    headers: headers(token),
    credentials: "omit",
    cache: "no-store",
  });
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as DocumentSummary[];
}

export interface UploadRequest {
  filename: string;
  contentBase64: string;
  department: string;
  classification: string | null; // null: upload with unknown permissions
  groups: string[];
  departmentRestricted: boolean;
  countries: string[];
}

export async function uploadDocument(token: string, req: UploadRequest): Promise<string> {
  const body: Record<string, unknown> = {
    filename: req.filename,
    content_base64: req.contentBase64,
    department: req.department,
  };
  if (req.classification) {
    body.permissions = {
      classification: req.classification,
      department_restricted: req.departmentRestricted,
      allowed_countries: req.countries.length ? req.countries : ["*"],
      grants: req.groups.map((group) => ({ effect: "allow", group })),
    };
  }
  const response = await fetch(`${API_BASE_URL}/api/v1/documents`, {
    method: "POST",
    headers: headers(token),
    body: JSON.stringify(body),
    credentials: "omit",
    cache: "no-store",
  });
  if (!response.ok) throw await errorFrom(response);
  return ((await response.json()) as { document_id: string }).document_id;
}
