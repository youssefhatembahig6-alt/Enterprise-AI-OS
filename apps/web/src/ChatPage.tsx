/**
 * The signed-in app in the NileTech Assistant shell (ui-demo/eaios-ui-demo.html):
 * icon rail | chat sidebar | main canvas | Sources panel. The Ask view is the chat;
 * the Files, Approvals, Activity and Settings views host the existing panels, passed in
 * by App and kept mounted so their state survives navigation.
 *
 * Every question is one POST /api/v1/chat with the signed-in user's token. There is no
 * source picker: the server's orchestrator chooses the handler (General, Documents, Sales
 * report, Contracts or Code) and each answer shows which one replied. The server checks
 * access, verifies the answer and re-checks access whenever a citation is opened.
 *
 * Chats are saved by the server for this employee only (M8) and listed under Recents after
 * a reload or a new sign-in. Reopening a chat replays each answer from its run, so access
 * is checked again; Delete removes a chat after confirmation.
 */

import { useCallback, useEffect, useReducer, useRef, useState, type FormEvent, type ReactNode } from "react";
import { api, ApiError, modelFailureText, type Identity, type ModelStatus } from "./api";
import { statusButton, type CheckOutcome } from "./modelStatusView";
import { activeConversation, chatReducer, INITIAL_CHAT, type Conversation, type Turn } from "./chatState";
import { history } from "./history";
import { cancelReport, type Citation } from "./reports";
import { executeChatTurn, replayRun } from "./runStream";
import {
  citationKind,
  citationNumber,
  clarifyView,
  EMPTY_RUN,
  HANDLER_LABELS,
  newRequestId,
  runKind,
  type RunView,
} from "./runView";
import { EvidenceDetail, PassageDetail } from "./sourceViews";
import { GENERAL_LABEL, LEGAL_LABEL, verificationText } from "./verification";

export type ShellView = "chat" | "files" | "approvals" | "activity" | "settings";

const EMPTY_TURNS: Turn[] = [];

const INDEPENDENT_NOTE =
  "The assistant picks the right source for each question. Chats are saved to your account; each question is answered on its own (earlier messages are not sent to the assistant).";

// The prototype's icon set (stroke paths, 24×24).
const ICONS = {
  chat: '<path d="M3 11.5 12 4l9 7.5"/><path d="M5 10v10h14V10"/>',
  files: '<path d="M4 6h6l2 2h8v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/>',
  appr: '<path d="M9 11l3 3 8-8"/><path d="M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9"/>',
  act: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.8 1.2V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3.1 14H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 3.1V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 20.9 10H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  pen: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4z"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  trash: '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  up: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  theme: '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"/>',
} as const;

function Svg({ k, size }: { k: keyof typeof ICONS; size?: number }) {
  return (
    <svg
      className="i"
      viewBox="0 0 24 24"
      aria-hidden
      style={size ? { width: size, height: size } : undefined}
      dangerouslySetInnerHTML={{ __html: ICONS[k] }}
    />
  );
}

function initials(name: string): string {
  const parts = name.replace(/\(.*?\)/g, "").trim().split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "?";
}

function shortName(name: string): string {
  // Display name without its parenthetical note, e.g. "Sales Reader (unauthorized …)".
  return name.replace(/\(.*?\)/g, "").trim() || name;
}

