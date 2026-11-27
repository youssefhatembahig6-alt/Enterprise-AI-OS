/**
 * The signed-in app in the NileTech Assistant shell (ui-demo/eaios-ui-demo.html):
 * icon rail | chat sidebar | main canvas | Sources panel. The Ask view is the chat;
 * the Files, Approvals, Activity and Settings views host the existing panels, passed in
 * by App and kept mounted so their state survives navigation.
 *
 * Every question is one POST /api/v1/chat run with the signed-in user's token; the server
 * checks access, verifies the answer and re-checks access whenever a citation is opened.
 * Chat history lives in memory only and goes away with this component on sign-out.
 */

import { useCallback, useEffect, useReducer, useRef, useState, type FormEvent, type ReactNode } from "react";
import type { Identity } from "./api";
import { activeConversation, chatReducer, INITIAL_CHAT, type Turn } from "./chatState";
import { cancelReport, type AskMode, type Citation } from "./reports";
import { executeChatTurn } from "./runStream";
import { citationKind, citationNumber, EMPTY_RUN, newRequestId, runKind, type RunView } from "./runView";
import { EvidenceDetail, PassageDetail } from "./sourceViews";
import { LEGAL_LABEL, verificationText } from "./verification";

export type ShellView = "chat" | "files" | "approvals" | "activity" | "settings";

const EMPTY_TURNS: Turn[] = [];

const MODE_LABELS: Record<AskMode, string> = {
  auto: "Automatic",
  documents: "Documents you can access",
  report: "Sales report (SQL)",
  legal: "Contracts (clauses and expiry)",
  code: "Code repositories",
};

const INDEPENDENT_NOTE =
  "Each question is answered on its own: earlier messages in this chat are not sent to the assistant.";

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

function verifyPills(view: RunView): { text: string; warn: boolean }[] {
  const pills: { text: string; warn: boolean }[] = [];
  const v = view.done?.verification;
  if (v) {
    if (v.label === LEGAL_LABEL) {
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
  if (view.done?.abstention) pills.push({ text: "No sources used", warn: true });
  if (view.fakeLabel) pills.push({ text: "FAKE inference — not a model", warn: true });
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
  mode,
  modes,
  onMode,
}: {
  id: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  onSubmit: () => void;
  onUpload: () => void;
  mode: AskMode;
  modes: AskMode[];
  onMode: (m: AskMode) => void;
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
        <label className="srcsel" title="Where the answer comes from">
          <select id={`${id}-source`} aria-label="Source" value={mode} onChange={(e) => onMode(e.target.value as AskMode)}>
            {modes.map((m) => (
              <option key={m} value={m}>
                {MODE_LABELS[m]}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="send" aria-label="Ask" disabled={disabled || !value.trim()}>
          <Svg k="up" />
        </button>
      </div>
    </form>
  );
}

function Answer({
  turn,
  badge,
  focused,
  onCite,
  onCancel,
  canEmail,
  onEmail,
}: {
  turn: Turn;
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

  return (
    <div className="a">
      <span className="badge">{badge}</span>
      <div className="body">
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
            {view.error?.code ? ` (${view.error.code})` : ""}
          </div>
        ) : null}
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
            {verifyPills(view).map((p) => (
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
  packs,
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
  packs: string[] | null;
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
  const [mode, setMode] = useState<AskMode>("auto");
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
  const threadEnd = useRef<HTMLDivElement | null>(null);

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
    (question: string, askMode: AskMode) => {
      const text = question.trim();
      if (!text) return;
      const conversationId = state.activeId ?? `c-${newRequestId()}`;
      const turnId = `t-${newRequestId()}`;
      dispatch({ type: "ask", conversationId, turnId, question: text, mode: askMode, view: EMPTY_RUN });
      setDraft("");
      const controller = new AbortController();
      controllers.current.set(turnId, controller);
      void executeChatTurn(
        token,
        text,
        askMode,
        controller.signal,
        (next) => dispatch({ type: "view", conversationId, turnId, view: next }),
        onReportPublished,
      ).then((failure) => {
        controllers.current.delete(turnId);
        if (controller.signal.aborted) return;
        dispatch({ type: "finish", conversationId, turnId, failure });
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
          {recents.length === 0 ? (
            <p className="side-empty">
              {state.conversations.length === 0
                ? "Your chats appear here until you sign out."
                : "No chats match."}
            </p>
          ) : (
            recents.map((c) => (
              <button
                key={c.id}
                type="button"
                className="row"
                aria-current={view === "chat" && c.id === state.activeId ? "page" : undefined}
                onClick={() => {
                  dispatch({ type: "select", id: c.id });
                  setFocus(null);
                  go("chat");
                }}
              >
                <span className="t">{c.title}</span>
              </button>
            ))
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
              onSubmit={() => ask(draft, mode)}
              onUpload={() => go("files")}
              mode={mode}
              modes={modes}
              onMode={setMode}
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
                    {turn.mode !== "auto" ? <small>{MODE_LABELS[turn.mode]}</small> : null}
                  </div>
                  <Answer
                    turn={turn}
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
                onSubmit={() => ask(draft, mode)}
                onUpload={() => go("files")}
                mode={mode}
                modes={modes}
                onMode={setMode}
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
