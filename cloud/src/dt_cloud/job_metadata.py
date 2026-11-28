"""A local job's own GCE metadata server (specs/vm-pilot.md "Identity"): the subset of the metadata API Google's client
libraries ask for, answered for one service account, so every process in the job's containers (`dt-cloud`, the
`disk-tree` CLI, gcsfs, `google-cloud-storage`, Go's and Java's clients, a host-side gcsfuse mount of the job's
buckets) authenticates as the job's account, with tokens refreshed for as long as the job runs.

The local job backend (`job_backend.Local`) starts one per job, bound to the gateway of the job's own Docker network,
and points the job's containers at it (`$GCE_METADATA_HOST`, `$GCE_METADATA_IP`, `$GCE_METADATA_ROOT`: the variables
Python's, Go's and C++'s clients read). It answers only requests from that network's subnet and from the host's own
addresses (`local_addresses`: the host's gcsfuse mounts reached it from the VM's primary address, not the gateway's,
on the box, 2026-11-28) carrying `Metadata-Flavor: Google`, so another job's
containers, on networks of their own, get 403. The host's real metadata server stays closed to every container
(`deploy/vm-pilot/docker-no-metadata.conf`).

Tokens are minted under the host's credentials (`Identity.access_token`: IAM Credentials `generateAccessToken`, which
the box's account may call only for the tenants' job accounts) and reused until `refresh_s` before they expire.
"""
from __future__ import annotations

import ipaddress
import json
import sys
import threading
import time
from collections.abc import Sequence, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
#: The variables a client reads the metadata server's address from: Python's google-auth and Go's
#: `cloud.google.com/go/compute/metadata` (`HOST`), google-auth's GCE check (`IP`), google-cloud-cpp and gcloud (`ROOT`).
ADDRESS_ENVS = ("GCE_METADATA_HOST", "GCE_METADATA_IP", "GCE_METADATA_ROOT")
FLAVOR = ("Metadata-Flavor", "Google")
#: A token is reminted once it has less than this long left.
REFRESH_S = 300
PREFIX = "/computeMetadata/v1"


