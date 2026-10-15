"""Client for the GitHub REST API, version 2026-03-10.

Only the standard library (urllib). Every request sets the header
X-GitHub-Api-Version: 2026-03-10.

- GET requests are repeated after network errors, 5xx answers and rate limit
  answers, at most four attempts in total. They only read.
- POST requests are never repeated. A failed dispatch is reported to the caller,
  which looks the run up by its run name before anything else happens.
- Download endpoints answer 302 with a short lived URL of a storage service.
  The client follows it by hand and without the Authorization header.
- The token is never part of a log line, an exception or repr().
- A network error is logged and reported with its cause (URLError.reason,
  for example name resolution, refused connection, TLS or timeout) and the
  time the attempt took. The token is removed from the cause.
- TLS certificates are checked against the CA bundle of certifi (pinned in
  uv.lock), independent of the OpenSSL setup of the Python installation.

Endpoints and fields: REST reference of version 2026-03-10,
src/rest/data/fpt-2026-03-10/actions.json in github/docs at commit
12691d2d8a5e1a1d03c9f168f625786d576158f2 (read on 2026-10-15).
"""

import json
import logging
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import certifi

API = "https://api.github.com"
API_VERSION = "2026-03-10"
USER_AGENT = "mcs-pipeline-harness"
GET_ATTEMPTS = 4
BACKOFF_S = (5, 15, 45)
MAX_RATE_LIMIT_WAIT_S = 3600

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, method, path, status, message, headers=None):
        super().__init__(f"{method} {path}: {status} {message}")
        self.method = method
        self.path = path
        self.status = status
        self.message = message
        self.headers = headers


class TokenError(Exception):
    pass


