// Renders nginx.conf.template for the production web image (bug B1, M7 acceptance).
//
// The bundle calls the API at VITE_API_BASE_URL, so the CSP's connect-src must allow
// exactly that origin: no wildcard, no scheme-only source, nothing else. Both come from
// the same build argument, so they cannot disagree. Run in the image build:
//
//   node render-nginx.mjs "$VITE_API_BASE_URL" nginx.conf.template > nginx.conf
//
// The build fails on anything that is not a plain http(s) origin, naming the reason but
// never the rejected value.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const PLACEHOLDER = "@API_CONNECT_SRC@";
// The whole base URL, checked on the raw text before any parsing (the WHATWG parser
// silently repairs "http:host" and drops an empty "?" or "#"):
//   scheme "http" or "https", then "//", a DNS name, IPv4 or bracketed IPv6 literal, an
//   optional port, and an optional path of unreserved characters. No userinfo, query,
//   fragment, backslash, percent-encoding, whitespace or wildcard.
const BASE_URL = /^(https?):\/\/(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*)(?::(\d{1,5}))?(\/[A-Za-z0-9._~\/-]*)?$/;
// Exactly the two loopback spellings; never a lookup on an object with a prototype.
const LOOPBACK_ALIAS = new Map([
  ["localhost", "127.0.0.1"],
  ["127.0.0.1", "localhost"],
]);

// Errors name the reason only. The rejected value is never echoed: it may carry a
// credential or other secret, and build logs are widely readable.
function reject(reason) {
  throw new Error(`VITE_API_BASE_URL ${reason} (value not shown)`);
}

/** The connect-src sources for an API base URL: its origin, plus the other spelling
 * of the loopback host when it is exactly localhost or 127.0.0.1 (the policy allowed
 * both before this change). Throws a descriptive error on anything else. */
export function apiConnectSources(apiBaseUrl) {
  if (typeof apiBaseUrl !== "string" || apiBaseUrl === "") reject("must be set");
  if (/\s/.test(apiBaseUrl)) reject("must not contain whitespace");
  if (apiBaseUrl.includes("*")) reject("must not contain a wildcard");
  if (!/^https?:/i.test(apiBaseUrl)) reject("must use http or https");
  if (!/^https?:\/\//.test(apiBaseUrl)) {
    reject('must start with "http://" or "https://" (lower case, with both slashes)');
  }
  if (apiBaseUrl.includes("?")) reject("must not contain a query or a trailing '?'");
  if (apiBaseUrl.includes("#")) reject("must not contain a fragment or a trailing '#'");
  if (apiBaseUrl.includes("@")) reject("must not contain credentials");
  const match = BASE_URL.exec(apiBaseUrl);
  if (!match) reject("is not a plain http(s) base URL (host, optional port and path)");
  const [, scheme, , port] = match;
  if (port !== undefined && (Number(port) < 1 || Number(port) > 65535)) {
    reject("has a port outside 1-65535");
  }
  let url;
  try {
    url = new URL(apiBaseUrl);
  } catch {
    reject("is not a valid URL");
  }
  // The parser must agree with the raw text: same scheme, no repairs.
  if (url.protocol !== `${scheme}:` || url.username || url.password || url.search || url.hash) {
    reject("was changed by URL parsing; write it in canonical form");
  }
  const sources = [url.origin];
  const alias = LOOPBACK_ALIAS.get(url.hostname);
  if (alias !== undefined) sources.push(`${url.protocol}//${alias}${url.port ? `:${url.port}` : ""}`);
  return sources;
}

export function renderNginx(template, apiBaseUrl) {
  if (template.split(PLACEHOLDER).length !== 3) {
    // One in the explanatory comment, one in the CSP header.
    throw new Error(`the template must contain ${PLACEHOLDER} exactly twice`);
  }
  const header = template.split("\n").filter((line) => !line.startsWith("#")).join("\n");
  if (!header.includes(`connect-src 'self' ${PLACEHOLDER};`)) {
    throw new Error("the template's CSP must read connect-src 'self' " + PLACEHOLDER + ";");
  }
  return header.replace(PLACEHOLDER, apiConnectSources(apiBaseUrl).join(" "));
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [apiBaseUrl, templatePath] = process.argv.slice(2);
  if (!templatePath) {
    console.error("usage: node render-nginx.mjs <api base url> <template>");
    process.exit(2);
  }
  try {
    process.stdout.write(renderNginx(readFileSync(templatePath, "utf8"), apiBaseUrl));
  } catch (error) {
    console.error(`render-nginx: ${error.message}`);
    process.exit(1);
  }
}
