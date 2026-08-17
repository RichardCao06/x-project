"""Dependency-free local HTTP server for the LCA dashboard."""
from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .service import DashboardService


STATIC_ROOT = Path(__file__).with_name("static")


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], root: str | Path) -> None:
        self.dashboard = DashboardService(root)
        super().__init__(address, DashboardHandler)
        self.dashboard.start_goal_reconciler()

    def server_close(self) -> None:
        self.dashboard.stop_goal_reconciler()
        super().server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if size > 64 * 1024:
            raise ValueError("request body is too large")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _static(self, route: str) -> None:
        relative = "index.html" if route in {"/", ""} else route.removeprefix("/")
        path = (STATIC_ROOT / relative).resolve()
        try:
            path.relative_to(STATIC_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not path.is_file():
            path = STATIC_ROOT / "index.html"
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(content)

    @staticmethod
    def _arg(query: dict[str, list[str]], name: str, default: str = "") -> str:
        return query.get(name, [default])[0]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route, query = unquote(parsed.path), parse_qs(parsed.query)
        try:
            if route == "/api/health":
                self._json({"status": "ok", "service": "lca-dashboard"})
            elif route == "/api/overview":
                self._json(self.server.dashboard.overview())
            elif route == "/api/jobs":
                self._json(self.server.dashboard.jobs(
                    status=self._arg(query, "status"), query=self._arg(query, "q"),
                    limit=int(self._arg(query, "limit", "50")), offset=int(self._arg(query, "offset", "0"))))
            elif route == "/api/skills":
                self._json(self.server.dashboard.skill_catalog())
            elif route == "/api/workers":
                self._json(self.server.dashboard.workers())
            elif match := re.fullmatch(r"/api/jobs/([^/]+)", route):
                self._json(self.server.dashboard.job(match.group(1)))
            elif route == "/api/workflows":
                self._json(self.server.dashboard.workflow_runs(
                    status=self._arg(query, "status"), limit=int(self._arg(query, "limit", "100"))))
            elif route == "/api/artifacts":
                self._json(self.server.dashboard.artifacts(
                    query=self._arg(query, "q"), media_type=self._arg(query, "media_type"),
                    limit=int(self._arg(query, "limit", "60")), offset=int(self._arg(query, "offset", "0"))))
            elif match := re.fullmatch(r"/api/artifacts/([0-9a-f]{64})", route):
                self._json(self.server.dashboard.artifact(match.group(1)))
            elif route == "/api/events":
                self._json(self.server.dashboard.events(
                    query=self._arg(query, "q"), event_type=self._arg(query, "event_type"),
                    limit=int(self._arg(query, "limit", "100")), after=int(self._arg(query, "after", "0"))))
            elif route == "/api/exceptions":
                self._json(self.server.dashboard.exceptions(
                    status=self._arg(query, "status"), limit=int(self._arg(query, "limit", "100"))))
            elif route == "/api/system":
                self._json(self.server.dashboard.system())
            elif route == "/api/goal-alignment":
                self._json(self.server.dashboard.goal_alignment(
                    job_id=self._arg(query, "job_id") or None,
                    limit=int(self._arg(query, "limit", "100"))))
            elif route == "/api/autonomy":
                self._json(self.server.dashboard.autonomy(
                    limit=int(self._arg(query, "limit", "100"))))
            elif match := re.fullmatch(r"/api/autonomy/([^/]+)", route):
                self._json(self.server.dashboard.autonomy(campaign_id=match.group(1)))
            elif route.startswith("/api/"):
                self._json({"status": "error", "message": "API route not found"}, 404)
            else:
                self._static(route)
        except KeyError as exc:
            self._json({"status": "error", "message": f"not found: {exc.args[0]}"}, 404)
        except (ValueError, RuntimeError) as exc:
            self._json({"status": "error", "error": type(exc).__name__, "message": str(exc)}, 400)

    def do_POST(self) -> None:  # noqa: N802
        route = unquote(urlparse(self.path).path)
        try:
            body = self._body()
            if route == "/api/jobs":
                allowed = {"skill", "request", "idempotency_key", "materialize"}
                extras = sorted(set(body) - allowed)
                if extras:
                    raise ValueError(f"unknown task creation fields: {extras}")
                if "materialize" in body and not isinstance(body["materialize"], bool):
                    raise ValueError("materialize must be a boolean")
                self._json(self.server.dashboard.create_job(
                    body.get("skill", ""), body.get("request"),
                    idempotency_key=body.get("idempotency_key"),
                    materialize=body.get("materialize", False),
                ), 201)
            elif route == "/api/autonomy":
                allowed = {"spec", "start"}
                extras = sorted(set(body) - allowed)
                if extras or not isinstance(body.get("spec"), dict):
                    raise ValueError(f"autonomy requires spec object; unknown fields: {extras}")
                start = body.get("start", True)
                if not isinstance(start, bool):
                    raise ValueError("start must be boolean")
                self._json(self.server.dashboard.create_autonomy(body["spec"], start=start), 201)
            elif match := re.fullmatch(r"/api/jobs/([^/]+)/materialize", route):
                self._json(self.server.dashboard.materialize(match.group(1)))
            elif match := re.fullmatch(r"/api/jobs/([^/]+)/worker", route):
                self._json(self.server.dashboard.start_worker(match.group(1)), 202)
            elif match := re.fullmatch(r"/api/jobs/([^/]+)/pause", route):
                if body.get("confirm") is not True:
                    raise ValueError("pause requires confirm=true")
                self._json(self.server.dashboard.pause_job(match.group(1)))
            elif match := re.fullmatch(r"/api/jobs/([^/]+)/resume", route):
                if body.get("confirm") is not True:
                    raise ValueError("resume requires confirm=true")
                self._json(self.server.dashboard.resume_job(match.group(1)))
            elif match := re.fullmatch(r"/api/jobs/([^/]+)/goal-audit", route):
                auto_repair = body.get("auto_repair", False)
                if not isinstance(auto_repair, bool):
                    raise ValueError("auto_repair must be boolean")
                self._json(self.server.dashboard.audit_goal(match.group(1), auto_repair=auto_repair))
            elif match := re.fullmatch(r"/api/jobs/([^/]+)/goal-feedback", route):
                message = str(body.get("message") or "").strip()
                if not message:
                    raise ValueError("feedback message is required")
                self._json(self.server.dashboard.goal_feedback(
                    match.group(1), message, str(body.get("category") or "user_feedback")))
            elif match := re.fullmatch(r"/api/autonomy/([^/]+)/start", route):
                self._json(self.server.dashboard.start_autonomy(match.group(1)), 202)
            elif match := re.fullmatch(r"/api/autonomy/([^/]+)/pause", route):
                if body.get("confirm") is not True:
                    raise ValueError("autonomy pause requires confirm=true")
                self._json(self.server.dashboard.pause_autonomy(match.group(1)))
            elif match := re.fullmatch(r"/api/autonomy/([^/]+)/resume", route):
                self._json(self.server.dashboard.resume_autonomy(match.group(1), start=True))
            elif match := re.fullmatch(r"/api/workflows/([^/]+)/tasks/([^/]+)/recover", route):
                if body.get("confirm") is not True:
                    raise ValueError("recover requires confirm=true")
                self._json(self.server.dashboard.recover(match.group(1), match.group(2)))
            else:
                self._json({"status": "error", "message": "API route not found"}, 404)
        except KeyError as exc:
            self._json({"status": "error", "message": f"not found: {exc.args[0]}"}, 404)
        except (json.JSONDecodeError, ValueError, RuntimeError) as exc:
            self._json({"status": "error", "error": type(exc).__name__, "message": str(exc)}, 400)


def serve(root: str | Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = DashboardHTTPServer((host, port), root)
    try:
        print(f"LCA Dashboard: http://{host}:{server.server_port}")
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
