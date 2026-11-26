// B1 regression (docs/evidence/m7-functional/README.md): the image's CSP hard-coded
// the API at localhost:8000, so any other VITE_API_BASE_URL gave a dead UI.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { apiConnectSources, renderNginx } from "../render-nginx.mjs";

const TEMPLATE = readFileSync(new URL("../nginx.conf.template", import.meta.url), "utf8");
const csp = (conf) => /Content-Security-Policy "([^"]*)"/.exec(conf)[1];
const connectSrc = (conf) => /connect-src ([^;]*);/.exec(csp(conf))[1];

test("the default build allows the same API origins as before", () => {
  assert.equal(
    connectSrc(renderNginx(TEMPLATE, "http://localhost:8000")),
    "'self' http://localhost:8000 http://127.0.0.1:8000",
  );
});

test("a non-default API origin is allowed, and only that origin", () => {
  assert.equal(
    connectSrc(renderNginx(TEMPLATE, "http://localhost:18000")),
    "'self' http://localhost:18000 http://127.0.0.1:18000",
  );
  assert.equal(
    connectSrc(renderNginx(TEMPLATE, "https://api.niletech.example/")),
    "'self' https://api.niletech.example",
  );
  assert.equal(
    connectSrc(renderNginx(TEMPLATE, "https://API.Example.test:8443/eaios")),
    "'self' https://api.example.test:8443",
  );
  assert.deepEqual(apiConnectSources("http://[::1]:18000"), ["http://[::1]:18000"]);
});

test("everything else in the policy and the server block is unchanged", () => {
  const rendered = renderNginx(TEMPLATE, "http://localhost:18000");
  assert.equal(
    csp(rendered).replace(/connect-src [^;]*;/, "connect-src X;"),
    "default-src 'self'; connect-src X; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
  );
  assert.ok(!rendered.includes("@API_CONNECT_SRC@"));
  for (const line of ["listen 3000;", 'add_header X-Frame-Options "DENY" always;', "try_files $uri /index.html;"]) {
    assert.ok(rendered.includes(line), line);
  }
});

test("values that would broaden or break the policy fail the build", () => {
  const bad = [
    "",
    " http://localhost:8000",
    "*",
    "http://*.example.com",
    "https:",
    "localhost:8000",
    "ftp://files.example.com",
    "javascript:alert(1)",
    "data:text/plain,x",
    "http://user:pw@api.example.com",
    "http://api.example.com/?x=1",
    "http://api.example.com/#frag",
    "http://api.example.com; script-src *",
    "http://api.example.com' 'unsafe-inline",
    'http://api.example.com" always; add_header X "y',
    "http://api.example.com http://evil.example",
  ];
  for (const value of bad) {
    assert.throws(() => renderNginx(TEMPLATE, value), Error, JSON.stringify(value));
  }
  assert.throws(() => apiConnectSources(undefined));
});

test("a template without the placeholder in the CSP is refused", () => {
  assert.throws(() => renderNginx(TEMPLATE.replaceAll("@API_CONNECT_SRC@", "http://x"), "http://localhost:8000"));
});
