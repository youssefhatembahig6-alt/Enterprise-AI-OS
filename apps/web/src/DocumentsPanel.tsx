import { Alert, Button, Card, Field, StatusPill, type StatusTone } from "@eaios/ui";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError } from "./api";
import { listDocuments, uploadDocument, type DocumentSummary } from "./reports";

const IN_PROGRESS = new Set(["queued", "parsing", "indexing"]);

function statusTone(doc: DocumentSummary): StatusTone {
  if (doc.status === "ready") return doc.searchable ? "ok" : "warn";
  if (IN_PROGRESS.has(doc.status)) return "unknown";
  return "error";
}

function statusLabel(doc: DocumentSummary): string {
  if (doc.status === "ready" && doc.permission_state === "unknown") return "Indexed · sharing not set";
  if (doc.status === "ready") return doc.searchable ? "Searchable" : "Not searchable";
  if (doc.status === "needs_ocr") return "Needs OCR";
  if (IN_PROGRESS.has(doc.status)) return `Ingesting (${doc.status})`;
  return doc.status === "failed" ? "Failed" : doc.status;
}

async function toBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

export function DocumentsPanel({ token, canUpload }: { token: string; canUpload: boolean }) {
  const [docs, setDocs] = useState<DocumentSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [department, setDepartment] = useState("finance");
  const [classification, setClassification] = useState("internal");
  const [groups, setGroups] = useState("all-staff");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setDocs(await listDocuments(token));
    } catch {
      setError("Could not load documents.");
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!docs.some((d) => IN_PROGRESS.has(d.status))) return;
    const timer = setInterval(() => void refresh(), 1500);
    return () => clearInterval(timer);
  }, [docs, refresh]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      await uploadDocument(token, {
        filename: file.name,
        contentBase64: await toBase64(file),
        department: department.trim(),
        classification: classification === "unknown" ? null : classification,
        groups: groups.split(",").map((g) => g.trim()).filter(Boolean),
        departmentRestricted: false,
        countries: [],
      });
      setFile(null);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Documents">
      <div className="flex flex-col gap-4">
        {canUpload ? (
          <form className="flex flex-col gap-3" onSubmit={(e) => void submit(e)}>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="doc-file" className="text-sm font-medium">
                Upload (PDF with a text layer, Markdown, text, CSV or XLSX; up to 10 MB)
              </label>
              <input
                id="doc-file"
                type="file"
                accept=".pdf,.md,.markdown,.txt,.csv,.xlsx"
                className="text-sm"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Field id="doc-dept" label="Department" value={department}
                     onChange={(e) => setDepartment(e.target.value)} />
              <div className="flex flex-col gap-1.5">
                <label htmlFor="doc-class" className="text-sm font-medium">Classification</label>
                <select id="doc-class" className="h-10 rounded-md border border-border bg-bg px-2 text-sm"
                        value={classification} onChange={(e) => setClassification(e.target.value)}>
                  <option value="public">Public</option>
                  <option value="internal">Internal</option>
                  <option value="confidential">Confidential</option>
                  <option value="restricted">Restricted</option>
                  <option value="unknown">Decide later (not searchable)</option>
                </select>
              </div>
              <Field id="doc-groups" label="Share with groups" value={groups}
                     onChange={(e) => setGroups(e.target.value)} placeholder="all-staff, finance-team" />
            </div>
            <div>
              <Button type="submit" disabled={busy || !file}>
                {busy ? "Uploading…" : "Upload"}
              </Button>
            </div>
          </form>
        ) : null}
        {error ? <Alert>{error}</Alert> : null}
        {docs.length === 0 ? (
          <p className="text-sm text-muted-fg">No documents you can see yet.</p>
        ) : (
          <ul className="divide-y divide-border text-sm">
            {docs.map((doc) => (
              <li key={doc.document_id} className="flex flex-col gap-1 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{doc.title}</span>
                  <StatusPill tone={statusTone(doc)}>{statusLabel(doc)}</StatusPill>
                  <span className="text-xs text-muted-fg">
                    {doc.classification} · {doc.department} · v{doc.current_version}
                  </span>
                </div>
                {doc.error ? <span className="text-xs text-error-fg">{doc.error.message}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}
