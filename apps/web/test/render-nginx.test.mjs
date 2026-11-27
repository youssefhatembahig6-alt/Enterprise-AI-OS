// B1 regression (docs/evidence/m7-functional/README.md): the image's CSP hard-coded
// the API at localhost:8000, so any other VITE_API_BASE_URL gave a dead UI.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
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

test("malformed bases that URL parsing would silently repair fail with a clear message", () => {
  const cases = [
    ["http:api.example.test", /must start with "http:\/\/" or "https:\/\/"/],
    ["https:api.example.test", /must start with "http:\/\/" or "https:\/\/"/],
    ["http:/api.example.test", /must start with "http:\/\/" or "https:\/\/"/],
    ["HTTP://api.example.test", /must start with "http:\/\/" or "https:\/\/"/],
    ["http://api.example.test?", /query or a trailing '\?'/],
    ["http://api.example.test/?", /query or a trailing '\?'/],
    ["http://api.example.test#", /fragment or a trailing '#'/],
    ["http://api.example.test/#", /fragment or a trailing '#'/],
    ["http://@api.example.test", /credentials/],
    ["http://api.example.test:0", /port outside 1-65535/],
    ["http://api.example.test:65536", /port outside 1-65535/],
    ["http://api.example.test:", /not a plain http\(s\) base URL/],
    ["http://api\\example.test", /not a plain http\(s\) base URL/],
    ["http://ex%61mple.test", /not a plain http\(s\) base URL/],
    ["http://api.example.test./", /not a plain http\(s\) base URL/],
    ["http://-api.example.test", /not a plain http\(s\) base URL/],
    ["http://api.example.test\t", /whitespace/],
  ];
  for (const [value, message] of cases) {
    assert.throws(() => apiConnectSources(value), message, JSON.stringify(value));
    assert.throws(() => renderNginx(TEMPLATE, value), message, JSON.stringify(value));
  }
});

test("the loopback alias applies to exactly localhost and 127.0.0.1", () => {
  assert.deepEqual(apiConnectSources("http://127.0.0.1:18000"), [
    "http://127.0.0.1:18000",
    "http://localhost:18000",
  ]);
  assert.deepEqual(apiConnectSources("https://localhost"), ["https://localhost", "https://127.0.0.1"]);
  // Names that an object lookup would resolve through Object.prototype.
  for (const host of ["constructor", "toString", "hasOwnProperty", "valueOf", "proto"]) {
    assert.deepEqual(apiConnectSources(`http://${host}:8000`), [`http://${host.toLowerCase()}:8000`], host);
  }
  for (const host of ["localhost.example.test", "127.0.0.2", "localhost6", "my-localhost", "[::1]"]) {
    assert.equal(apiConnectSources(`http://${host}:8000`).length, 1, host);
  }
  assert.throws(() => apiConnectSources("http://__proto__:8000"), /not a plain/);
});

const SECRET = "SYNTHETIC-PW-7f3a9c";
const SCRIPT = fileURLToPath(new URL("../render-nginx.mjs", import.meta.url));
const TEMPLATE_PATH = fileURLToPath(new URL("../nginx.conf.template", import.meta.url));
const SECRET_INPUTS = [
  `http://user:${SECRET}@api.example.test`,
  `https://${SECRET}@api.example.test:8443/`,
  `http://api.example.test/?token=${SECRET}`,
  `http://api.example.test/#${SECRET}`,
  `http:${SECRET}.example.test`,
  `http://${SECRET}.example.test:99999`,
  `ftp://${SECRET}.example.test`,
  `http://*.${SECRET}.example.test`,
  `http://api.example.test/${SECRET} x`,
];

test("rejections state the reason but never echo the value", () => {
  for (const value of SECRET_INPUTS) {
    let message = "";
    assert.throws(() => apiConnectSources(value), (error) => {
      message = error.message;
      return true;
    });
    assert.match(message, /^VITE_API_BASE_URL .+ \(value not shown\)$/, value);
    assert.ok(!message.includes(SECRET), `${value} -> ${message}`);
    assert.ok(!message.includes(value), `${value} -> ${message}`);
  }
  assert.throws(() => apiConnectSources(`http://user:${SECRET}@api.example.test`), /must not contain credentials/);
});

test("the build step's CLI fails without printing the value", () => {
  for (const value of SECRET_INPUTS) {
    let failure;
    try {
      execFileSync(process.execPath, [SCRIPT, TEMPLATE_PATH], {
        stdio: "pipe",
        env: { ...process.env, VITE_API_BASE_URL: value },
      });
    } catch (error) {
      failure = error;
    }
    assert.ok(failure, `accepted: ${value}`);
    assert.equal(failure.status, 1);
    const output = `${failure.stdout}${failure.stderr}`;
    assert.match(output, /^render-nginx: VITE_API_BASE_URL .+ \(value not shown\)\n$/);
    assert.ok(!output.includes(SECRET), output);
  }
});

test("the CLI reads the value from the environment and renders the default", () => {
  const out = execFileSync(process.execPath, [SCRIPT, TEMPLATE_PATH], {
    env: { ...process.env, VITE_API_BASE_URL: "http://localhost:8000" },
  }).toString();
  assert.equal(connectSrc(out), "'self' http://localhost:8000 http://127.0.0.1:8000");
  let failure;
  try {
    execFileSync(process.execPath, [SCRIPT, "http://localhost:8000", TEMPLATE_PATH], { stdio: "pipe" });
  } catch (error) {
    failure = error;
  }
  assert.equal(failure?.status, 2, "a value on the command line is refused (usage)");
});

test("a template without the placeholder in the CSP is refused", () => {
  assert.throws(() => renderNginx(TEMPLATE.replaceAll("@API_CONNECT_SRC@", "http://x"), "http://localhost:8000"));
});
