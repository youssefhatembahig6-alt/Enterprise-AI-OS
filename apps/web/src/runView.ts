/**
 * Pure state for one run of POST /api/v1/chat (docs/contracts/reports.md): the
 * events of one stream folded into what the UI shows. Shared by the dashboard's
 * ReportPanel and the Chat page. Type-only imports, so it also runs under node:test.
 */

import type { Citation, DonePayload, ReportEvent, ReportTable } from "./reports";

export interface Step {
  step: string;
  label: string;
  state: string;
  attempt?: number;
  maxAttempts?: number;
}

export interface Sentence {
  text: string;
  citations: string[];
}

export interface RunView {
  runId: string | null;
  lastId: number;
  steps: Step[];
  title: string | null;
  table: ReportTable | null;
  definition: Record<string, unknown> | null;
  sentences: Sentence[];
  citations: Citation[];
  done: DonePayload | null;
  error: { code: string; message: string; detail?: string } | null;
  fakeLabel: string | null;
  reconnecting: boolean;
}

export const EMPTY_RUN: RunView = {
  runId: null,
  lastId: 0,
  steps: [],
  title: null,
  table: null,
  definition: null,
  sentences: [],
  citations: [],
  done: null,
  error: null,
  fakeLabel: null,
  reconnecting: false,
};

export function newRequestId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function applyEvent(view: RunView, event: ReportEvent): RunView {
  const next: RunView = { ...view, reconnecting: false };
  if (event.id !== null) next.lastId = Math.max(view.lastId, event.id);
  const d = event.data;
  if (typeof d.inference_label === "string") next.fakeLabel = d.inference_label;
  if (!next.runId && typeof d.run_id === "string") next.runId = d.run_id;
  switch (event.event) {
    case "status": {
      const step: Step = {
        step: String(d.step),
        label: String(d.label),
        state: String(d.state),
        attempt: typeof d.attempt === "number" ? d.attempt : undefined,
        maxAttempts: typeof d.max_attempts === "number" ? d.max_attempts : undefined,
      };
      const others = view.steps.filter((s) => s.step !== step.step);
      next.steps = [...others, step];
      break;
    }
    case "token":
      if (d.part === "table") {
        next.table = d.table as ReportTable;
        next.title = typeof d.title === "string" ? d.title : null;
        next.definition = (d.definition as Record<string, unknown>) ?? null;
      } else if (d.part === "text") {
        next.sentences = [
          ...view.sentences,
          { text: String(d.text), citations: (d.citations as string[]) ?? [] },
        ];
      }
      break;
    case "citation":
      next.citations = [...view.citations, d as unknown as Citation];
      break;
    case "error":
      next.error = {
        code: String(d.code),
        message: String(d.message),
        ...(typeof d.detail === "string" ? { detail: d.detail } : {}),
      };
      break;
    case "done":
      next.done = d as unknown as DonePayload;
      break;
    case "approval_required":
      break; // reserved; never sent
  }
  return next;
}

/** What kind of answer a finished run is, for rendering. */
export type RunKind = "running" | "report" | "answer" | "refusal" | "error" | "empty";

export function runKind(view: RunView, failure: string | null): RunKind {
  if (failure || view.error) return "error";
  if (!view.done) return "running";
  if (view.done.abstention) return "refusal";
  if (view.done.outcome === "published" && view.table) return "report";
  if (view.done.outcome === "published" && view.sentences.length > 0) return "answer";
  if (view.done.outcome === "failed" || view.done.outcome === "cancelled") return "error";
  return "empty";
}

export function citationNumber(view: RunView, id: string): number {
  return view.citations.findIndex((c) => c.citation_id === id) + 1;
}

/** A citation either opens a document passage (document_id) or SQL evidence. */
export function citationKind(c: Citation): "document" | "evidence" {
  return c.document_id ? "document" : "evidence";
}