def token_from_environment():
    """GH_TOKEN or GITHUB_TOKEN, else the token of the GitHub CLI (gh auth token)."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        done = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TokenError(f"gh auth token failed: {type(error).__name__}") from None
    token = done.stdout.strip()
    if done.returncode != 0 or not token:
        raise TokenError("no token: set GH_TOKEN or log in with gh auth login")
    return token


def tls_context():
    return ssl.create_default_context(cafile=certifi.where())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, repository, token, base=API, sleep=time.sleep, timeout=60):
        self.repository = repository
        self._token = token
        self.base = base.rstrip("/")
        self.sleep = sleep
        self.timeout = timeout
        context = tls_context()
        self._opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))
        self._download_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    def __repr__(self):
        return f"GitHub(repository={self.repository!r}, base={self.base!r})"

    # Transport

    def _headers(self, accept):
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }

    def _send(self, method, path, params=None, body=None, accept="application/vnd.github+json"):
        """One request. Returns (status, headers, body bytes) for 2xx and 3xx."""
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = self._headers(accept)
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                return error.code, error.headers, b""
            text = error.read()[:500].decode("utf-8", "replace")
            raise ApiError(method, path, error.code, _message(text), error.headers) from None

    def get(self, path, params=None, accept="application/vnd.github+json"):
        """GET with up to four attempts. Returns (status, headers, body)."""
        for attempt in range(GET_ATTEMPTS):
            started = time.monotonic()
            try:
                status, headers, body = self._send("GET", path, params, accept=accept)
                self._respect_rate_limit(headers)
                return status, headers, body
            except ApiError as error:
                rate_limited = error.status in (403, 429) and "rate limit" in error.message.lower()
                if not (error.status >= 500 or rate_limited) or attempt == GET_ATTEMPTS - 1:
                    raise
                wait = _rate_limit_wait_s(error.headers) if rate_limited else BACKOFF_S[attempt]
                log.warning("GET %s: %s, attempt %d of %d, next in %d s",
                            path, error.status, attempt + 1, GET_ATTEMPTS, wait)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                cause = f"{_cause(error, self._token)} after {time.monotonic() - started:.1f} s"
                if attempt == GET_ATTEMPTS - 1:
                    raise ApiError("GET", path, "network", cause) from None
                wait = BACKOFF_S[attempt]
                log.warning("GET %s: %s, attempt %d of %d, next in %d s",
                            path, cause, attempt + 1, GET_ATTEMPTS, wait)
            self.sleep(wait)
        raise AssertionError("unreachable")

    def _respect_rate_limit(self, headers):
        remaining = headers.get("X-RateLimit-Remaining") if headers else None
        reset = headers.get("X-RateLimit-Reset") if headers else None
        if remaining is None or reset is None:
            return
        if int(remaining) < 20:
            wait = min(max(int(reset) - int(time.time()), 0) + 5, MAX_RATE_LIMIT_WAIT_S)
            log.warning("rate limit: %s requests left, waiting %d s", remaining, wait)
            self.sleep(wait)

    def get_json(self, path, params=None):
        status, _, body = self.get(path, params)
        if status != 200:
            raise ApiError("GET", path, status, "expected 200")
        return json.loads(body)

    def download(self, path):
        """Body of a download endpoint (302 to a storage URL, followed without token)."""
        status, headers, body = self.get(path)
        if status == 200:
            return body
        location = headers.get("Location") if headers else None
        if status not in (301, 302, 303, 307, 308) or not location:
            raise ApiError("GET", path, status, "expected a redirect")
        if urllib.parse.urlsplit(location).scheme != "https" and not self.base.startswith("http://"):
            raise ApiError("GET", path, status, "redirect to a URL without https")
        for attempt in range(GET_ATTEMPTS):
            # A new request without the Authorization header.
            request = urllib.request.Request(location, headers={"User-Agent": USER_AGENT})
            started = time.monotonic()
            try:
                with self._download_opener.open(request, timeout=self.timeout) as response:
                    return response.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                cause = f"{_cause(error, self._token)} after {time.monotonic() - started:.1f} s"
                if attempt == GET_ATTEMPTS - 1:
                    raise ApiError("GET", path + " (download)", "network", cause) from None
                log.warning("download %s: %s, attempt %d of %d", path, cause, attempt + 1, GET_ATTEMPTS)
                self.sleep(BACKOFF_S[attempt])
        raise AssertionError("unreachable")

    # Endpoints

    def repo_path(self, suffix):
        return f"/repos/{self.repository}{suffix}"

    def dispatch(self, workflow, ref, inputs):
        """POST .../actions/workflows/{workflow_id}/dispatches, never repeated.

        In version 2026-03-10 the endpoint answers 200 with the schema "Workflow
        Dispatch Response": workflow_run_id (integer), run_url and html_url, all
        required. The parameter return_run_details no longer exists and is not sent.
        """
        path = self.repo_path(f"/actions/workflows/{workflow}/dispatches")
        started = time.monotonic()
        try:
            status, _, body = self._send("POST", path, body={"ref": ref, "inputs": inputs})
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            cause = f"{_cause(error, self._token)} after {time.monotonic() - started:.1f} s"
            raise ApiError("POST", path, "network", cause) from None
        if status != 200:
            raise ApiError("POST", path, status, "expected 200 with run details")
        response = json.loads(body)
        missing = [k for k in ("workflow_run_id", "run_url", "html_url") if k not in response]
        if missing:
            raise ApiError("POST", path, status, f"response without {', '.join(missing)}")
        return {k: response[k] for k in ("workflow_run_id", "run_url", "html_url")}

    def head_commit(self, ref):
        return self.get_json(self.repo_path(f"/commits/{urllib.parse.quote(ref, safe='')}"))["sha"]

    def get_run(self, run_id):
        return self.get_json(self.repo_path(f"/actions/runs/{run_id}"))

    def list_runs(self, workflow, created, branch):
        """workflow_dispatch runs of a workflow on a branch, created filter like >=2026-10-15T18:00:00+00:00."""
        runs = []
        page = 1
        while True:
            body = self.get_json(
                self.repo_path(f"/actions/workflows/{workflow}/runs"),
                {
                    "event": "workflow_dispatch",
                    "branch": branch,
                    "created": created,
                    "per_page": 100,
                    "page": page,
                },
            )
            runs.extend(body.get("workflow_runs", []))
            if len(body.get("workflow_runs", [])) < 100:
                return runs
            page += 1

    def list_active_runs(self, workflow):
        active = []
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            body = self.get_json(
                self.repo_path(f"/actions/workflows/{workflow}/runs"),
                {"status": status, "per_page": 100},
            )
            active.extend(body.get("workflow_runs", []))
        return active

    def list_jobs(self, run_id):
        body = self.get_json(
            self.repo_path(f"/actions/runs/{run_id}/jobs"), {"filter": "latest", "per_page": 100}
        )
        return body.get("jobs", [])

    def list_run_artifacts(self, run_id, name):
        body = self.get_json(
            self.repo_path(f"/actions/runs/{run_id}/artifacts"), {"name": name, "per_page": 100}
        )
        return body.get("artifacts", [])

    def download_artifact(self, artifact_id):
        return self.download(self.repo_path(f"/actions/artifacts/{artifact_id}/zip"))

    def download_job_log(self, job_id):
        return self.download(self.repo_path(f"/actions/jobs/{job_id}/logs"))


def _rate_limit_wait_s(headers):
    """Retry-After, else the reset time of the rate limit, else 60 s."""
    if headers is not None:
        retry_after = headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(int(retry_after), MAX_RATE_LIMIT_WAIT_S)
        reset = headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            return min(max(int(reset) - int(time.time()), 0) + 5, MAX_RATE_LIMIT_WAIT_S)
    return 60


def _cause(error, token):
    """Class and cause of a network error for log and exceptions, without the token.

    urllib wraps the cause of a failed connection in URLError.reason: an
    exception (gaierror, ConnectionRefusedError, SSLCertVerificationError,
    TimeoutError) or a string.
    """
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        detail = f"{type(reason).__name__}: {reason}" if isinstance(reason, BaseException) else str(reason)
    else:
        detail = str(error)
    text = f"{type(error).__name__} ({detail or 'no detail'})"
    if token:
        text = text.replace(token, "***")
    return text[:300]


def _message(text):
    try:
        return json.loads(text).get("message", "")[:200]
    except (ValueError, AttributeError):
        return text[:200]
