/**
 * Chat page (layout after the NileTech Assistant prototype, ui-demo/eaios-ui-demo.html):
 * conversation sidebar, centred composer on an empty chat, message thread with a
 * bottom composer, and a Sources panel. Every question is one POST /api/v1/chat run
 * with the signed-in user's token; the server checks access, verifies the answer and
 * re-checks access whenever a citation is opened. History lives in memory only.
 */

import { StatusPill } from "@eaios/ui";
import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type { Identity } from "./api";
import {
  activeConversation,
  chatReducer,
  INITIAL_CHAT,
  type Turn,
} from "./chatState";
import { cancelReport, type AskMode, type Citation } from "./reports";
import { executeRun } from "./runStream";
import { citationKind, citationNumber, EMPTY_RUN, newRequestId, runKind, type RunView } from "./runView";
import { EvidenceDetail, PassageDetail } from "./sourceViews";
import { verificationText } from "./verification";

const MODE_LABELS: Record<AskMode, string> = {
  auto: "Automatic",
  documents: "Documents you can access",
  report: "Sales report (SQL)",
  legal: "Contracts (clauses and expiry)",
  code: "Code repositories",
};

interface Suggestion {
  question: string;
  mode: AskMode;
  pack: string;
}

const SUGGESTIONS: Suggestion[] = [
  { question: "What were net (tax-exclusive) fulfilled sales for Q1 2026, in USD?", mode: "report", pack: "sales_reports" },
  { question: "Why did collections lag revenue in Q1 2026?", mode: "documents", pack: "documents" },
  { question: "When do our contracts expire?", mode: "legal", pack: "legal" },
];

const INDEPENDENT_NOTE =
  "Each question is answered on its own: earlier messages in this chat are not sent to the assistant.";

function Icon({ d, label }: { d: string; label?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-[18px] w-[18px] flex-none fill-none stroke-current"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={label ? undefined : true}
      aria-label={label}
    >
      <path d={d} />
    </svg>
  );
}

const ICONS = {
  pen: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4",
  menu: "M4 7h16M4 12h16M4 17h16",
  panel: "M3 4h18v16H3zM15 4v16",
  close: "M6 6l12 12M18 6L6 18",
  send: "M5 12h14M13 6l6 6-6 6",
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  out: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10",
};

function IconButton({
  label,
  icon,
  onClick,
  pressed,
  className = "",
}: {
  label: string;
  icon: keyof typeof ICONS;
  onClick: () => void;
  pressed?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={pressed}
      onClick={onClick}
      className={`grid h-8 w-8 place-items-center rounded-lg text-muted-fg hover:bg-muted hover:text-fg ${className}`}
    >
      <Icon d={ICONS[icon]} />
    </button>
  );
}

function Composer({
  id,
  placeholder,
  value,
  onChange,
  mode,
  onMode,
  modes,
  disabled,
  onSubmit,
}: {
  id: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  mode: AskMode;
  onMode: (m: AskMode) => void;
  modes: AskMode[];
  disabled: boolean;
  onSubmit: () => void;
}) {
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!disabled && value.trim()) onSubmit();
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!disabled && value.trim()) onSubmit();
    }
  };
  return (
    <form
      onSubmit={submit}
      className="flex w-full max-w-[680px] flex-col gap-1 rounded-[28px] border border-border bg-card px-3 py-2 shadow-[0_6px_24px_-14px_rgba(15,23,42,0.35)]"
    >
      <label htmlFor={id} className="sr-only">
        Your question
      </label>
      <textarea
        id={id}
        rows={1}
        maxLength={1000}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKey}
        className="max-h-40 min-h-[40px] w-full resize-none bg-transparent px-1 py-2 text-[15px] outline-none placeholder:text-muted-fg"
      />
      <div className="flex items-center gap-2">
        <label htmlFor={`${id}-mode`} className="sr-only">
          Source
        </label>
        <select
          id={`${id}-mode`}
          value={mode}
          onChange={(e) => onMode(e.target.value as AskMode)}
          className="h-8 max-w-[60%] rounded-full border border-border bg-bg px-2 text-xs text-muted-fg"
        >
          {modes.map((m) => (
            <option key={m} value={m}>
              {MODE_LABELS[m]}
            </option>
          ))}
        </select>
        <span className="flex-1" />
        <button
          type="submit"
          aria-label="Send"
          disabled={disabled || !value.trim()}
          className="grid h-9 w-9 place-items-center rounded-full bg-primary text-primary-fg disabled:opacity-40"
        >
          <Icon d={ICONS.send} />
        </button>
      </div>
    </form>
  );
}

