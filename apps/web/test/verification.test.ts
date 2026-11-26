// B3 regression (docs/evidence/m7-functional/README.md): a verified contract answer
// showed the SQL report's "query result … no written summary" warning.
import assert from "node:assert/strict";
import { test } from "node:test";
import type { DonePayload, Verification } from "../src/reports.js";
import { verificationText } from "../src/verification.js";

function done(verification: Partial<Verification> & { label: string }): DonePayload {
  return {
    message_id: "m1",
    outcome: "published",
    verification: { narrative: "", inference_mode: "fake", checker: null, ...verification } as Verification,
    abstention: null,
    inference: "fake",
  };
}

// The payload the legal pack sends (backend/app/legal/workflow.py: verification()); it
// has no "narrative" field.
const LEGAL = {
  label: "legal_facts_extracted_and_rechecked",
  method: "deterministic_extraction_no_model",
  quotes: "exact_spans_of_cited_versions",
  values: "re_derived_from_cited_quotes",
  access: "rechecked_at_publication",
  inference_mode: "fake",
  model_used: false,
  checker: null,
} as unknown as Verification;

test("a verified contract answer describes the legal checks, not the SQL report", () => {
  const line = verificationText({ ...done({ label: "x" }), verification: LEGAL });
  assert.ok(line);
  assert.equal(line.tone, "ok");
  assert.match(line.text, /exact passage of a contract you can access/);
  assert.match(line.text, /re-checked at publication/);
  assert.match(line.text, /no model was used/);
  assert.doesNotMatch(line.text, /query result|summary|figures/i);
});

test("sales report messages are unchanged", () => {
  assert.deepEqual(
    verificationText(done({ label: "figures_verified_narrative_dev_checked", narrative: "verified" })),
    {
      tone: "ok",
      text: "Figures verified exactly against the query result; summary checked by the development baseline checker (not the final trained checker).",
    },
  );
  const withheld: [string, string][] = [
    ["withheld_checker_unavailable", "the claim checker was unavailable"],
    ["withheld_failed_verification", "no summary passed verification"],
    ["withheld_model_unavailable", "the model was unavailable for the summary"],
    ["something_new", "it could not be verified"],
  ];
  for (const [narrative, why] of withheld) {
    assert.deepEqual(
      verificationText(done({ label: "figures_verified_structured_only", narrative })),
      {
        tone: "warn",
        text: `Figures verified exactly against the query result. No written summary is shown because ${why}.`,
      },
    );
  }
});

test("document (and code) answer message is unchanged", () => {
  assert.deepEqual(verificationText(done({ label: "answer_quotes_verified_dev_checked" })), {
    tone: "ok",
    text: "Every sentence quotes an exact passage of a document you can access (re-checked at publication); claims checked by the development baseline checker (not the final trained checker).",
  });
});

test("an unknown label never borrows the SQL report wording", () => {
  const line = verificationText(done({ label: "some_future_pack_checked" }));
  assert.ok(line);
  assert.equal(line.tone, "warn");
  assert.doesNotMatch(line.text, /query result/);
});

test("no verification block shows no line", () => {
  assert.equal(verificationText({ ...done({ label: "x" }), verification: null }), null);
});
