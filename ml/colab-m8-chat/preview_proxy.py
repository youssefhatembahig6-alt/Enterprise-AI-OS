"""Same-origin preview server for the private Colab chat preview (M8). Standard library only.

Serves a preview build of apps/web (built with an empty VITE_API_BASE_URL, so the UI calls
``/api/...`` on its own origin) and forwards ``/api/*``, ``/health`` and ``/ready`` to the
backend API on loopback, streaming responses (Server-Sent Events) as they arrive. Colab's
own ``google.colab.output.serve_kernel_port_as_window`` then shows this ONE port to the
notebook's owner in their session; nothing else is exposed and no tunnel is opened.

    python preview_proxy.py --dist apps/web/dist-preview --port 13100 --api 127.0.0.1:18000
"""

from __future__ import annotations

import argparse
import http.client
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}
# Colab's port window authenticates the browser with its own session cookie (the preview
# build sends cookies same-origin for that reason). They are Colab's, not the app's: the
# app authenticates with the bearer token, so cookies never reach the API and none come back.
REQUEST_DROP = HOP_BY_HOP | {"cookie"}
RESPONSE_DROP = HOP_BY_HOP | {"set-cookie"}
# Same rules as the production nginx image: everything from this origin only.
CSP = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; base-uri 'none'; form-action 'self'"
)


def make_handler(dist: Path, api_host: str, api_port: int) -> type[BaseHTTPRequestHandler]:
    root = dist.resolve()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # close after each response: streams need no length
        server_version = "eaios-preview"

        def log_message(self, fmt: str, *args: object) -> None:  # quiet
            return

        def _proxy(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in REQUEST_DROP}
            upstream = http.client.HTTPConnection(api_host, api_port, timeout=900)
            try:
                upstream.request(self.command, self.path, body=body, headers=headers)
                response = upstream.getresponse()
            except OSError:
                self.send_error(502, "API unreachable")
                return
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in RESPONSE_DROP:
                    self.send_header(key, value)
            self.end_headers()
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
            upstream.close()

        def _static(self, path: str) -> None:
            target = (root / path.lstrip("/")).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                target = root / "index.html"  # single-page app: unknown paths get the shell
            data = target.read_bytes()
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if target.name == "index.html":
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _route(self) -> None:
            path = urlsplit(self.path).path
            if path.startswith("/api/") or path in ("/health", "/ready"):
                self._proxy()
            elif self.command in ("GET", "HEAD"):
                self._static(path)
            else:
                self.send_error(405)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _route  # noqa: N815 - BaseHTTPRequestHandler names

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dist", required=True, type=Path)
    ap.add_argument("--port", type=int, default=13100)
    ap.add_argument("--api", default="127.0.0.1:18000", help="host:port of the backend API")
    args = ap.parse_args()
    host, port = args.api.rsplit(":", 1)
    if not (args.dist / "index.html").is_file():
        raise SystemExit(f"{args.dist} has no index.html (build the preview UI first)")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.dist, host, int(port)))
    print(f"preview on http://127.0.0.1:{args.port} -> API {args.api}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
