"""The REST client against a local HTTP server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mcs_harness.github import API_VERSION, ApiError, GitHub

TOKEN = "test-token-not-real-0123456789"


class Server:
    def __init__(self):
        self.requests = []
        self.routes = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_one(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                # self.headers looks names up without regard to case, like HTTP.
                server.requests.append((method, self.path, self.headers, body))
                path = self.path.split("?")[0]
                answers = server.routes.get((method, path))
                status, headers, data = answers.pop(0) if len(answers) > 1 else answers[0]
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value.replace("BASE", server.base))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.handle_one("GET")

            def do_POST(self):
                self.handle_one("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def route(self, method, path, *answers):
        self.routes[(method, path)] = list(answers)


@pytest.fixture
def server():
    s = Server()
    yield s
    s.httpd.shutdown()


def client(server):
    return GitHub("o/r", TOKEN, base=server.base, sleep=lambda s: None)


def ok(value):
    return 200, {"Content-Type": "application/json"}, json.dumps(value).encode()


def test_dispatch_sends_version_header_and_no_return_run_details(server):
    server.route("POST", "/repos/o/r/actions/workflows/measure.yml/dispatches",
                 ok({"workflow_run_id": 5, "run_url": "a", "html_url": "b", "extra": 1}))
    response = client(server).dispatch("measure.yml", "main", {"sign": "true"})
    assert response == {"workflow_run_id": 5, "run_url": "a", "html_url": "b"}
    method, path, headers, body = server.requests[0]
    assert headers["X-GitHub-Api-Version"] == API_VERSION == "2026-03-10"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(body) == {"ref": "main", "inputs": {"sign": "true"}}


def test_dispatch_is_never_repeated(server):
    server.route("POST", "/repos/o/r/actions/workflows/measure.yml/dispatches",
                 (500, {}, b'{"message": "boom"}'))
    with pytest.raises(ApiError) as error:
        client(server).dispatch("measure.yml", "main", {})
    assert len(server.requests) == 1
    assert TOKEN not in str(error.value)


def test_dispatch_without_run_details_is_an_error(server):
    server.route("POST", "/repos/o/r/actions/workflows/measure.yml/dispatches", (204, {}, b""))
    with pytest.raises(ApiError):
        client(server).dispatch("measure.yml", "main", {})


def test_get_is_repeated_after_5xx(server):
    server.route("GET", "/repos/o/r/actions/runs/1", (502, {}, b"{}"), (503, {}, b"{}"), ok({"id": 1}))
    assert client(server).get_run(1) == {"id": 1}
    assert len(server.requests) == 3


def test_get_gives_up_after_four_attempts(server):
    server.route("GET", "/repos/o/r/actions/runs/1", (503, {}, b"{}"))
    with pytest.raises(ApiError):
        client(server).get_run(1)
    assert len(server.requests) == 4


def test_get_is_not_repeated_after_404(server):
    server.route("GET", "/repos/o/r/actions/runs/1", (404, {}, b'{"message": "Not Found"}'))
    with pytest.raises(ApiError):
        client(server).get_run(1)
    assert len(server.requests) == 1


def test_download_follows_the_redirect_without_token(server):
    server.route("GET", "/repos/o/r/actions/artifacts/9/zip", (302, {"Location": "BASE/blob/9?sig=x"}, b""))
    server.route("GET", "/blob/9", (200, {}, b"zip bytes"))
    assert client(server).download_artifact(9) == b"zip bytes"
    (_, _, api_headers, _), (_, blob_path, blob_headers, _) = server.requests
    assert api_headers["Authorization"] == f"Bearer {TOKEN}"
    assert blob_path == "/blob/9?sig=x"
    assert "Authorization" not in blob_headers


def test_list_runs_pages_and_passes_the_filter(server):
    page = [{"id": i} for i in range(100)]
    server.route("GET", "/repos/o/r/actions/workflows/measure.yml/runs",
                 ok({"workflow_runs": page}), ok({"workflow_runs": [{"id": 100}]}))
    runs = client(server).list_runs("measure.yml", ">=2026-10-15T18:00:00+00:00", "main")
    assert len(runs) == 101
    query = server.requests[0][1]
    assert "created=%3E%3D2026-10-15T18%3A00%3A00%2B00%3A00" in query and "event=workflow_dispatch" in query


def test_token_is_not_in_repr():
    assert TOKEN not in repr(GitHub("o/r", TOKEN))
