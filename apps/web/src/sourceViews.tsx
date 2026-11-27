/**
 * Citation details, fetched on every open so the server re-checks current access:
 * SQL evidence (GET /api/v1/evidence/{id}) and document or code passages
 * (GET /api/v1/runs/{run}/citations/{id}). Used by the dashboard and the Chat page.
 */

import { Alert, StatusPill } from "@eaios/ui";
import { useEffect, useState } from "react";
import { ApiError } from "./api";
import {
  fetchDocumentCitation,
  fetchEvidence,
  type Citation,
  type DocumentCitation,
  type Evidence,
} from "./reports";

function useFetched<T>(load: () => Promise<T>, deps: unknown[], notFound: string, failed: string) {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setValue(null);
    setError(null);
    load()
      .then((v) => live && setValue(v))
      .catch((err: unknown) => {
        if (!live) return;
        setError(err instanceof ApiError && err.status === 404 ? notFound : failed);
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deps are passed by the caller
  }, deps);
  return [value, error] as const;
}

export function EvidenceDetail({
  token,
  citation,
  compact = false,
}: {
  token: string;
  citation: Citation;
  compact?: boolean;
}) {
  const [evidence, error] = useFetched<Evidence>(
    () => fetchEvidence(token, citation.evidence_id ?? ""),
    [token, citation.evidence_id],
    "This evidence is not available to you (your access may have changed).",
    "Could not load the evidence.",
  );
  return (
    <>
      <p className="text-muted-fg">
        Exact stored query result for <strong>{citation.label}</strong> (row {(citation.row ?? 0) + 1},
        column <code>{citation.column}</code>). Access is re-checked every time it is opened.
      </p>
      {error ? <Alert>{error}</Alert> : null}
      {!evidence && !error ? <p className="text-muted-fg">Loading…</p> : null}
      {evidence ? (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr>
                  {evidence.columns.map((c) => (
                    <th key={c.name} className="border-b border-border px-2 py-1 font-medium">
                      {c.name} <span className="text-muted-fg">({c.type})</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {evidence.rows.map((row, r) => (
                  <tr key={r}>
                    {row.map((cell, c) => {
                      const hit = r === citation.row && evidence.columns[c]?.name === citation.column;
                      return (
                        <td
                          key={c}
                          className={`px-2 py-1 font-mono ${hit ? "bg-warn-bg text-warn-fg" : ""}`}
                        >
                          {cell === null ? "NULL" : String(cell)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div>
            <div className="mb-1 font-medium">Read-only SQL that produced it</div>
            <pre className="overflow-x-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap break-words">
              {evidence.executed_sql}
            </pre>
            <div className="mt-1 text-xs text-muted-fg break-all">
              Bound parameters: <code>{JSON.stringify(evidence.parameters)}</code>
            </div>
          </div>
          <dl className={`grid grid-cols-1 gap-1 text-xs ${compact ? "" : "sm:grid-cols-3"}`}>
            <dt className="text-muted-fg">Integrity</dt>
            <dd className={compact ? "" : "sm:col-span-2"}>
              <StatusPill tone={evidence.integrity === "verified" ? "ok" : "error"}>
                {evidence.integrity === "verified" ? "hash verified" : "hash mismatch"}
              </StatusPill>
            </dd>
            <dt className="text-muted-fg">Result SHA-256</dt>
            <dd className={`font-mono break-all ${compact ? "" : "sm:col-span-2"}`}>{evidence.result_sha256}</dd>
            <dt className="text-muted-fg">Dataset</dt>
            <dd className={`font-mono break-all ${compact ? "" : "sm:col-span-2"}`}>
              {evidence.dataset_fingerprint}
            </dd>
            <dt className="text-muted-fg">Executed</dt>
            <dd className={compact ? "" : "sm:col-span-2"}>{new Date(evidence.executed_at).toLocaleString()}</dd>
            <dt className="text-muted-fg">Policy version</dt>
            <dd className={compact ? "" : "sm:col-span-2"}>{evidence.policy_epoch}</dd>
          </dl>
        </>
      ) : null}
    </>
  );
}

export function PassageDetail({
  token,
  runId,
  citation,
}: {
  token: string;
  runId: string;
  citation: Citation;
}) {
  const [passage, error] = useFetched<DocumentCitation>(
    () => fetchDocumentCitation(token, runId, citation.citation_id),
    [token, runId, citation.citation_id],
    "This source is not available to you (your access may have changed or the document was removed).",
    "Could not load the source.",
  );
  return (
    <>
      {error ? <Alert>{error}</Alert> : null}
      {!passage && !error ? <p className="text-muted-fg">Loading…</p> : null}
      {passage ? (
        <>
          <p className="text-muted-fg">
            <strong>{passage.title}</strong> · {passage.filename} · version {passage.version}
            {passage.is_current_version
              ? ""
              : ` (current is ${passage.current_version}; this is the version the answer used)`}
          </p>
          {passage.repository && passage.commit ? (
            <p className="text-xs text-muted-fg break-all">
              {passage.repository} · {passage.path} · commit <code>{passage.commit.slice(0, 12)}</code>
              {passage.line_start ? ` · lines ${passage.line_start}–${passage.line_end}` : ""}
            </p>
          ) : null}
          <blockquote className="rounded-md bg-muted p-3 whitespace-pre-wrap break-words">
            <span className="text-muted-fg">…{passage.before}</span>
            <mark className="bg-warn-bg text-warn-fg">{passage.quote}</mark>
            <span className="text-muted-fg">{passage.after}…</span>
          </blockquote>
          <p className="text-xs text-muted-fg">
            Characters {passage.char_start}–{passage.char_end} (Unicode code points) ·{" "}
            <StatusPill tone={passage.integrity === "verified" ? "ok" : "error"}>
              {passage.integrity === "verified" ? "text hash verified" : "text hash mismatch"}
            </StatusPill>
          </p>
        </>
      ) : null}
    </>
  );
}
