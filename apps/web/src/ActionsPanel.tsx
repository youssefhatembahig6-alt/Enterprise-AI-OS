import { Alert, Button, Card, Field, StatusPill, type StatusTone } from "@eaios/ui";
import { useCallback, useEffect, useState } from "react";
import { actions, STATUS_TEXT, type Approval, type Draft, type Recipient, type Schedule } from "./actions";
import { ApiError } from "./api";

export interface ReportSource {
  runId: string;
  title: string;
}

function tone(status: string): StatusTone {
  if (status === "delivered") return "ok";
  if (["blocked", "failed", "cancelled"].includes(status)) return "error";
  if (["uncertain", "approved", "queued", "sending"].includes(status)) return "warn";
  return "unknown";
}

function message(err: unknown): string {
  if (err instanceof ApiError) return err.status === 401 ? "Your session has ended. Sign in again." : err.message;
  return "Could not reach the API.";
}

function RecipientSelect({
  id,
  recipients,
  value,
  onChange,
}: {
  id: string;
  recipients: Recipient[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        Recipient (people in your organization only)
      </label>
      <select
        id={id}
        className="h-10 rounded-md border border-border bg-transparent px-3 text-sm"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">Choose a recipient…</option>
        {recipients.map((r) => (
          <option key={r.user_id} value={r.user_id}>
            {r.display_name} ({r.email})
          </option>
        ))}
      </select>
    </div>
  );
}

function DraftEditor({
  token,
  draft,
  recipients,
  onChanged,
}: {
  token: string;
  draft: Draft;
  recipients: Recipient[];
  onChanged: (d: Draft) => void;
}) {
  const [subject, setSubject] = useState(draft.subject ?? "");
  const [body, setBody] = useState(draft.body ?? "");
  const [to, setTo] = useState(draft.recipient.user_id);
  const [approval, setApproval] = useState<Approval | null>(null); // memory only
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [expiredNotice, setExpiredNotice] = useState(false);

  useEffect(() => {
    setSubject(draft.subject ?? "");
    setBody(draft.body ?? "");
    setTo(draft.recipient.user_id);
  }, [draft]);

  // An approval is only usable for the exact version it names and until it expires.
  // When it lapses, drop it so "Approve this version" is offered again.
  useEffect(() => {
    if (!approval) return;
    if (approval.version !== draft.version || approval.draft_sha256 !== draft.draft_sha256) {
      setApproval(null);
      return;
    }
    const left = new Date(approval.expires_at).getTime() - Date.now();
    const timer = setTimeout(() => {
      setApproval(null);
      setExpiredNotice(true);
    }, Math.max(0, left));
    return () => clearTimeout(timer);
  }, [approval, draft.version, draft.draft_sha256]);

  const editable = ["draft", "approved"].includes(draft.status) && !draft.content_withheld;
  const dirty = subject !== (draft.subject ?? "") || body !== (draft.body ?? "") || to !== draft.recipient.user_id;

  const act = async (fn: () => Promise<Draft | null>) => {
    setBusy(true);
    setError(null);
    try {
      const next = await fn();
      if (next) onChanged(next);
    } catch (err) {
      setError(message(err));
      if (err instanceof ApiError && err.code === "approval_rejected") setApproval(null);
      try {
        onChanged(await actions.draft(token, draft.action_id));
      } catch {
        /* keep the current view */
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill tone={tone(draft.status)}>{draft.status}</StatusPill>
        <span className="text-xs text-muted-fg">
          Version {draft.version}
          {draft.edited ? " (edited)" : ""} · {draft.origin === "briefing" ? "scheduled briefing" : "from a report"}
        </span>
      </div>
      <p className="text-sm">{STATUS_TEXT[draft.status] ?? draft.status}</p>
      {draft.status_reason ? <p className="text-xs text-muted-fg">Reason: {draft.status_reason}</p> : null}
      {draft.content_withheld ? (
        <Alert>You no longer have access to the source report, so this draft's content is hidden.</Alert>
      ) : (
        <>
          <RecipientSelect id={`to-${draft.action_id}`} recipients={recipients} value={to} onChange={setTo} />
          <Field
            id={`subject-${draft.action_id}`}
            label="Subject"
            value={subject}
            maxLength={200}
            disabled={!editable}
            onChange={(e) => setSubject(e.target.value.replace(/[\r\n]/g, " "))}
          />
          <div className="flex flex-col gap-1.5">
            <label htmlFor={`body-${draft.action_id}`} className="text-sm font-medium">
              Message
            </label>
            <textarea
              id={`body-${draft.action_id}`}
              className="min-h-56 rounded-md border border-border bg-transparent p-3 font-mono text-xs"
              value={body}
              disabled={!editable}
              onChange={(e) => setBody(e.target.value)}
            />
          </div>
        </>
      )}
      {draft.delivery ? (
        <p className="text-xs text-muted-fg break-all">
          Delivery {draft.delivery.status} · attempts {draft.delivery.attempts} · Message-ID {draft.delivery.message_id}
        </p>
      ) : null}
      {error ? <Alert>{error}</Alert> : null}
      <div className="flex flex-wrap gap-2">
        {editable && dirty ? (
          <Button disabled={busy || !to || !subject || !body} onClick={() => void act(() => actions.edit(token, draft, to, subject, body))}>
            Save as new version
          </Button>
        ) : null}
        {editable && !dirty && !approval ? (
          <Button
            disabled={busy}
            onClick={() =>
              void act(async () => {
                const a = await actions.approve(token, draft);
                setExpiredNotice(false);
                setApproval(a);
                return actions.draft(token, draft.action_id);
              })
            }
          >
            Approve this version
          </Button>
        ) : null}
        {editable && !dirty && approval ? (
          <Button
            disabled={busy}
            onClick={() =>
              void act(async () => {
                const sent = await actions.send(token, draft, approval);
                setApproval(null); // consumed: one send per approval
                return sent;
              })
            }
          >
            Send now
          </Button>
        ) : null}
        {["draft", "approved", "queued"].includes(draft.status) ? (
          <Button variant="secondary" disabled={busy} onClick={() => void act(() => actions.cancel(token, draft))}>
            Cancel
          </Button>
        ) : null}
      </div>
      {expiredNotice && !approval ? (
        <p className="text-xs text-muted-fg" role="status">
          Your approval expired before sending. Review the draft and approve this version again.
        </p>
      ) : null}
      {approval && editable ? (
        <p className="text-xs text-muted-fg">
          You approved version {approval.version}. The approval expires at{" "}
          {new Date(approval.expires_at).toLocaleTimeString()} and any edit cancels it.
        </p>
      ) : null}
    </div>
  );
}

export function ActionsPanel({ token, source }: { token: string; source: ReportSource | null }) {
  const [recipients, setRecipients] = useState<Recipient[]>([]);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [to, setTo] = useState("");
  const [question, setQuestion] = useState("");
  const [briefTo, setBriefTo] = useState("");
  const [interval, setInterval_] = useState(86400);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [r, d, s] = await Promise.all([actions.recipients(token), actions.drafts(token), actions.schedules(token)]);
      setRecipients(r);
      setDrafts(d);
      setSchedules(s);
    } catch (err) {
      setError(message(err));
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Delivery runs in the background; refresh while anything is in flight.
  useEffect(() => {
    if (!drafts.some((d) => ["queued", "sending", "uncertain"].includes(d.status))) return;
    const timer = setTimeout(() => void refresh(), 2000);
    return () => clearTimeout(timer);
  }, [drafts, refresh]);

  const replace = (d: Draft) => setDrafts((all) => all.map((x) => (x.action_id === d.action_id ? d : x)));

  const createDraft = async () => {
    if (!source || !to) return;
    setBusy(true);
    setError(null);
    try {
      const d = await actions.create(token, source.runId, to);
      setDrafts((all) => [d, ...all]);
      setOpen(d.action_id);
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };

  const createSchedule = async () => {
    setBusy(true);
    setError(null);
    try {
      await actions.schedule(token, question.trim(), briefTo, interval, new Date(Date.now() + 60_000));
      setQuestion("");
      await refresh();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Email and briefings">
      <p className="mb-4 text-sm text-muted-fg">
        Emails are drafted from verified reports and are sent only after you approve the exact version. Development
        delivery goes to the local Mailpit inbox.
      </p>
      {error ? <Alert>{error}</Alert> : null}
      {source ? (
        <div className="mb-6 flex flex-col gap-3">
          <p className="text-sm font-medium">Email “{source.title}”</p>
          <RecipientSelect id="new-draft-to" recipients={recipients} value={to} onChange={setTo} />
          <div>
            <Button disabled={busy || !to} onClick={() => void createDraft()}>
              Create draft for review
            </Button>
          </div>
        </div>
      ) : (
        <p className="mb-6 text-sm text-muted-fg">Run a verified sales report to draft an email from it.</p>
      )}

      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold">Your drafts</h3>
        <Button variant="secondary" onClick={() => void refresh()}>
          Refresh
        </Button>
      </div>
      {drafts.length === 0 ? <p className="text-sm text-muted-fg">No drafts yet.</p> : null}
      <ul className="flex flex-col gap-3">
        {drafts.map((d) => (
          <li key={d.action_id} className="flex flex-col gap-2">
            <button
              className="flex flex-wrap items-center gap-2 text-left text-sm"
              onClick={() => setOpen(open === d.action_id ? null : d.action_id)}
              aria-expanded={open === d.action_id}
            >
              <StatusPill tone={tone(d.status)}>{d.status}</StatusPill>
              <span className="font-medium">{d.subject ?? "(content hidden)"}</span>
              <span className="text-muted-fg">→ {d.recipient.display_name}</span>
            </button>
            {open === d.action_id ? (
              <DraftEditor token={token} draft={d} recipients={recipients} onChanged={replace} />
            ) : null}
          </li>
        ))}
      </ul>

      <h3 className="mt-8 mb-2 text-sm font-semibold">Scheduled briefings</h3>
      <p className="mb-3 text-xs text-muted-fg">
        Each run uses your permissions at that moment and produces a draft for review. Nothing is sent automatically.
      </p>
      <div className="flex flex-col gap-3">
        <Field
          id="briefing-question"
          label="Report question"
          placeholder="Net fulfilled sales for Q1 2026"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <RecipientSelect id="briefing-to" recipients={recipients} value={briefTo} onChange={setBriefTo} />
        <div className="flex flex-col gap-1.5">
          <label htmlFor="briefing-interval" className="text-sm font-medium">
            Every
          </label>
          <select
            id="briefing-interval"
            className="h-10 rounded-md border border-border bg-transparent px-3 text-sm"
            value={interval}
            onChange={(e) => setInterval_(Number(e.target.value))}
          >
            <option value={86400}>day</option>
            <option value={604800}>week</option>
          </select>
        </div>
        <div>
          <Button disabled={busy || !question.trim() || !briefTo} onClick={() => void createSchedule()}>
            Schedule briefing
          </Button>
        </div>
      </div>
      <ul className="mt-4 flex flex-col gap-2">
        {schedules.map((s) => (
          <li key={s.schedule_id} className="rounded-md border border-border p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill tone={s.active ? "ok" : "unknown"}>{s.active ? "active" : "stopped"}</StatusPill>
              <span className="font-medium">{s.question}</span>
            </div>
            <p className="text-xs text-muted-fg">
              To {s.recipient.display_name} · next {new Date(s.next_run_at).toLocaleString()}
            </p>
            {s.occurrences.map((o) => (
              <p key={o.occurrence_id} className="text-xs">
                {new Date(o.due_at).toLocaleString()}: {o.status}
                {o.status_reason ? ` (${o.status_reason})` : ""}
              </p>
            ))}
            {s.active ? (
              <Button
                variant="secondary"
                className="mt-2"
                onClick={() => void actions.deactivate(token, s.schedule_id).then(refresh, (e: unknown) => setError(message(e)))}
              >
                Stop
              </Button>
            ) : null}
          </li>
        ))}
      </ul>
    </Card>
  );
}