function CiteChip({
  n,
  active,
  label,
  onClick,
}: {
  n: number;
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-expanded={active}
      className={`mx-0.5 inline-grid h-[18px] min-w-5 place-items-center rounded-md px-1.5 align-[1px] text-[11px] font-semibold ${
        active ? "bg-primary text-primary-fg" : "bg-primary/15 text-primary hover:bg-primary/25"
      }`}
    >
      {n}
    </button>
  );
}

function citationLabel(c: Citation): string {
  if (c.document_id) return `Open source: ${c.title ?? "document"}`;
  return `Show SQL evidence for ${c.label ?? c.column ?? "figure"}`;
}

function AssistantBody({
  turn,
  focused,
  onCite,
  onCancel,
  canEmail,
  onEmail,
}: {
  turn: Turn;
  focused: string | null;
  onCite: (citationId: string) => void;
  onCancel: () => void;
  canEmail: boolean;
  onEmail: () => void;
}) {
  const { view, failure } = turn;
  const kind = runKind(view, failure);
  const verification = view.done ? verificationText(view.done) : null;
  const cite = (id: string) => view.citations.find((c) => c.citation_id === id) ?? null;
  const chip = (id: string) => {
    const c = cite(id);
    return c ? (
      <CiteChip
        key={id}
        n={citationNumber(view, id)}
        active={focused === id}
        label={citationLabel(c)}
        onClick={() => onCite(id)}
      />
    ) : null;
  };
  const sentences = view.sentences.map((s, i) => (
    <p key={i} className="m-0">
      {s.text} {s.citations.map(chip)}
    </p>
  ));

  return (
    <div className="flex min-w-0 flex-1 flex-col gap-2.5">
      {view.fakeLabel ? (
        <div>
          <StatusPill tone="warn">{view.fakeLabel}</StatusPill>
        </div>
      ) : null}
      {view.steps.length > 0 || turn.running ? (
        <ol className="m-0 flex list-none flex-wrap gap-x-3.5 gap-y-1 p-0 text-[12.5px] text-muted-fg" aria-live="polite">
          {view.steps.map((s) => (
            <li key={s.step} className={s.state === "retrying" ? "text-warn-fg" : ""}>
              <span aria-hidden className={s.state === "retrying" ? "" : "text-ok-fg"}>
                {s.state === "retrying" ? "↻ " : "✓ "}
              </span>
              {s.label}
              {s.state === "retrying" && s.attempt
                ? ` (repair ${s.attempt - 1} of ${(s.maxAttempts ?? 3) - 1})`
                : ""}
            </li>
          ))}
          {turn.running && !view.done ? (
            <li className="flex items-center gap-1.5">
              <span
                aria-hidden
                className="inline-block h-2 w-2 animate-spin rounded-full border-2 border-primary border-r-transparent"
              />
              {view.reconnecting ? "Reconnecting…" : "Working…"}
              {view.runId ? (
                <button type="button" onClick={onCancel} className="ml-2 underline hover:text-fg">
                  Cancel
                </button>
              ) : null}
            </li>
          ) : null}
        </ol>
      ) : null}

      {kind === "error" ? (
        <div role="alert" className="rounded-md bg-error-bg px-3 py-2 text-sm text-error-fg">
          {failure ?? view.error?.message ?? "The request did not complete."}
          {view.error?.code ? <span className="ml-1 opacity-75">({view.error.code})</span> : null}
        </div>
      ) : null}

      {kind === "refusal" && view.done?.abstention ? (
        <p className="m-0 rounded-md bg-muted px-3 py-2 text-sm">{view.done.abstention.message}</p>
      ) : null}

      {kind === "empty" ? (
        <p className="m-0 text-sm text-muted-fg">The run ended without an answer ({view.done?.outcome}).</p>
      ) : null}

      {kind === "report" && view.table ? (
        <div className="flex flex-col gap-2.5 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold">{view.title ?? "Verified result"}</span>
            {verification ? (
              <StatusPill tone={verification.tone}>
                {verification.tone === "ok" ? "Verified" : "Figures verified, summary withheld"}
              </StatusPill>
            ) : null}
          </div>
          {verification ? <p className="m-0 text-xs text-muted-fg">{verification.text}</p> : null}
          <dl className="m-0 grid grid-cols-1 overflow-hidden rounded-xl border border-border sm:grid-cols-2">
            {view.table.rows[0]?.map((cell) => {
              const column = view.table?.columns.find((c) => c.key === cell.column);
              return (
                <div key={cell.column} className="border-b border-border px-3 py-2 last:border-b-0 sm:odd:border-r">
                  <dt className="text-xs text-muted-fg">{column?.label ?? cell.column}</dt>
                  <dd className="m-0 font-medium tabular-nums">
                    {cell.display} {chip(cell.citation_id)}
                  </dd>
                </div>
              );
            })}
          </dl>
          {sentences}
          {view.definition ? (
            <p className="m-0 text-xs text-muted-fg">
              Definition: order date basis; included statuses{" "}
              {(view.definition.included_statuses as string[]).join(", ")} (excluded{" "}
              {(view.definition.excluded_statuses as string[]).join(", ") || "none"});{" "}
              {String(view.definition.tax_treatment)}; currency {String(view.definition.currency)}.
            </p>
          ) : null}
          {canEmail ? (
            <div>
              <button
                type="button"
                onClick={onEmail}
                className="rounded-full border border-border px-3 py-1 text-xs hover:bg-muted"
              >
                Email this report for approval (opens the dashboard)
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      {kind === "answer" ? (
        <div className="flex flex-col gap-2 text-sm">
          {verification ? (
            <div className="flex flex-col gap-1">
              <div>
                <StatusPill tone={verification.tone}>Verified against sources</StatusPill>
              </div>
              <p className="m-0 text-xs text-muted-fg">{verification.text}</p>
            </div>
          ) : null}
          {sentences}
        </div>
      ) : null}
    </div>
  );
}

function SourceCard({
  token,
  runId,
  citation,
  n,
  open,
  onToggle,
}: {
  token: string;
  runId: string | null;
  citation: Citation;
  n: number;
  open: boolean;
  onToggle: () => void;
}) {
  const kind = citationKind(citation);
  const name =
    kind === "document"
      ? `${citation.repository ? `${citation.repository}/` : ""}${citation.title ?? "Document"}`
      : (citation.label ?? citation.column ?? "Query result");
  const meta =
    kind === "document"
      ? [
          `version ${citation.version}`,
          citation.page ? `page ${citation.page}` : null,
          citation.line_start ? `lines ${citation.line_start}–${citation.line_end}` : null,
          citation.heading_path?.length && !citation.repository ? citation.heading_path.join(" › ") : null,
        ]
          .filter(Boolean)
          .join(" · ")
      : `SQL evidence · row ${(citation.row ?? 0) + 1} · column ${citation.column}`;
  return (
    <div
      className={`flex flex-col gap-1.5 rounded-xl border bg-bg px-3 py-2.5 text-sm ${
        open ? "border-primary ring-3 ring-primary/15" : "border-border"
      }`}
    >
      <button type="button" onClick={onToggle} aria-expanded={open} className="flex min-w-0 items-center gap-2 text-left">
        <span className="flex-none rounded bg-primary/15 px-1.5 text-[11px] font-bold text-primary">{n}</span>
        <span className="min-w-0 truncate font-semibold">{name}</span>
      </button>
      <div className="text-[11.5px] text-muted-fg">{meta}</div>
      {open ? (
        <div className="flex flex-col gap-2 text-[13px]">
          {kind === "document" && runId ? (
            <PassageDetail token={token} runId={runId} citation={citation} />
          ) : kind === "evidence" ? (
            <EvidenceDetail token={token} citation={citation} compact />
          ) : (
            <p className="text-muted-fg">This source cannot be opened yet.</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

export function ChatPage({
  token,
  identity,
  packs,
  header,
  canEmail,
  onReportPublished,
  onOpenDashboard,
  onSignOut,
}: {
  token: string;
  identity: Identity;
  packs: string[] | null;
  header: ReactNode;
  canEmail: boolean;
  onReportPublished: (runId: string, title: string) => void;
  onOpenDashboard: () => void;
  onSignOut: () => void;
}) {
  const [state, dispatch] = useReducer(chatReducer, INITIAL_CHAT);
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<AskMode>("auto");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [focus, setFocus] = useState<{ turnId: string; citationId: string | null } | null>(null);
  const controllers = useRef(new Map<string, AbortController>());
  const threadEnd = useRef<HTMLDivElement | null>(null);

  // Signing out unmounts the page: abort open streams; the history goes with the state.
  useEffect(() => {
    const live = controllers.current;
    return () => live.forEach((c) => c.abort());
  }, []);

  const has = (pack: string) => packs === null || packs.includes(pack);
  const modes: AskMode[] = ["auto"];
  if (has("documents")) modes.push("documents");
  if (has("sales_reports")) modes.push("report");
  if (packs?.includes("legal")) modes.push("legal");
  if (packs?.includes("code")) modes.push("code");
  const suggestions = SUGGESTIONS.filter((s) => (s.pack === "legal" ? packs?.includes("legal") : has(s.pack)));

  const conversation = activeConversation(state);
  const turns = conversation?.turns ?? [];
  const lastTurn = turns[turns.length - 1] ?? null;
  const panelTurn = turns.find((t) => t.id === focus?.turnId) ?? lastTurn;

  useEffect(() => {
    threadEnd.current?.scrollIntoView({ block: "end" });
  }, [turns.length, lastTurn?.view.done, lastTurn?.view.steps.length]);

  const ask = useCallback(
    (question: string, askMode: AskMode) => {
      const text = question.trim();
      if (!text) return;
      const conversationId = state.activeId ?? `c-${newRequestId()}`;
      const turnId = `t-${newRequestId()}`;
      dispatch({ type: "ask", conversationId, turnId, question: text, mode: askMode, view: EMPTY_RUN });
      setDraft("");
      setFocus({ turnId, citationId: null });
      const controller = new AbortController();
      controllers.current.set(turnId, controller);
      let last: RunView | null = null;
      void executeRun(token, text, askMode, controller.signal, (view) => {
        last = view;
        dispatch({ type: "view", conversationId, turnId, view });
      }).then((failure) => {
        controllers.current.delete(turnId);
        if (controller.signal.aborted) return;
        dispatch({ type: "finish", conversationId, turnId, failure });
        const final = last as RunView | null;
        if (final?.table && final.done?.outcome === "published" && final.runId) {
          onReportPublished(final.runId, final.title ?? "Verified report");
        }
      });
    },
    [state.activeId, token, onReportPublished],
  );

  const cancel = async (turn: Turn) => {
    if (!turn.view.runId) return;
    try {
      await cancelReport(token, turn.view.runId);
    } catch {
      /* the run's own events report the outcome */
    }
  };

  const openCitation = (turnId: string, citationId: string) => {
    setFocus({ turnId, citationId });
    setPanelOpen(true);
  };

  const firstName = identity.display_name.split(/\s+/)[0] || identity.display_name;
  const badge = (identity.tenant.name || "A").slice(0, 1).toUpperCase();
  const anyRunning = turns.some((t) => t.running);

  return (
    <div className="flex h-dvh w-full overflow-hidden bg-bg text-fg">
      {/* Sidebar: new chat and history */}
      <aside
        aria-label="Conversations"
        className={`fixed inset-y-0 left-0 z-30 flex w-[min(280px,82vw)] flex-col border-r border-border bg-card transition-transform md:static md:w-[264px] md:translate-x-0 ${
          sidebarOpen ? "translate-x-0 shadow-xl" : "-translate-x-[110%]"
        }`}
      >
        <div className="flex items-center gap-2 px-3 pt-3 pb-1.5">
          <span className="grid h-7 w-7 place-items-center rounded-lg bg-primary text-xs font-bold text-primary-fg">
            {badge}
          </span>
          <span className="min-w-0 flex-1 truncate font-semibold">{identity.tenant.name} Assistant</span>
          <IconButton label="Close menu" icon="close" onClick={() => setSidebarOpen(false)} className="md:hidden" />
        </div>
        <div className="px-2">
          <button
            type="button"
            onClick={() => {
              dispatch({ type: "new" });
              setFocus(null);
              setPanelOpen(false);
              setSidebarOpen(false);
            }}
            className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13.5px] hover:bg-muted"
          >
            <Icon d={ICONS.pen} />
            New chat
          </button>
        </div>
        <div className="px-4 pt-3 pb-1 text-[12.5px] text-muted-fg">This session</div>
        <nav className="flex-1 overflow-y-auto px-2 pb-3" aria-label="Chat history">
          {state.conversations.length === 0 ? (
            <p className="px-2.5 py-1 text-xs text-muted-fg">No chats yet. History is kept until you sign out.</p>
          ) : (
            <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
              {state.conversations.map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    aria-current={c.id === state.activeId ? "page" : undefined}
                    onClick={() => {
                      dispatch({ type: "select", id: c.id });
                      setFocus(null);
                      setSidebarOpen(false);
                    }}
                    className={`block w-full truncate rounded-lg px-2.5 py-1.5 text-left text-[13.5px] hover:bg-muted ${
                      c.id === state.activeId ? "bg-muted font-medium" : ""
                    }`}
                  >
                    {c.title}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </nav>
        <div className="flex flex-col gap-1 border-t border-border p-2 text-[13.5px]">
          <button
            type="button"
            onClick={onOpenDashboard}
            className="flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-left hover:bg-muted"
          >
            <Icon d={ICONS.grid} />
            Dashboard, documents and email
          </button>
          <div className="flex items-center gap-2 px-2.5 py-1">
            <span className="min-w-0 flex-1">
              <span className="block truncate font-medium">{identity.display_name}</span>
              <span className="block truncate text-xs text-muted-fg">{identity.email}</span>
            </span>
            <button
              type="button"
              onClick={onSignOut}
              className="flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-muted-fg hover:bg-muted hover:text-fg"
            >
              <Icon d={ICONS.out} />
              Sign out
            </button>
          </div>
        </div>
      </aside>
      {sidebarOpen ? (
        <div className="fixed inset-0 z-20 bg-fg/25 md:hidden" aria-hidden onClick={() => setSidebarOpen(false)} />
      ) : null}

      {/* Main column */}
      <main className="flex min-w-0 flex-1 flex-col">
        <div className="relative flex min-h-[52px] items-center justify-center gap-2 px-12 pt-2.5 pb-1.5">
          <div className="absolute left-2.5 flex gap-1 md:hidden">
            <IconButton label="Open menu" icon="menu" onClick={() => setSidebarOpen(true)} />
          </div>
          <div className="flex flex-wrap items-center justify-center gap-x-2 gap-y-1 text-[12.5px] text-muted-fg">
            <span>
              Limited to what <b className="text-fg">{identity.display_name}</b> can open
            </span>
            {header}
          </div>
          <div className="absolute right-2.5 flex gap-1">
            <IconButton
              label="Sources panel"
              icon="panel"
              pressed={panelOpen}
              onClick={() => setPanelOpen((o) => !o)}
            />
          </div>
        </div>

        {!conversation ? (
          <section className="flex flex-1 flex-col items-center justify-center gap-5 overflow-y-auto px-4 pb-[10vh]">
            <h1 className="m-0 text-center text-[clamp(21px,2.6vw,28px)] font-medium tracking-tight text-balance">
              What would you like to know, {firstName}?
            </h1>
            <Composer
              id="chat-home"
              placeholder="Ask about policies, contracts, documents or sales…"
              value={draft}
              onChange={setDraft}
              mode={mode}
              onMode={setMode}
              modes={modes}
              disabled={false}
              onSubmit={() => ask(draft, mode)}
            />
            <div className="flex w-full max-w-[680px] flex-col gap-0.5">
              {suggestions.map((s) => (
                <button
                  key={s.question}
                  type="button"
                  onClick={() => {
                    setMode(s.mode);
                    ask(s.question, s.mode);
                  }}
                  className="flex items-center gap-3 rounded-lg px-3 py-2 text-left text-sm text-muted-fg hover:bg-muted hover:text-fg"
                >
                  <span className="grid h-[22px] w-[22px] flex-none place-items-center rounded-md bg-primary/15 text-[11px] font-bold text-primary">
                    ?
                  </span>
                  <span className="min-w-0">
                    {s.question} <span className="text-xs opacity-75">· {MODE_LABELS[s.mode]}</span>
                  </span>
                </button>
              ))}
            </div>
            <p className="m-0 max-w-[680px] text-center text-xs text-muted-fg">{INDEPENDENT_NOTE}</p>
          </section>
        ) : (
          <section className="flex min-h-0 flex-1 flex-col overflow-y-auto px-4" aria-label="Conversation">
            <div className="mx-auto flex w-full max-w-[760px] flex-1 flex-col gap-5 pt-3.5 pb-6">
              <p className="m-0 self-center rounded-full border border-border px-3 py-0.5 text-center text-[11.5px] text-muted-fg">
                {INDEPENDENT_NOTE}
              </p>
              {turns.map((turn) => (
                <div key={turn.id} className="flex flex-col gap-3">
                  <div className="max-w-[80%] self-end rounded-[19px] bg-muted px-4 py-2.5 text-sm break-words">
                    {turn.question}
                    {turn.mode !== "auto" ? (
                      <span className="mt-0.5 block text-[11px] text-muted-fg">{MODE_LABELS[turn.mode]}</span>
                    ) : null}
                  </div>
                  <div className="flex items-start gap-3">
                    <span className="grid h-[26px] w-[26px] flex-none place-items-center rounded-lg bg-primary text-xs font-bold text-primary-fg">
                      {badge}
                    </span>
                    <AssistantBody
                      turn={turn}
                      focused={focus?.turnId === turn.id ? focus.citationId : null}
                      onCite={(id) => openCitation(turn.id, id)}
                      onCancel={() => void cancel(turn)}
                      canEmail={canEmail}
                      onEmail={onOpenDashboard}
                    />
                  </div>
                </div>
              ))}
              <div ref={threadEnd} />
            </div>
            <div className="sticky bottom-0 flex justify-center bg-gradient-to-t from-bg from-70% to-transparent pt-1.5 pb-3.5">
              <Composer
                id="chat-dock"
                placeholder="Ask another question…"
                value={draft}
                onChange={setDraft}
                mode={mode}
                onMode={setMode}
                modes={modes}
                disabled={anyRunning}
                onSubmit={() => ask(draft, mode)}
              />
            </div>
          </section>
        )}
      </main>

      {/* Sources panel */}
      {panelOpen ? (
        <>
          <div className="fixed inset-0 z-30 bg-fg/25 lg:hidden" aria-hidden onClick={() => setPanelOpen(false)} />
          <aside
            aria-label="Sources"
            className="fixed inset-y-0 right-0 z-40 flex w-[min(380px,94vw)] flex-col border-l border-border bg-card shadow-xl lg:static lg:z-auto lg:w-[360px] lg:shadow-none"
          >
            <div className="flex items-center gap-2 border-b border-border px-3 py-2.5">
              <span className="font-semibold">Sources</span>
              <span className="text-xs text-muted-fg">{panelTurn?.view.citations.length ?? 0}</span>
              <span className="flex-1" />
              <IconButton label="Close sources panel" icon="close" onClick={() => setPanelOpen(false)} />
            </div>
            <div className="flex flex-1 flex-col gap-2.5 overflow-y-auto p-3">
              {panelTurn ? (
                <p className="m-0 text-xs text-muted-fg">
                  For: <span className="text-fg">{panelTurn.question}</span>. Opening a source asks the server
                  again, so your current access is checked.
                </p>
              ) : null}
              {!panelTurn || panelTurn.view.citations.length === 0 ? (
                <p className="m-0 text-sm text-muted-fg">
                  {panelTurn?.running ? "Waiting for the answer…" : "No sources for this answer."}
                </p>
              ) : (
                panelTurn.view.citations.map((c) => (
                  <SourceCard
                    key={c.citation_id}
                    token={token}
                    runId={panelTurn.view.runId}
                    citation={c}
                    n={citationNumber(panelTurn.view, c.citation_id)}
                    open={focus?.turnId === panelTurn.id && focus.citationId === c.citation_id}
                    onToggle={() =>
                      setFocus((f) =>
                        f?.turnId === panelTurn.id && f.citationId === c.citation_id
                          ? { turnId: panelTurn.id, citationId: null }
                          : { turnId: panelTurn.id, citationId: c.citation_id },
                      )
                    }
                  />
                ))
              )}
            </div>
          </aside>
        </>
      ) : null}
    </div>
  );
}
