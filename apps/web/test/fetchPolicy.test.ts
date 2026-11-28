// Request credentials (M8 private Colab preview): cookies only for the same-origin preview
// build, and the bearer token is sent the same way in every build.

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { api, FETCH_CREDENTIALS } from "../src/api.js";
import { fetchCredentials } from "../src/fetchPolicy.js";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

test("same-origin credentials only for the preview build: relative API base and the explicit flag", () => {
  assert.equal(fetchCredentials("same-origin", ""), "same-origin");
  // Default and production builds.
  assert.equal(fetchCredentials(undefined, ""), "omit");
  assert.equal(fetchCredentials(undefined, "http://localhost:8000"), "omit");
  // The flag never applies to an absolute API base, even the page's own origin.
  assert.equal(fetchCredentials("same-origin", "http://localhost:8000"), "omit");
  assert.equal(fetchCredentials("same-origin", "https://eaios.example"), "omit");
  // Cookies never go cross-origin, whatever the flag says.
  assert.equal(fetchCredentials("include", ""), "omit");
  assert.equal(fetchCredentials("include", "https://eaios.example"), "omit");
  assert.equal(fetchCredentials("", ""), "omit");
});

test("a default build omits cookies and still authenticates with the bearer token", async () => {
  assert.equal(FETCH_CREDENTIALS, "omit");
  const seen: RequestInit[] = [];
  globalThis.fetch = (async (_url: RequestInfo | URL, init?: RequestInit) => {
    seen.push(init ?? {});
    return new Response(JSON.stringify({ email: "finance.reader@niletech.example" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  await api.me("token-123");
  assert.equal(seen.length, 1);
  assert.equal(seen[0]?.credentials, "omit");
  assert.equal(new Headers(seen[0]?.headers).get("Authorization"), "Bearer token-123");
});
