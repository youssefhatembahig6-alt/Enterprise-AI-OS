import type { DonePayload } from "./reports";

/** The line shown under a published answer's verification pill. Each answer type gets
 * a description of the checks its own workflow ran (backend `verification.label`):
 *
 * - `answer_quotes_verified_dev_checked`: documents and code (M3/M5).
 * - `legal_facts_extracted_and_rechecked`: contracts (M5 legal pack, deterministic, no model).
 * - `figures_verified_*`: SQL sales reports (M2), with or without a checked summary.
 *
 * An unrecognised label never borrows another workflow's wording. */
export type VerificationLine = { tone: "ok" | "warn"; text: string };

export const LEGAL_LABEL = "legal_facts_extracted_and_rechecked";
const DOCUMENT_LABEL = "answer_quotes_verified_dev_checked";
const REPORT_LABEL_PREFIX = "figures_verified_";

export function verificationText(done: DonePayload): VerificationLine | null {
  const v = done.verification;
  if (!v) return null;
  if (v.label === DOCUMENT_LABEL) {
    return {
      tone: "ok",
      text: "Every sentence quotes an exact passage of a document you can access (re-checked at publication); claims checked by the development baseline checker (not the final trained checker).",
    };
  }
  if (v.label === LEGAL_LABEL) {
    return {
      tone: "ok",
      text: "Every statement cites an exact passage of a contract you can access (re-checked at publication). Dates, terms and notice periods were re-derived from those passages by fixed rules; no model was used.",
    };
  }
  if (!v.label.startsWith(REPORT_LABEL_PREFIX)) {
    return { tone: "warn", text: "Verification details for this answer type are not available." };
  }
  if (v.narrative === "verified") {
    return {
      tone: "ok",
      text: "Figures verified exactly against the query result; summary checked by the development baseline checker (not the final trained checker).",
    };
  }
  const why: Record<string, string> = {
    withheld_checker_unavailable: "the claim checker was unavailable",
    withheld_failed_verification: "no summary passed verification",
    withheld_model_unavailable: "the model was unavailable for the summary",
  };
  return {
    tone: "warn",
    text: `Figures verified exactly against the query result. No written summary is shown because ${why[v.narrative] ?? "it could not be verified"}.`,
  };
}
