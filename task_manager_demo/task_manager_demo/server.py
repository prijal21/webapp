"""Local HTTP API and web server for the task management demo.

Usage:
    python server.py
    python server.py --port 9000 --data tasks.json

Then open http://127.0.0.1:8000 in your browser.
No third-party packages are required.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from storage import TaskNotFound, TaskRepository


print("Starting task dashboard server...")
print("Python version:", ".".join(str(x) for x in (Path(__file__).parent.parent / "pyproject.toml").read_text().splitlines()[0].split()[1].split(".")))
BASE_DIR = Path(__file__).resolve().parent
MAX_BODY_BYTES = 64 * 1024


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def create_handler(repository: TaskRepository):
    """Build a request handler attached to a repository instance."""

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "LocalTaskDashboard/1.0"

        def log_message(self, message: str, *args: object) -> None:
            print(f"[{self.log_date_time_string()}] {self.client_address[0]}: {message % args}")

        def reply(self, code: int, payload: object) -> None:
            body = json_bytes(payload)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def error_response(self, status: int, message: str) -> None:
            self.reply(status, {"error": message, "status": status})

        def send_html(self) -> None:
            try:
                body = (BASE_DIR / "index.html").read_bytes()
            except OSError:
                self.error_response(500, "index.html was not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def read_json(self) -> dict:
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ValueError("Content-Length is required")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("Invalid Content-Length") from exc
            if length < 0 or length > MAX_BODY_BYTES:
                raise ValueError("Request body is too large")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Invalid JSON body") from exc
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
            return value

        def route(self) -> tuple[str, dict[str, list[str]]]:
            parsed = urlsplit(self.path)
            return parsed.path.rstrip("/") or "/", parse_qs(parsed.query)

        def handle_error(self, exc: Exception) -> None:
            if isinstance(exc, TaskNotFound):
                self.error_response(404, "Task not found")
            elif isinstance(exc, ValueError):
                self.error_response(400, str(exc))
            else:
                print(f"Unexpected server error: {exc}")
                self.error_response(500, "An internal server error occurred")

        def do_GET(self) -> None:
            path, params = self.route()
            if path == "/":
                self.send_html()
                return
            try:
                if path == "/api/health":
                    self.reply(200, {"status": "ok", "service": "task-dashboard"})
                elif path == "/api/tasks":
                    def param(key: str, default: str = "") -> str:
                        return params.get(key, [default])[0]
                    tasks = repository.list_tasks(
                        status=param("status") or None,
                        priority=param("priority") or None,
                        query=param("q"),
                        sort_by=param("sort", "created_at"),
                        descending=param("order", "desc") != "asc",
                    )
                    self.reply(200, {"tasks": tasks, "count": len(tasks)})
                elif path == "/api/summary":
                    self.reply(200, repository.summary())
                elif path.startswith("/api/tasks/") and path.count("/") == 3:
                    self.reply(200, {"task": repository.get_task(path.split("/")[-1])})
                else:
                    self.error_response(404, "Endpoint not found")
            except Exception as exc:
                self.handle_error(exc)

        def do_POST(self) -> None:
            path, _ = self.route()
            if path != "/api/tasks":
                self.error_response(404, "Endpoint not found")
                return
            try:
                task = repository.create_task(self.read_json())
                self.reply(201, {"message": "Task created", "task": task})
            except Exception as exc:
                self.handle_error(exc)

        def do_PATCH(self) -> None:
            path, _ = self.route()
            if not path.startswith("/api/tasks/") or path.count("/") != 3:
                self.error_response(404, "Endpoint not found")
                return
            try:
                task = repository.update_task(path.split("/")[-1], self.read_json())
                self.reply(200, {"message": "Task updated", "task": task})
            except Exception as exc:
                self.handle_error(exc)

        def do_DELETE(self) -> None:
            path, _ = self.route()
            if not path.startswith("/api/tasks/") or path.count("/") != 3:
                self.error_response(404, "Endpoint not found")
                return
            try:
                repository.delete_task(path.split("/")[-1])
                self.reply(200, {"message": "Task deleted"})
            except Exception as exc:
                self.handle_error(exc)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Allow", "GET, POST, PATCH, DELETE, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return DashboardHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local task dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--data", default=str(BASE_DIR / "tasks.json"), help="JSON data file")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    repository = TaskRepository(args.data)
    server = ThreadingHTTPServer((args.host, args.port), create_handler(repository))
    print(f"Task dashboard running at http://{args.host}:{server.server_port}")
    print(f"Data file: {Path(args.data).resolve()}")
    print("Press Ctrl+C to stop the server")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
