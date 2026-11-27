import { Alert, Button, Card, StatusPill } from "@eaios/ui";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { verificationText } from "./verification";
import { cancelReport, type AskMode, type Citation } from "./reports";
import { executeRun } from "./runStream";
import { citationNumber as numberOf, EMPTY_RUN, type RunView } from "./runView";
import { EvidenceDetail, PassageDetail } from "./sourceViews";

const EXAMPLE = "What were net (tax-exclusive) fulfilled sales for Q1 2026, in USD?";

function EvidencePanel({
  token,
  citation,
  onClose,
}: {
  token: string;
  citation: Citation;
  onClose: () => void;
}) {
  return (
    <Card title="Evidence">
      <div className="flex flex-col gap-3 text-sm">
        <EvidenceDetail token={token} citation={citation} />
        <div className="flex justify-end">
          <Button variant="secondary" onClick={onClose}>
            Close evidence
          </Button>
        </div>
      </div>
    </Card>
  );
}

function DocumentCitationPanel({
  token,
  runId,
  citation,
  onClose,
}: {
  token: string;
  runId: string;
  citation: Citation;
  onClose: () => void;
}) {
  return (
    <Card title="Source">
      <div className="flex flex-col gap-3 text-sm">
        <PassageDetail token={token} runId={runId} citation={citation} />
        <div className="flex justify-end">
          <Button variant="secondary" onClick={onClose}>
            Close source
          </Button>
        </div>
      </div>
    </Card>
  );
}