class MetadataServer:
    """GCE's metadata API for `account` (in `project`), on `bind` (port `port`, 0: any), to clients in `allow`.

    `mint(lifetime_s)` returns a fresh access token for the account, good for `lifetime_s`."""

    def __init__(self, account: str, project: str, mint: Callable[[int], str], *, bind: str, allow: str | Sequence[str],
                 port: int = 0, lifetime_s: int = 3600, refresh_s: float = REFRESH_S,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] | None = None):
        self.account, self.project, self.mint = account, project, mint
        self.bind, self.port = bind, port
        self.allow = [ipaddress.ip_network(a, strict=False) for a in ([allow] if isinstance(allow, str) else allow)]
        self.lifetime_s, self.refresh_s, self.clock = lifetime_s, refresh_s, clock
        self.log = log or (lambda s: print(s, file=sys.stderr, flush=True))
        self._lock = threading.Lock()
        self._token: tuple[str, float] | None = None
        self._server: ThreadingHTTPServer | None = None

    # ── tokens ──

    def token(self) -> tuple[str, float]:
        """The account's current token and when it expires (epoch seconds), minted when there is none or it has less
        than `refresh_s` left."""
        with self._lock:
            now = self.clock()
            if self._token is None or self._token[1] - now < self.refresh_s:
                self._token = (self.mint(self.lifetime_s), now + self.lifetime_s)
            return self._token

    # ── the server ──

    @property
    def address(self) -> str:
        """`host:port`, what `ADDRESS_ENVS` are set to."""
        if self._server is None:
            raise RuntimeError("metadata server not started")
        return f"{self.bind}:{self._server.server_address[1]}"

    def env(self) -> dict[str, str]:
        return {k: self.address for k in ADDRESS_ENVS}

    def start(self) -> str:
        """Serve in a daemon thread; returns `address`."""
        server = ThreadingHTTPServer((self.bind, self.port), _handler(self))
        server.daemon_threads = True
        self._server = server
        threading.Thread(target=server.serve_forever, name="job-metadata", daemon=True).start()
        return self.address

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def answer(self, client: str, path: str, headers: dict[str, str]) -> tuple[int, str, str]:
        """`(status, content type, body)` for a GET of `path` (with its query) from `client`."""
        if not any(ipaddress.ip_address(client) in a for a in self.allow):
            self.log(f"job metadata: refused {client} (not in {', '.join(map(str, self.allow))}): {urlsplit(path).path}")
            return 403, "text/plain", "forbidden\n"
        if headers.get(FLAVOR[0].lower()) != FLAVOR[1]:
            return 403, "text/plain", "Missing Metadata-Flavor:Google header.\n"
        url = urlsplit(path)
        query = parse_qs(url.query)
        p = url.path
        if p in ("", "/"):
            return 200, "application/text", "computeMetadata/\n"
        if not p.startswith(PREFIX):
            return 404, "text/plain", "not found\n"
        p = p[len(PREFIX):]
        text = "application/text"
        if p in ("", "/"):
            return 200, text, "instance/\nproject/\nuniverse/\n"
        if p == "/project/project-id":
            return 200, text, self.project
        if p == "/universe/universe-domain":
            return 200, text, "googleapis.com"
        if p.rstrip("/") == "/instance/service-accounts":
            return 200, text, f"default/\n{self.account}/\n"
        parts = p.split("/")  # "", "instance", "service-accounts", <who>, [<what>]
        if len(parts) < 4 or parts[1:3] != ["instance", "service-accounts"] or parts[3] not in ("default", self.account):
            return 404, "text/plain", "not found\n"
        what = "/".join(parts[4:]).rstrip("/")
        if what == "":
            if query.get("recursive", [""])[0].lower() == "true":
                return 200, "application/json", json.dumps({"aliases": ["default"], "email": self.account, "scopes": [SCOPE]})
            return 200, text, "aliases\nemail\nscopes\ntoken\n"
        if what == "email":
            return 200, text, self.account
        if what == "aliases":
            return 200, text, "default\n"
        if what == "scopes":
            return 200, text, f"{SCOPE}\n"
        if what == "token":
            try:
                token, expires = self.token()
            except Exception as e:  # noqa: BLE001 — the client sees a 500 and retries; the reason goes to the log
                self.log(f"job metadata: minting a token for {self.account} failed: {type(e).__name__}: {e}")
                return 500, "text/plain", f"token for {self.account} unavailable\n"
            body = {"access_token": token, "expires_in": max(0, int(expires - self.clock())), "token_type": "Bearer"}
            return 200, "application/json", json.dumps(body)
        return 404, "text/plain", "not found\n"


def _handler(server: MetadataServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server's name
            status, ctype, body = server.answer(self.client_address[0], self.path, {k.lower(): v for k, v in self.headers.items()})
            data = body.encode()
            self.send_response(status)
            self.send_header(*FLAVOR)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — http.server's name
            pass  # each request is routine; refusals and failures are logged by `answer`

    return Handler


def local_addresses(ip_json: str | None = None) -> list[str]:
    """Every IPv4 address this host owns (`ip -j -4 addr`'s `local`s), loopback included; `ip_json` instead of running
    `ip`. The job's metadata server answers them all: the host's own processes (its gcsfuse mounts) reach the gateway
    from whichever one the kernel picks, which on the box was the primary address, not the gateway (2026-11-28), and
    only this host's processes can use them; another job's containers come from their own subnets."""
    import json as _json
    import subprocess

    if ip_json is None:
        try:
            ip_json = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return []
    return [a["local"] for i in _json.loads(ip_json or "[]") for a in i.get("addr_info", []) if a.get("family") == "inet"]