function scopeText(identity: Identity): string {
  const dept = identity.attributes.department;
  const clearance = identity.attributes.clearance;
  const admin = identity.capabilities.some((c) => c.capability.endsWith(".admin"));
  return [
    dept ? dept.charAt(0).toUpperCase() + dept.slice(1) : "No department",
    clearance ? `up to ${clearance.charAt(0).toUpperCase() + clearance.slice(1)}` : null,
    admin ? "admin" : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function shortModel(name: string | null | undefined, revision: string | null | undefined): string {
  const base = (name ?? "model").split("/").pop() ?? "model";
  return revision ? `${base} @${revision.slice(0, 7)}` : base;
}

function verifyPills(view: RunView, served: ModelStatus | null): { text: string; warn: boolean }[] {
  const pills: { text: string; warn: boolean }[] = [];
  const v = view.done?.verification;
  if (v) {
    if (v.label === GENERAL_LABEL) {
      pills.push({ text: "General reply · no company data", warn: false });
      pills.push({ text: "Not checked against sources", warn: true });
    } else if (v.label === LEGAL_LABEL) {
      pills.push({ text: "Exact contract passages", warn: false });
      pills.push({ text: "Dates re-derived by fixed rules · no model", warn: false });
    } else if (v.label.startsWith("figures_verified_")) {
      pills.push({ text: "Numbers match query rows", warn: false });
      pills.push(
        v.narrative === "verified"
          ? { text: "Summary checked (development checker)", warn: false }
          : { text: "Summary withheld", warn: true },
      );
    } else if (v.label === "answer_quotes_verified_dev_checked") {
      pills.push({ text: "Every sentence quotes a source you can open", warn: false });
      pills.push({ text: "Claims checked (development checker)", warn: false });
    } else {
      pills.push({ text: "Verification details unavailable", warn: true });
    }
  }
  const sources = new Set(view.citations.map((c) => c.document_id ?? c.evidence_id)).size;
  if (view.done?.outcome === "published" && sources) {
    pills.push({ text: `${sources} source${sources === 1 ? "" : "s"} allowed`, warn: false });
  }
  if (view.done?.abstention && view.done.abstention.code !== "clarification_needed") {
    pills.push({ text: "No sources used", warn: true });
  }
  if (view.fakeLabel) pills.push({ text: "FAKE inference — not a model", warn: true });
  if (view.done?.inference === "gateway" && view.done.outcome === "published") {
    const m = v?.model ?? (served?.verified ? { name: served.chat?.model, revision: served.chat?.revision } : null);
    pills.push({ text: `Real model · ${m ? shortModel(m.name, m.revision) : "gateway"}`, warn: false });
  }
  return pills;
}

function citationName(c: Citation): string {
  if (citationKind(c) === "document") return `${c.repository ? `${c.repository}/` : ""}${c.title ?? "Document"}`;
  return c.label ?? c.column ?? "Query result";
}

function citationMeta(c: Citation): string {
  if (citationKind(c) === "evidence") return `SQL evidence · row ${(c.row ?? 0) + 1} · ${c.column}`;
  return [
    `version ${c.version}`,
    c.page ? `page ${c.page}` : null,
    c.line_start ? `lines ${c.line_start}–${c.line_end}` : null,
    c.heading_path?.length && !c.repository ? c.heading_path.join(" › ") : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function Composer({
  id,
  placeholder,
  value,
  onChange,
  disabled,
  onSubmit,
  onUpload,
}: {
  id: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  onSubmit: () => void;
  onUpload: () => void;
}) {
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!disabled && value.trim()) onSubmit();
  };
  return (
    <form className="composer" onSubmit={submit}>
      <input
        id={id}
        className="ask"
        value={value}
        maxLength={1000}
        placeholder={placeholder}
        autoComplete="off"
        aria-label="Your question"
        onChange={(e) => onChange(e.target.value)}
      />
      <div className="bar">
        <button type="button" className="iconbtn" aria-label="Upload a document" title="Upload a document" onClick={onUpload}>
          <Svg k="plus" />
        </button>
        <span className="sp" />
        <button type="submit" className="send" aria-label="Ask" disabled={disabled || !value.trim()}>
          <Svg k="up" />
        </button>
      </div>
    </form>
  );
}

function Answer({
  turn,
  served,
  badge,
  focused,
  onCite,
  onCancel,
  canEmail,
  onEmail,
}: {
  turn: Turn;
  served: ModelStatus | null;
  badge: string;
  focused: string | null;
  onCite: (citationId: string) => void;
  onCancel: () => void;
  canEmail: boolean;
  onEmail: (runId: string, title: string) => void;
}) {
  const { view, failure } = turn;
  const kind = runKind(view, failure);
  const verification = view.done ? verificationText(view.done) : null;
  const cite = (id: string) => {
    const c = view.citations.find((x) => x.citation_id === id);
    if (!c) return null;
    return (
      <button
        key={id}
        type="button"
        className="cite"
        aria-expanded={focused === id}
        aria-label={citationKind(c) === "document" ? `Open source: ${citationName(c)}` : `Show SQL evidence for ${citationName(c)}`}
        onClick={() => onCite(id)}
      >
        {citationNumber(view, id)}
      </button>
    );
  };
  const sentences = view.sentences.map((s, i) => (
    <p key={i}>
      {s.text} {s.citations.map(cite)}
    </p>
  ));
  const names = [...new Map(view.citations.map((c) => [citationName(c), c])).keys()];
  const finished = kind !== "running";
  const handler = view.handler ?? view.done?.handler ?? null;
  const handlerLabel = view.handlerLabel ?? (handler ? HANDLER_LABELS[handler] : null) ?? null;

  return (
    <div className="a">
      <span className="badge">{badge}</span>
      <div className="body">
        {handlerLabel ? (
          <span className={`chip handler h-${handler ?? "unknown"}`} title="Which part of the assistant answered">
            {handlerLabel}
          </span>
        ) : null}
        <div className="steps" aria-live="polite">
          {view.steps.map((s) => (
            <span key={s.step} className={`step${s.state === "retrying" ? " warn" : ""}`}>
              {s.label}
              {s.state === "retrying" && s.attempt ? ` (repair ${s.attempt - 1} of ${(s.maxAttempts ?? 3) - 1})` : ""}
            </span>
          ))}
          {turn.running && !view.done ? (
            <span className="step run">
              {view.reconnecting ? "Reconnecting…" : "Working…"}{" "}
              {view.runId ? (
                <button type="button" className="linkish" onClick={onCancel}>
                  Cancel
                </button>
              ) : null}
            </span>
          ) : null}
        </div>

        {kind === "error" ? (
          <div className="errbox" role="alert">
            {failure ?? view.error?.message ?? "The request did not complete."}
            {modelFailureText(view.error?.detail) ? ` Reason: ${modelFailureText(view.error?.detail)}.` : ""}
            {view.error?.code ? ` (${view.error.code}${view.error.detail ? ` · ${view.error.detail}` : ""})` : ""}
          </div>
        ) : null}
        {kind === "clarify" && view.done?.abstention ? <p className="clarify">{view.done.abstention.message}</p> : null}
        {kind === "refusal" && view.done?.abstention ? <p className="refuse">{view.done.abstention.message}</p> : null}
        {kind === "empty" ? <p className="note">The run ended without an answer ({view.done?.outcome}).</p> : null}

        {kind === "report" && view.table ? (
          <>
            <p>
              <b>{view.title ?? "Verified result"}</b>
            </p>
            <div className="tw">
              <table className="res">
                <thead>
                  <tr>
                    <th>Figure</th>
                    <th className="n">Value</th>
                  </tr>
                </thead>
                <tbody>
                  {view.table.rows[0]?.map((cell) => (
                    <tr key={cell.column}>
                      <td>{view.table?.columns.find((c) => c.key === cell.column)?.label ?? cell.column}</td>
                      <td className="n">
                        {cell.display} {cite(cell.citation_id)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {sentences}
            {view.definition ? (
              <p className="note">
                Definition: order date basis; included statuses {(view.definition.included_statuses as string[]).join(", ")}{" "}
                (excluded {(view.definition.excluded_statuses as string[]).join(", ") || "none"});{" "}
                {String(view.definition.tax_treatment)}; currency {String(view.definition.currency)}.
              </p>
            ) : null}
          </>
        ) : null}

        {kind === "answer" ? sentences : null}

        {finished ? (
          <div className="verify">
            {verifyPills(view, served).map((p) => (
              <span key={p.text} className={`vpill${p.warn ? " w" : ""}`}>
                {p.text}
              </span>
            ))}
          </div>
        ) : null}
        {verification && (kind === "report" || kind === "answer") ? <p className="note">{verification.text}</p> : null}
        {names.length && finished ? (
          <div className="srcrow">
            {names.map((n) => (
              <span key={n} className="chip">
                {n}
              </span>
            ))}
          </div>
        ) : null}
        {kind === "report" && canEmail && view.runId ? (
          <button
            type="button"
            className="btn small"
            onClick={() => view.runId && onEmail(view.runId, view.title ?? "Verified report")}
          >
            Email this report for approval
          </button>
        ) : null}
      </div>
    </div>
  );
}

function SourceCard({
  token,
  runId,
  citation,
  n,
  on,
  openCount,
  onOpen,
}: {
  token: string;
  runId: string | null;
  citation: Citation;
  n: number;
  on: boolean;
  openCount: number;
  onOpen: () => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (on) ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [on]);
  const doc = citationKind(citation) === "document";
  return (
    <div ref={ref} className={`pcard${on ? " on" : ""}`}>
      <div className="top">
        <span className="num">{n}</span>
        <span className="name">{citationName(citation)}</span>
      </div>
      <div className="meta">
        <span>{citationMeta(citation)}</span>
      </div>
      {openCount > 0 ? (
        <div key={openCount} className="flex flex-col gap-2 text-[12.5px]">
          {doc && runId ? (
            <PassageDetail token={token} runId={runId} citation={citation} />
          ) : !doc ? (
            <EvidenceDetail token={token} citation={citation} compact />
          ) : (
            <p className="note">This source cannot be opened yet.</p>
          )}
        </div>
      ) : (
        <>
          {doc && citation.quote ? <blockquote>{citation.quote}</blockquote> : null}
          <div className="acts">
            <button type="button" className="btn small" onClick={onOpen}>
              {doc ? "Open passage" : "Show SQL evidence"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export function ChatPage({
  token,
  identity,
  modelLabel,
  canEmail,
  showActivity,
  view,
  onNavigate,
  pages,
  onReportPublished,
  onEmailReport,
  onSignOut,
}: {
  token: string;
  identity: Identity;
  /** e.g. "FAKE model" when the server runs the labelled development double. */
  modelLabel: string | null;
  canEmail: boolean;
  showActivity: boolean;
  view: ShellView;
  onNavigate: (view: ShellView) => void;
  pages: Record<Exclude<ShellView, "chat">, ReactNode>;
  onReportPublished: (runId: string, title: string) => void;
  /** The clicked report becomes the email source (not just the latest one). */
  onEmailReport: (runId: string, title: string) => void;
  onSignOut: () => void;
}) {
  const [state, dispatch] = useReducer(chatReducer, INITIAL_CHAT);
  const [draft, setDraft] = useState("");
  // Recents: the server's list of this employee's chats, and the row awaiting a Delete confirmation.
  const [historyNote, setHistoryNote] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [open, setOpen] = useState(false); // narrow screens: rail + sidebar drawer
  const [menuOpen, setMenuOpen] = useState(false);
  const [search, setSearch] = useState<string | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [ptab, setPtab] = useState<"sources" | "media">("sources");
  const [focus, setFocus] = useState<{ turnId: string; citationId: string } | null>(null);
  // Each explicit open bumps the counter, remounting the detail view so it fetches again
  // (and the server re-checks access). 0 / missing = collapsed.
  const [opens, setOpens] = useState<Record<string, number>>({});
  const controllers = useRef(new Map<string, AbortController>());
  // M8: the model status button. Checked on sign-in, every 30 s and when clicked (a click
  // skips the server's short cache). "Connected" only after the backend confirmed the
  // pinned model and authenticated gateway access (modelStatusView.ts).
  const [served, setServed] = useState<ModelStatus | null>(null);
  const [check, setCheck] = useState<CheckOutcome>({ kind: "checking" });
  const checkSeq = useRef(0);
  const recheck = useCallback(
    (refresh: boolean) => {
      const seq = ++checkSeq.current;
      if (refresh) setCheck({ kind: "checking" });
      api.modelStatus(token, refresh).then(
        (s) => {
          if (seq !== checkSeq.current) return;
          setServed(s);
          setCheck({ kind: "status", status: s });
        },
        (err: unknown) => {
          if (seq !== checkSeq.current) return;
          setServed(null);
          setCheck(
            err instanceof ApiError && err.status === 401
              ? { kind: "session_ended" }
              : { kind: "backend_offline" },
          );
        },
      );
    },
    [token],
  );
  useEffect(() => {
    const seq = checkSeq; // the ref object (not a rendered node); bumping it drops late replies
    recheck(false);
    const timer = setInterval(() => recheck(false), 30_000);
    return () => {
      seq.current++;
      clearInterval(timer);
    };
  }, [recheck]);
  const button = statusButton(check);
  const threadEnd = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const live = controllers.current;
    return () => live.forEach((c) => c.abort());
  }, []);

  // Saved chats: listed once per sign-in (the token changes on every sign-in).
  useEffect(() => {
    let live = true;
    history.list(token).then(
      (items) => {
        if (!live) return;
        setHistoryNote(null);
        dispatch({
          type: "saved",
          items: items.map((c) => ({ serverId: c.conversation_id, title: c.title })),
        });
      },
      () => live && setHistoryNote("Saved chats could not be loaded."),
    );
    return () => {
      live = false;
    };
  }, [token]);

  const conversation = activeConversation(state);
  const turns = conversation?.turns ?? EMPTY_TURNS;
  const lastTurn = turns[turns.length - 1] ?? null;
  const sourceCount = turns.reduce((n, t) => n + t.view.citations.length, 0);

  useEffect(() => {
    threadEnd.current?.scrollIntoView({ block: "end" });
  }, [turns.length, lastTurn?.view.done, lastTurn?.view.steps.length]);

  const go = (v: ShellView) => {
    setMenuOpen(false);
    setOpen(false);
    onNavigate(v);
  };

  const ask = useCallback(
    (question: string) => {
      const text = question.trim();
      if (!text) return;
      const current = activeConversation(state);
      const conversationId = current?.id ?? `local-${newRequestId()}`;
      const turnId = `t-${newRequestId()}`;
      dispatch({ type: "ask", conversationId, turnId, question: text, view: EMPTY_RUN });
      setDraft("");
      const controller = new AbortController();
      controllers.current.set(turnId, controller);
      void executeChatTurn(
        token,
        text,
        current?.serverId ?? null,
        controller.signal,
        (next) => dispatch({ type: "view", conversationId, turnId, view: next }),
        onReportPublished,
        (start) => {
          if (start.conversationId) dispatch({ type: "bind", conversationId, serverId: start.conversationId });
        },
      ).then((failure) => {
        controllers.current.delete(turnId);
        if (controller.signal.aborted) return;
        dispatch({ type: "finish", conversationId, turnId, failure });
      });
    },
    [state, token, onReportPublished],
  );

  // Reopen a saved chat: fetch its turns, then replay each answer from its run (the server
  // re-checks current access while replaying). A clarifying question has no run.
  const openSaved = useCallback(
    (conversation: Conversation) => {
      if (conversation.loaded || !conversation.serverId) return;
      const conversationId = conversation.id;
      history.get(token, conversation.serverId).then(
        (detail) => {
          const turns: Turn[] = detail.turns.map((t) => ({
            id: `s-${t.turn_id}`,
            question: t.question,
            view: t.run_id
              ? { ...EMPTY_RUN, runId: t.run_id, handler: t.handler, handlerLabel: t.handler_label }
              : clarifyView(t.reply ?? ""),
            failure: null,
            running: Boolean(t.run_id),
          }));
          dispatch({ type: "opened", conversationId, turns });
          for (const t of turns) {
            const runId = t.view.runId;
            if (!runId) continue;
            const controller = new AbortController();
            controllers.current.set(t.id, controller);
            void replayRun(token, runId, controller.signal, (next) =>
              dispatch({
                type: "view",
                conversationId,
                turnId: t.id,
                view: { ...next, handler: next.handler ?? t.view.handler, handlerLabel: next.handlerLabel ?? t.view.handlerLabel },
              }),
            ).then((failure) => {
              controllers.current.delete(t.id);
              if (controller.signal.aborted) return;
              dispatch({ type: "finish", conversationId, turnId: t.id, failure });
            });
          }
        },
        (err: unknown) => {
          if (err instanceof ApiError && err.status === 404) {
            dispatch({ type: "removed", conversationId });
            setHistoryNote("That chat is no longer available.");
          } else {
            setHistoryNote("The chat could not be opened. Try again.");
          }
        },
      );
    },
    [token],
  );

  const removeSaved = async (conversation: Conversation) => {
    setConfirming(null);
    if (conversation.serverId) {
      try {
        await history.remove(token, conversation.serverId);
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 404)) {
          setHistoryNote("The chat could not be deleted. Try again.");
          return;
        }
      }
    }
    conversation.turns.forEach((t) => controllers.current.get(t.id)?.abort());
    dispatch({ type: "removed", conversationId: conversation.id });
    setHistoryNote(null);
  };

  const cancel = async (turn: Turn) => {
    if (!turn.view.runId) return;
    try {
      await cancelReport(token, turn.view.runId);
    } catch {
      /* the run's own events report the outcome */
    }
  };

  // A citation click focuses its card and opens it: the passage or evidence is fetched
  // from the server at that moment, so the reader's current access is checked.
  const openCitation = (turnId: string, citationId: string) => {
    const key = `${turnId}/${citationId}`;
    setFocus({ turnId, citationId });
    setOpens((o) => ({ ...o, [key]: (o[key] ?? 0) + 1 }));
    setPtab("sources");
    setPanelOpen(true);
  };

  const closePanel = () => {
    setPanelOpen(false);
    setOpens({});
    setFocus(null);
  };

  const newChat = () => {
    dispatch({ type: "new" });
    setFocus(null);
    go("chat");
  };

  const short = shortName(identity.display_name);
  const badge = (identity.tenant.name || "A").slice(0, 1).toUpperCase();
  const anyRunning = turns.some((t) => t.running);
  const recents = state.conversations.filter(
    (c) => !search || c.title.toLowerCase().includes(search.toLowerCase()),
  );
  const railButton = (v: ShellView, icon: keyof typeof ICONS, label: string) => (
    <button
      type="button"
      title={label}
      aria-label={label}
      aria-current={view === v ? "page" : undefined}
      onClick={() => go(v)}
    >
      <Svg k={icon} />
    </button>
  );

  return (
    <div className={`eaios-shell app${panelOpen && view === "chat" ? " with-panel" : ""}${open ? " open" : ""}`}>
      <nav className="rail" aria-label="Main">
        {railButton("chat", "chat", "Ask")}
        {railButton("files", "files", "Files and sources")}
        {railButton("approvals", "appr", "Approvals")}
        {showActivity ? railButton("activity", "act", "Activity log") : null}
        <span className="sep" />
        {railButton("settings", "gear", "Settings")}
        <span className="grow" />
        <button
          type="button"
          className="me"
          aria-label="Account menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((m) => !m)}
        >
          <span className="avatar">{initials(identity.display_name)}</span>
        </button>
        {menuOpen ? (
          <div className="menu" role="menu">
            <div className="who">
              <b>{identity.display_name}</b>
              <small>
                {identity.roles.join(", ") || "member"} · {identity.tenant.name}
              </small>
            </div>
            <button type="button" role="menuitem" className="row" onClick={() => go("settings")}>
              <Svg k="gear" />
              Settings
            </button>
            <button type="button" role="menuitem" className="row" onClick={onSignOut}>
              <Svg k="lock" />
              Sign out
            </button>
          </div>
        ) : null}
      </nav>

      <aside className="side" aria-label="Conversations">
        <div className="side-head">
          <span className="brand">{identity.tenant.name.replace(/\s+Solutions$/, "")} Assistant</span>
          <span className="sp" />
          <button
            type="button"
            className="iconbtn"
            aria-label="Search chats"
            aria-pressed={search !== null}
            onClick={() => setSearch((s) => (s === null ? "" : null))}
          >
            <Svg k="search" />
          </button>
        </div>
        {search !== null ? (
          <div className="side-search">
            <Svg k="search" size={16} />
            <input
              autoFocus
              value={search}
              placeholder="Search chats"
              aria-label="Search chats"
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        ) : null}
        <div className="side-scroll">
          <button type="button" className="row" onClick={newChat}>
            <Svg k="pen" />
            New chat
          </button>
          <div className="label">Recents</div>
          {historyNote ? <p className="side-empty" role="status">{historyNote}</p> : null}
          {recents.length === 0 ? (
            <p className="side-empty">
              {state.conversations.length === 0 ? "Your saved chats appear here." : "No chats match."}
            </p>
          ) : (
            recents.map((c) =>
              confirming === c.id ? (
                <div key={c.id} className="row confirm" role="group" aria-label={`Delete chat: ${c.title}`}>
                  <span className="t">Delete this chat?</span>
                  <span className="sp" />
                  <button type="button" className="btn small danger" onClick={() => void removeSaved(c)}>
                    Delete
                  </button>
                  <button type="button" className="btn small" onClick={() => setConfirming(null)}>
                    Cancel
                  </button>
                </div>
              ) : (
                <div key={c.id} className="recent" data-conversation={c.serverId ?? undefined}>
                  <button
                    type="button"
                    className="row"
                    aria-current={view === "chat" && c.id === state.activeId ? "page" : undefined}
                    disabled={!c.serverId && c.turns.some((t) => t.running)}
                    onClick={() => {
                      dispatch({ type: "select", id: c.id });
                      openSaved(c);
                      setFocus(null);
                      go("chat");
                    }}
                  >
                    <span className="t">{c.title}</span>
                  </button>
                  <button
                    type="button"
                    className="iconbtn del"
                    aria-label={`Delete chat: ${c.title}`}
                    title="Delete chat"
                    disabled={!c.serverId}
                    onClick={() => setConfirming(c.id)}
                  >
                    <Svg k="trash" size={15} />
                  </button>
                </div>
              ),
            )
          )}
        </div>
      </aside>
      <div className="scrim" aria-hidden onClick={() => setOpen(false)} />

      <main className="canvas">
        <div className="topbar">
          <div className="left">
            <button type="button" className="iconbtn menu-btn" aria-label="Open menu" onClick={() => setOpen(true)}>
              <Svg k="menu" />
            </button>
          </div>
          <div className="scope">
            <span>Limited to what</span>
            <b style={{ color: "var(--fg)" }}>{short}</b>
            <span>can open:</span>
            <span className="chip">{scopeText(identity)}</span>
            {modelLabel ? <span className="chip warn">{modelLabel}</span> : null}
            <button
              type="button"
              className={`chip status ${button.tone}`}
              title={button.title}
              aria-label={`Model status: ${button.label}. ${button.title}`}
              aria-live="polite"
              onClick={() => recheck(true)}
            >
              <span className="dot" aria-hidden />
              {button.label}
            </button>
          </div>
          <div className="right">
            {view === "chat" ? (
              <button
                type="button"
                className="iconbtn"
                aria-label="Sources and media panel"
                title="Sources and media"
                aria-pressed={panelOpen}
                onClick={() => (panelOpen ? closePanel() : setPanelOpen(true))}
              >
                <Svg k="panel" />
              </button>
            ) : null}
            <button type="button" className="iconbtn" aria-label="Appearance" title="Appearance" onClick={() => go("settings")}>
              <Svg k="theme" />
            </button>
          </div>
        </div>

        {view === "chat" && !conversation ? (
          <section className="view home">
            <h1>What can I find for you today, {short}?</h1>
            <Composer
              id="homeAsk"
              placeholder="Ask about policies, contracts, budgets, sales…"
              value={draft}
              onChange={setDraft}
              disabled={false}
              onSubmit={() => ask(draft)}
              onUpload={() => go("files")}
            />
            <p className="asknote">{INDEPENDENT_NOTE}</p>
          </section>
        ) : null}

        {view === "chat" && conversation ? (
          <section className="view convo" aria-label="Conversation">
            <div className="thread">
              {turns.map((turn) => (
                <div key={turn.id} style={{ display: "contents" }}>
                  <div className="u">
                    {turn.question}
                  </div>
                  <Answer
                    turn={turn}
                    served={served}
                    badge={badge}
                    focused={focus?.turnId === turn.id ? focus.citationId : null}
                    onCite={(id) => openCitation(turn.id, id)}
                    onCancel={() => void cancel(turn)}
                    canEmail={canEmail}
                    onEmail={(runId, title) => {
                      onEmailReport(runId, title);
                      go("approvals");
                    }}
                  />
                </div>
              ))}
              <div ref={threadEnd} />
            </div>
            <div className="dock">
              <Composer
                id="dockAsk"
                placeholder="Ask a follow-up…"
                value={draft}
                onChange={setDraft}
                disabled={anyRunning}
                onSubmit={() => ask(draft)}
                onUpload={() => go("files")}
              />
              <p className="asknote">{INDEPENDENT_NOTE}</p>
            </div>
          </section>
        ) : null}

        {(Object.keys(pages) as Exclude<ShellView, "chat">[]).map((key) => (
          <section key={key} className="view" hidden={view !== key}>
            <div className="page">{pages[key]}</div>
          </section>
        ))}
      </main>

      <aside className="panel" aria-label="Sources and media">
        <div className="panel-head">
          <div className="seg" role="tablist" aria-label="Panel tabs">
            <button type="button" role="tab" aria-selected={ptab === "sources"} onClick={() => setPtab("sources")}>
              Sources<b>{sourceCount}</b>
            </button>
            <button type="button" role="tab" aria-selected={ptab === "media"} onClick={() => setPtab("media")}>
              Media<b>0</b>
            </button>
          </div>
          <span className="sp" />
          <button type="button" className="iconbtn" aria-label="Close panel" onClick={closePanel}>
            <Svg k="close" />
          </button>
        </div>
        <div className="panel-body" role="tabpanel">
          {ptab === "media" ? (
            <p className="pempty">Charts, drafted reports and documents you open from a source appear here.</p>
          ) : sourceCount === 0 ? (
            <p className="pempty">
              Sources used in this chat appear here. Click a numbered citation in an answer to jump to its passage.
            </p>
          ) : (
            <>
              <p className="note">Opening a source asks the server again, so your current access is checked.</p>
              {turns
                .filter((t) => t.view.citations.length > 0)
                .map((t) => (
                  <div key={t.id} style={{ display: "contents" }}>
                    <div className="pgroup">{t.question}</div>
                    {t.view.citations.map((c) => {
                      const key = `${t.id}/${c.citation_id}`;
                      return (
                        <SourceCard
                          key={key}
                          token={token}
                          runId={t.view.runId}
                          citation={c}
                          n={citationNumber(t.view, c.citation_id)}
                          on={focus?.turnId === t.id && focus.citationId === c.citation_id}
                          openCount={opens[key] ?? 0}
                          onOpen={() => openCitation(t.id, c.citation_id)}
                        />
                      );
                    })}
                  </div>
                ))}
            </>
          )}
        </div>
      </aside>
    </div>
  );
}
