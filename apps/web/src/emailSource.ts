/**
 * Which verified report the email form drafts from. A report the user explicitly picks
 * ("Email this report for approval") stays pinned until the user picks another one:
 * runs that finish later (a slower chat turn, a report run on the Approvals page) only
 * update `latest`, which the Approvals page can offer as a switch. Pure, for node:test.
 */

export interface ReportSource {
  runId: string;
  title: string;
}

export interface EmailSourceState {
  source: ReportSource | null; // what draft creation uses
  pinned: boolean; // true once the user explicitly chose `source`
  latest: ReportSource | null; // newest published report, chosen or not
}

export const INITIAL_EMAIL_SOURCE: EmailSourceState = { source: null, pinned: false, latest: null };

export type EmailSourceAction =
  | { type: "published"; runId: string; title: string }
  | { type: "select"; runId: string; title: string }
  | { type: "clear" };

/** Same-titled reports are told apart by their run reference. */
export function labelFor(runId: string, title: string): string {
  return `${title} · report ${runId.slice(0, 8)}`;
}

export function emailSourceReducer(state: EmailSourceState, action: EmailSourceAction): EmailSourceState {
  switch (action.type) {
    case "published": {
      const latest = { runId: action.runId, title: action.title };
      return state.pinned ? { ...state, latest } : { source: latest, pinned: false, latest };
    }
    case "select":
      return {
        ...state,
        source: { runId: action.runId, title: labelFor(action.runId, action.title) },
        pinned: true,
      };
    case "clear":
      return INITIAL_EMAIL_SOURCE;
  }
}

/** A newer report exists than the pinned one (the Approvals page offers to switch). */
export function newerThanPinned(state: EmailSourceState): ReportSource | null {
  return state.pinned && state.latest && state.latest.runId !== state.source?.runId ? state.latest : null;
}
