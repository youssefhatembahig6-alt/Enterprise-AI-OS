/**
 * Which credentials the browser attaches to API requests. No imports, so it runs under
 * node:test.
 *
 * Authentication is always the in-memory bearer token in the Authorization header
 * (ADR 0101); the app never sets or reads cookies. The default is "omit".
 *
 * The private Colab chat preview (M8, ml/colab-m8-chat) is the one exception. It is served
 * with its API on one origin behind Colab's port window, and that window's proxy admits a
 * request only with the Colab session cookie the browser already holds for that origin.
 * The preview build therefore sets VITE_FETCH_CREDENTIALS=same-origin with an empty
 * VITE_API_BASE_URL. "same-origin" is honoured only when the API base is relative (every
 * request goes to the page's own origin); any other value or combination gives "omit".
 * "include" is never used, so cookies never go to another origin.
 */

export function fetchCredentials(
  mode: string | undefined,
  apiBaseUrl: string,
): RequestCredentials {
  return mode === "same-origin" && apiBaseUrl === "" ? "same-origin" : "omit";
}
