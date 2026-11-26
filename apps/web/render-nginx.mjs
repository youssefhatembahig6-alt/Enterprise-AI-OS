// Renders nginx.conf.template for the production web image (bug B1, M7 acceptance).
//
// The bundle calls the API at VITE_API_BASE_URL, so the CSP's connect-src must allow
// exactly that origin: no wildcard, no scheme-only source, nothing else. Both come from
// the same build argument, so they cannot disagree. Run in the image build:
//
//   node render-nginx.mjs "$VITE_API_BASE_URL" nginx.conf.template > nginx.conf
//
// The build fails on anything that is not a plain http(s) origin.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const PLACEHOLDER = "@API_CONNECT_SRC@";
// Hostnames, IPv4 and bracketed IPv6 literals, with an optional port. Nothing that could
// end the header value or add a source (quotes, spaces, semicolons, commas, '*').
const SAFE_ORIGIN = /^https?:\/\/(?:[a-z0-9-]+(?:\.[a-z0-9-]+)*|\[[0-9a-f:.]+\])(?::\d{1,5})?$/;
const LOOPBACK = { localhost: "127.0.0.1", "127.0.0.1": "localhost" };

/** The connect-src sources for an API base URL: its origin, plus the other spelling
 * of the loopback host when it is localhost or 127.0.0.1 (the policy allowed both
 * before this change). Throws on anything else. */
export function apiConnectSources(apiBaseUrl) {
  if (typeof apiBaseUrl !== "string" || apiBaseUrl.trim() !== apiBaseUrl || apiBaseUrl === "") {
    throw new Error("VITE_API_BASE_URL must be a non-empty URL without surrounding spaces");
  }
  if (apiBaseUrl.includes("*")) throw new Error("VITE_API_BASE_URL must not contain a wildcard");
  let url;
  try {
    url = new URL(apiBaseUrl);
  } catch {
    throw new Error(`VITE_API_BASE_URL is not a valid URL: ${JSON.stringify(apiBaseUrl)}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("VITE_API_BASE_URL must use http or https");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error("VITE_API_BASE_URL must not carry credentials, a query or a fragment");
  }
  const origin = url.origin;
  if (!SAFE_ORIGIN.test(origin)) {
    throw new Error(`VITE_API_BASE_URL has an unsupported origin: ${JSON.stringify(origin)}`);
  }
  const sources = [origin];
  const alias = LOOPBACK[url.hostname];
  if (alias) sources.push(`${url.protocol}//${alias}${url.port ? `:${url.port}` : ""}`);
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
