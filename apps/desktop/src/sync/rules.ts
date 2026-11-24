/**
 * What the desktop connector uploads (M5, ADR 0503). Mirrors
 * backend/app/connectors/paths.py and the backend's supported formats; the backend
 * re-applies every rule, so a modified client cannot widen what is ingested.
 */

export const MAX_FILE_BYTES = 10 * 1024 * 1024;
export const MAX_DEPTH = 20;
export const MAX_FILES = 5000;
export const MAX_PATH_CHARS = 1024;

const DOCUMENT_EXTENSIONS = [".md", ".markdown", ".txt", ".csv", ".xlsx", ".pdf"];
const CODE_EXTENSIONS = [
  ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".go", ".rs",
  ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala", ".sh", ".bash",
  ".ps1", ".sql", ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".html", ".css", ".scss",
  ".xml", ".gradle", ".r", ".lua", ".dart", ".vue",
];
export const SUPPORTED_EXTENSIONS = new Set([...DOCUMENT_EXTENSIONS, ...CODE_EXTENSIONS]);

const EXCLUDED_DIRS = new Set([
  "node_modules", "vendor", "dist", "build", "target", "coverage", "__pycache__", "venv",
  "site-packages",
]);
const EXCLUDED_FILES = new Set([
  "thumbs.db", "desktop.ini", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
  "poetry.lock", "cargo.lock", "composer.lock",
]);
const EXCLUDED_SUFFIXES = [".tmp", ".temp", ".swp", ".swx", ".part", ".crdownload", ".bak", ".map"];
const EXCLUDED_PATTERNS = [/^~\$/, /~$/, /\.min\.(js|css)$/];

export type SkipReason =
  | "hidden_folder"
  | "hidden_file"
  | "dependency_or_build_folder"
  | "excluded_file"
  | "temporary_or_generated_file"
  | "unsupported_type"
  | "too_large"
  | "symlink_not_followed"
  | "too_deep"
  | "invalid_path"
  | "too_many_files";

/** Why a directory name is never descended into, or null. */
export function excludedDirectory(name: string): SkipReason | null {
  if (name.startsWith(".")) return "hidden_folder";
  if (EXCLUDED_DIRS.has(name.toLowerCase())) return "dependency_or_build_folder";
  return null;
}

/** Why a file (relative, "/"-separated path) is never uploaded, or null. */
export function excludedFile(relPath: string): SkipReason | null {
  const parts = relPath.split("/");
  for (const dir of parts.slice(0, -1)) {
    const reason = excludedDirectory(dir);
    if (reason) return reason;
  }
  const name = parts[parts.length - 1] ?? "";
  const lower = name.toLowerCase();
  if (name.startsWith(".")) return "hidden_file";
  if (EXCLUDED_FILES.has(lower)) return "excluded_file";
  if (EXCLUDED_SUFFIXES.some((s) => lower.endsWith(s)) || EXCLUDED_PATTERNS.some((p) => p.test(name))) {
    return "temporary_or_generated_file";
  }
  if (!SUPPORTED_EXTENSIONS.has(extension(name))) return "unsupported_type";
  return null;
}

export function extension(name: string): string {
  const match = /(\.[A-Za-z0-9]+)$/.exec(name);
  return match?.[1]?.toLowerCase() ?? "";
}

/** A relative path the backend will accept (no "..", absolute, backslash or control chars). */
export function validRelative(relPath: string): boolean {
  if (!relPath || relPath.length > MAX_PATH_CHARS) return false;
  // eslint-disable-next-line no-control-regex
  if (/[\x00-\x1f\x7f\\]/.test(relPath)) return false;
  if (relPath.startsWith("/") || /^[A-Za-z]:/.test(relPath)) return false;
  const parts = relPath.split("/");
  if (parts.length > MAX_DEPTH) return false;
  return parts.every((p) => p !== "" && p !== "." && p !== ".." && p.length <= 255);
}
