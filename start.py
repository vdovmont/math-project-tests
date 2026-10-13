#!/usr/bin/env python3
"""Start the MathCore GUI, or run console tests with --tester [tester options]."""

from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import gui_tester
import gui_optimizer
from project_storage import PROJECT_FOLDER, prepare_storage
from settings_storage import UrlStorageError

from config import (
    GUI_URL,
    HTML_FILE,
    MAX_REQUEST_BODY_BYTES,
)

GUI_MODULES = (gui_tester, gui_optimizer)


class GuiRequestHandler(BaseHTTPRequestHandler):
    server_version = "MathCoreGUI/1.0"

    def log_message(self, format_string: str, *args: object) -> None:
        # Frequent polling should not obscure runner output.
        return

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data: object, status: int = 200) -> None:
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8", status)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                self.send_bytes(HTML_FILE.read_bytes(), "text/html; charset=utf-8")
                return
            for module in GUI_MODULES:
                if path in module.GET_PATHS:
                    module.handle_get(self)
                    return
        except OSError as error:
            self.send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        module = next((item for item in GUI_MODULES if path in item.POST_PATHS), None)
        if module is None:
            self.send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json({"error": "Invalid Content-Length."}, HTTPStatus.BAD_REQUEST)
            return
        max_body_size = (
            gui_tester.MAX_UPLOAD_REQUEST_BODY_BYTES
            if path in {"/api/add-test-type", "/api/optimizer/files/add"}
            else MAX_REQUEST_BODY_BYTES
        )
        if content_length <= 0 or content_length > max_body_size:
            self.send_json({"error": "Invalid request-body size."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            data = json.loads(self.rfile.read(content_length).decode("utf-8"))
            result, status = module.handle_post(path, data)
        except (UnicodeDecodeError, ValueError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        except RuntimeError as error:
            self.send_json({"error": str(error)}, HTTPStatus.CONFLICT)
            return
        except (gui_tester.ProfileStorageError, gui_tester.TestSelectionStorageError,
                UrlStorageError,
                OSError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json(result, status)


def parse_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=GUI_URL,
        help=f"URL on which to host the GUI (default: {GUI_URL}).",
    )
    return parser.parse_args(arguments)


def parse_gui_url(url: str) -> tuple[str, int, str]:
    parsed = urlparse(url.strip())
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("GUI URL must be a valid http:// URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("GUI URL must not contain credentials, a query, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise ValueError("GUI URL path must be empty or '/'.")

    try:
        port = parsed.port or 80
    except ValueError as error:
        raise ValueError("GUI URL contains an invalid port.") from error
    if not 1 <= port <= 65535:
        raise ValueError("GUI URL port must be between 1 and 65535.")

    display_url = url.strip().rstrip("/") + "/"
    return parsed.hostname, port, display_url


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if arguments and arguments[0] == "--tester":
        import json_http_tester
        # Help should not migrate or create storage.
        if not any(arg in {"-h", "--help"} for arg in arguments[1:]):
            try:
                prepare_storage()
            except OSError as error:
                print(f"Could not prepare storage: {error}", file=sys.stderr)
                return 1
        return json_http_tester.run(arguments[1:])
    args = parse_arguments(arguments)
    try:
        host, port, gui_url = parse_gui_url(args.url)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    try:
        prepare_storage()
        gui_tester.create_required_folders()
    except OSError as error:
        print(error, file=sys.stderr)
        return 1

    try:
        server = ThreadingHTTPServer((host, port), GuiRequestHandler)
    except OSError as error:
        print(f"Could not host the GUI at {gui_url}: {error}", file=sys.stderr)
        return 1

    print(f"MathCore GUI is available at {gui_url}")
    print("Press Ctrl+C to stop the GUI server.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping GUI server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