export function ReportPanel({
  token,
  packs,
  onReportPublished,
}: {
  token: string;
  packs: string[] | null;
  /** M6: a verified sales report was published (it can be emailed after approval). */
  onReportPublished?: (runId: string, title: string) => void;
}) {
  // Menus follow the tenant's packs; the backend refuses disabled packs regardless.
  const docsOn = packs === null || packs.includes("documents");
  const legalOn = packs !== null && packs.includes("legal");
  const codeOn = packs !== null && packs.includes("code");
  const reportsOn = packs === null || packs.includes("sales_reports");
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<AskMode>("auto");
  const [view, setView] = useState<RunView>(EMPTY_RUN);
  const [running, setRunning] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [selected, setSelected] = useState<Citation | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const viewRef = useRef<RunView>(EMPTY_RUN);

  const onView = useCallback((next: RunView) => {
    viewRef.current = next;
    setView(next);
  }, []);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (view.table && view.done?.outcome === "published" && view.runId) {
      onReportPublished?.(view.runId, view.title ?? "Verified report");
    }
  }, [view.table, view.done, view.runId, view.title, onReportPublished]);

  const run = async (event: FormEvent) => {
    event.preventDefault();
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    onView(EMPTY_RUN);
    setSelected(null);
    setFailure(null);
    setRunning(true);
    try {
      const message = await executeRun(token, question.trim(), mode, controller.signal, onView);
      if (message && !controller.signal.aborted) setFailure(message);
    } finally {
      setRunning(false);
    }
  };

  const cancel = async () => {
    const id = viewRef.current.runId;
    if (!id) return;
    try {
      await cancelReport(token, id);
    } catch {
      setFailure("Could not cancel the request.");
    }
  };

  const cite = (id: string) => view.citations.find((c) => c.citation_id === id) ?? null;
  const citationNumber = (id: string) => numberOf(view, id);
  const verification = view.done ? verificationText(view.done) : null;
  const fake = view.fakeLabel;

  return (
    <div className="flex flex-col gap-6">
      <Card title="Ask">
        <form className="flex flex-col gap-3" onSubmit={(e) => void run(e)}>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <label htmlFor="ask-mode" className="font-medium">
              Source
            </label>
            <select
              id="ask-mode"
              className="h-9 rounded-md border border-border bg-bg px-2"
              value={mode}
              onChange={(e) => setMode(e.target.value as AskMode)}
            >
              <option value="auto">Automatic</option>
              {docsOn ? <option value="documents">Documents you can access</option> : null}
              {reportsOn ? <option value="report">Sales report (SQL)</option> : null}
              {legalOn ? <option value="legal">Contracts (clauses and expiry)</option> : null}
              {codeOn ? <option value="code">Code repositories</option> : null}
            </select>
          </div>
          <label htmlFor="question" className="text-sm font-medium">
            Ask about your documents, contracts, code or fulfilled sales
          </label>
          <textarea
            id="question"
            rows={2}
            maxLength={1000}
            className="rounded-md border border-border bg-bg px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-ring"
            placeholder={EXAMPLE}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
          />
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={running || !question.trim()}>
              {running ? "Working…" : "Run report"}
            </Button>
            {running ? (
              <Button type="button" variant="secondary" onClick={() => void cancel()}>
                Cancel
              </Button>
            ) : (
              <Button type="button" variant="secondary" onClick={() => setQuestion(EXAMPLE)}>
                Use example
              </Button>
            )}
          </div>
        </form>
        {fake ? (
          <div className="mt-4">
            <StatusPill tone="warn">{fake}</StatusPill>
          </div>
        ) : null}
        {view.steps.length > 0 ? (
          <ol className="mt-4 flex flex-col gap-1 text-sm" aria-live="polite">
            {view.steps.map((s) => (
              <li key={s.step} className="flex items-center gap-2">
                <span aria-hidden>{s.state === "retrying" ? "↻" : "✓"}</span>
                <span>
                  {s.label}
                  {s.state === "retrying" && s.attempt
                    ? ` (repair ${s.attempt - 1} of ${(s.maxAttempts ?? 3) - 1})`
                    : ""}
                </span>
              </li>
            ))}
            {view.reconnecting ? <li className="text-muted-fg">Reconnecting…</li> : null}
          </ol>
        ) : null}
        {failure ? (
          <div className="mt-4">
            <Alert>{failure}</Alert>
          </div>
        ) : null}
        {view.error ? (
          <div className="mt-4">
            <Alert>{view.error.message}</Alert>
          </div>
        ) : null}
        {view.done?.abstention ? (
          <p className="mt-4 rounded-md bg-muted px-3 py-2 text-sm">{view.done.abstention.message}</p>
        ) : null}
      </Card>

      {view.table && view.done?.outcome === "published" ? (
        <Card title={view.title ?? "Verified result"}>
          {verification ? (
            <div className="mb-4 flex flex-col gap-1">
              <StatusPill tone={verification.tone}>
                {verification.tone === "ok" ? "Verified" : "Figures verified, summary withheld"}
              </StatusPill>
              <p className="text-xs text-muted-fg">{verification.text}</p>
            </div>
          ) : null}
          <dl className="divide-y divide-border">
            {view.table.rows[0]?.map((cell) => {
              const column = view.table?.columns.find((c) => c.key === cell.column);
              const citation = cite(cell.citation_id);
              return (
                <div key={cell.column} className="grid grid-cols-1 gap-1 py-2 sm:grid-cols-3">
                  <dt className="text-sm text-muted-fg">{column?.label ?? cell.column}</dt>
                  <dd className="text-sm font-medium tabular-nums sm:col-span-2">
                    {cell.display}{" "}
                    {citation ? (
                      <button
                        type="button"
                        className="ml-1 rounded px-1 text-xs text-primary underline"
                        onClick={() => setSelected(citation)}
                        aria-label={`Show evidence for ${column?.label ?? cell.column}`}
                      >
                        [{citationNumber(citation.citation_id)}]
                      </button>
                    ) : null}
                  </dd>
                </div>
              );
            })}
          </dl>
          {view.sentences.length > 0 ? (
            <div className="mt-4 flex flex-col gap-2 text-sm">
              {view.sentences.map((s, i) => (
                <p key={i}>
                  {s.text}{" "}
                  {s.citations.map((id) => {
                    const c = cite(id);
                    return c ? (
                      <button
                        key={id}
                        type="button"
                        className="rounded px-0.5 text-xs text-primary underline"
                        onClick={() => setSelected(c)}
                      >
                        [{citationNumber(id)}]
                      </button>
                    ) : null;
                  })}
                </p>
              ))}
            </div>
          ) : null}
          {view.definition ? (
            <p className="mt-4 text-xs text-muted-fg">
              Definition: order date basis; included statuses{" "}
              {(view.definition.included_statuses as string[]).join(", ")} (excluded{" "}
              {(view.definition.excluded_statuses as string[]).join(", ") || "none"});{" "}
              {String(view.definition.tax_treatment)}; currency {String(view.definition.currency)}.
            </p>
          ) : null}
        </Card>
      ) : null}

      {!view.table && view.done?.outcome === "published" && view.sentences.length > 0 ? (
        <Card title="Answer">
          {verification ? (
            <div className="mb-4 flex flex-col gap-1">
              <StatusPill tone={verification.tone}>Verified against sources</StatusPill>
              <p className="text-xs text-muted-fg">{verification.text}</p>
            </div>
          ) : null}
          <div className="flex flex-col gap-2 text-sm">
            {view.sentences.map((s, i) => (
              <p key={i}>
                {s.text}{" "}
                {s.citations.map((id) => {
                  const c = cite(id);
                  return c ? (
                    <button
                      key={id}
                      type="button"
                      className="rounded px-0.5 text-xs text-primary underline"
                      onClick={() => setSelected(c)}
                      aria-label={`Open source: ${c.title ?? "document"}`}
                    >
                      [{citationNumber(id)}]
                    </button>
                  ) : null;
                })}
              </p>
            ))}
          </div>
          <ul className="mt-4 flex flex-col gap-1 text-xs text-muted-fg">
            {view.citations.map((c) => (
              <li key={c.citation_id}>
                [{citationNumber(c.citation_id)}] {c.repository ? `${c.repository}/` : ""}
                {c.title} (version {c.version}
                {c.commit ? `, commit ${c.commit.slice(0, 12)}` : ""}
                {c.line_start ? `, lines ${c.line_start}–${c.line_end}` : ""}
                {c.page ? `, page ${c.page}` : ""}
                {c.heading_path?.length && !c.repository ? `, ${c.heading_path.join(" › ")}` : ""})
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      {selected && selected.document_id && view.runId ? (
        <DocumentCitationPanel
          token={token}
          runId={view.runId}
          citation={selected}
          onClose={() => setSelected(null)}
        />
      ) : selected ? (
        <EvidencePanel token={token} citation={selected} onClose={() => setSelected(null)} />
      ) : null}
    </div>
  );
}
