"""A running phase's heartbeat: while its command runs, at most every `$DT_HEARTBEAT_S` (default 60 s) its run record
is touched — the run's `updated_ts` bumped, the phase's `note` set to a live progress line — so `/scans` tells a long
phase (static-names, path-index: an hour or two between phase boundaries) from a dead job.

- `Tail`: the latest non-empty line of the phase's command output (stdout unless captured, and stderr; forwarded to
  ours unchanged), and the latest stage line (`append_runner.Runner.stage`'s `<stage>: start`), so a nested runner's
  `merge …: drill/<kind>/fold k/n` shows while its Batch job polls.
- `Heartbeat`: a daemon thread calling `write(note)` every `interval` s until stopped; the note is
  `<elapsed> · [<stage> · ]<latest line>[ (<age> ago): two beats, and a minute, old]`, secrets scrubbed (`scrub`). Best-effort: a failed write is
  counted, logged once per `warn_every` failures, and never reaches the step.
- `run_teed`: `subprocess.run`'s shape (the scan steps' `sh_impl`), its output also fed to a `Tail`.

The record writes themselves (`scan_runs.phase_beat_sql`, `JobRun.heartbeat`) live with the recorder; the run-level
beat between phases is `scan-run supervise -H` (`JobRun.beat`)."""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import partial
from typing import IO

err = partial(print, file=sys.stderr)

HEARTBEAT_ENV = "DT_HEARTBEAT_S"
#: Seconds between a running phase's record touches (one small D1 request each).
HEARTBEAT_S = 60.0
#: A note's latest line past this many characters is cut (`…`); a stage name past `STAGE_CHARS`.
LINE_CHARS = 120
STAGE_CHARS = 60
#: One warning per this many failed writes (the first, then every N-th).
WARN_EVERY = 10
#: A stage line: `append_runner.Runner.stage`'s start marker.
STAGE_RE = re.compile(r"^(?P<stage>.+): start$")
#: How long a finished command's output readers are waited on (a grandchild may hold the pipe open past its exit).
PUMP_JOIN_S = 10.0
#: The pending partial line a reader keeps (a `\r` progress bar without a newline must not grow without bound).
PENDING_BYTES = 4096

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
#: Env vars whose names say they hold a secret: their values never reach a note.
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|WEBHOOK|(?:^|_)KEY(?:_ID)?$", re.IGNORECASE)
_SECRET_TEXT = (
    # `Authorization: Bearer …`, `bearer …`
    (re.compile(r"\b(bearer|basic)\s+\S+", re.IGNORECASE), r"\1 <redacted>"),
    # `token=…`, `api_key: …`, `X-Amz-Signature=…`
    (re.compile(r"\b([\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|access[_-]?key|signature|credentials?))(\s*[=:]\s*)[^\s&,;]+",
                re.IGNORECASE), r"\1\2<redacted>"),
    # `scheme://user:pass@host`
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://<redacted>@"),
    # a long opaque run of letters *and* digits (a key, a token, a signed URL's signature); paths and job names, split
    # by `/` and `-`, never match
    (re.compile(r"(?=[A-Za-z0-9+_=]*\d)(?=[A-Za-z0-9+_=]*[A-Za-z])[A-Za-z0-9+_=]{32,}"), "<redacted>"),
)


def interval_of(env: Mapping[str, str], log: Callable[[str], None] = err) -> float | None:
    """`$DT_HEARTBEAT_S` (N[s|m|h], default `HEARTBEAT_S`); None when `0` / `off` (no heartbeat). A malformed value
    is logged and the default used: a setting never fails the job."""
    v = (env.get(HEARTBEAT_ENV) or "").strip()
    if not v:
        return HEARTBEAT_S
    if v.lower() in ("0", "off", "false", "no"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", v)
    if not m or not float(m[1]):
        log(f"heartbeat: ${HEARTBEAT_ENV}={v!r} is not a duration (N[s|m|h]); every {HEARTBEAT_S:g}s")
        return HEARTBEAT_S
    return float(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[m[2]]


def scrub(text: str, env: Mapping[str, str] | None = None, names: Iterable[str] = ()) -> str:
    """`text` without secrets: each value of `names` and of every env var whose name says it holds one (`SECRET_NAME`)
    replaced by `<NAME>` (longest first; values under 6 characters are too common to replace), then anything shaped
    like a credential (`_SECRET_TEXT`) by `<redacted>`."""
    env = env or {}
    named = set(names) | {n for n in env if SECRET_NAME.search(n)}
    vals = sorted({(v, n) for n in named if len(v := (env.get(n) or "").strip()) >= 6}, key=lambda p: -len(p[0]))
    for v, n in vals:
        text = text.replace(v, f"<{n}>")
    for pat, rep in _SECRET_TEXT:
        text = pat.sub(rep, text)
    return text


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def fmt_secs(s: float) -> str:
    """`45s`, `12m`, `1h05m`."""
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def clean_line(text: str) -> str:
    """One output line as a note shows it: ANSI escapes and control characters gone, whitespace collapsed."""
    return " ".join(_CTRL.sub(" ", _ANSI.sub("", text)).split())


class Tail:
    """The latest non-empty line fed (and when, on `clock`), and the latest stage line's stage. Thread-safe: each
    piped stream's reader feeds it."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._line: str | None = None
        self._stage: str | None = None
        self._at: float | None = None

    def feed(self, text: str) -> None:
        line = clean_line(text)
        if not line:
            return
        with self._lock:
            self._line, self._at = line, self.clock()
            if m := STAGE_RE.match(line):
                self._stage = m["stage"]

    def latest(self) -> tuple[str | None, str | None, float | None]:
        """(the latest line, the latest stage, when the line came)."""
        with self._lock:
            return self._line, self._stage, self._at

    def pump(self, src: IO[bytes], dst: IO | None) -> threading.Thread:
        """A daemon thread copying `src` to `dst` (bytes as they come: a `\\r` progress bar stays live) and feeding
        each line here, until EOF."""
        def run() -> None:
            pending = b""
            try:
                while chunk := (src.read1(65536) if hasattr(src, "read1") else src.read(65536)):
                    _write(dst, chunk)
                    *lines, pending = re.split(rb"[\r\n]", pending + chunk)
                    pending = pending[-PENDING_BYTES:]
                    for ln in lines:
                        self.feed(ln.decode(errors="replace"))
                self.feed(pending.decode(errors="replace"))
            except (OSError, ValueError):  # the pipe closed under us: the command is gone
                pass
            finally:
                try:
                    src.close()
                except OSError:
                    pass

        t = threading.Thread(target=run, daemon=True, name="heartbeat-tail")
        t.start()
        return t


def _write(dst: IO | None, chunk: bytes) -> None:
    if dst is None:
        return
    try:
        buf = getattr(dst, "buffer", None)
        if buf is not None:
            buf.write(chunk)
            buf.flush()
        else:
            dst.write(chunk.decode(errors="replace"))
            dst.flush()
    except (OSError, ValueError):
        pass


class Heartbeat:
    """`write(note)` every `interval` s on a daemon thread, from `start()` until `stop()` (which joins it; the thread
    never writes after `stop()` returns). `wait(s)` is the pause (default: the stop event's; a test passes a fake), and
    `clock` times the note's elapsed and line ages (from `start()`)."""

    def __init__(
        self,
        write: Callable[[str], None],
        interval: float,
        tail: Tail | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
        scrub: Callable[[str], str] = lambda s: s,
        log: Callable[[str], None] | None = None,
        warn_every: int = WARN_EVERY,
        label: str = "heartbeat",
    ):
        self.write, self.interval, self.tail = write, interval, tail
        self.clock, self.scrub, self.log, self.warn_every, self.label = clock, scrub, log, warn_every, label
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self.started = clock()
        self.beats = 0
        self.failures = 0
        self.thread: threading.Thread | None = None

    def note(self) -> str:
        now = self.clock()
        parts = [fmt_secs(now - self.started)]
        line, stage, at = self.tail.latest() if self.tail else (None, None, None)
        if stage and line != f"{stage}: start":
            parts.append(clip(stage, STAGE_CHARS))
        if line:
            age = now - at if at is not None else 0
            parts.append(clip(line, LINE_CHARS) + (f" ({fmt_secs(age)} ago)" if age >= max(2 * self.interval, 60) else ""))
        return self.scrub(" · ".join(parts))

    def tick(self) -> None:
        """One beat: never raises."""
        try:
            self.write(self.note())
            self.beats += 1
        except (Exception, SystemExit) as e:  # noqa: BLE001 — a heartbeat never fails the step
            self.failures += 1
            if self.failures % self.warn_every == 1 or self.warn_every == 1:
                (self.log or err)(self.scrub(f"{self.label}: write failed ({self.failures} so far; the work goes on): {type(e).__name__}: {e}"))

    def _run(self) -> None:
        while not self._wait(self.interval):
            if self._stop.is_set():
                break
            self.tick()

    def start(self) -> Heartbeat:
        self.started = self.clock()
        self.thread = threading.Thread(target=self._run, daemon=True, name=self.label)
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self.thread is not None:
            self.thread.join()

    def __enter__(self) -> Heartbeat:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def run_teed(argv: Sequence[str], env: Mapping[str, str] | None = None, stdout: int | None = None, text: bool = True,
             tail: Tail | None = None) -> subprocess.CompletedProcess:
    """`subprocess.run(argv, env=env, stdout=stdout, text=text)`, and with a `tail`, the command's stderr (and its
    stdout unless captured: `stdout=PIPE`) piped through it on the way to ours."""
    if tail is None:
        return subprocess.run(list(argv), env=None if env is None else dict(env), stdout=stdout, text=text)
    capture = stdout == subprocess.PIPE
    p = subprocess.Popen(list(argv), env=None if env is None else dict(env), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pumps = [tail.pump(p.stderr, sys.stderr)]
    if not capture:
        pumps.append(tail.pump(p.stdout, sys.stdout))
    out = p.stdout.read() if capture else None
    rc = p.wait()
    for t in pumps:
        t.join(PUMP_JOIN_S)
    if capture and p.stdout is not None:
        p.stdout.close()
    return subprocess.CompletedProcess(list(argv), rc, out.decode(errors="replace") if out is not None and text else out, None)


def pipe_kwargs(tail: Tail | None) -> dict:
    """`Popen` kwargs piping a command's output for `tail` (none without one)."""
    return {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE} if tail is not None else {}


def pump_all(p: subprocess.Popen, tail: Tail | None) -> list[threading.Thread]:
    """`tail`'s readers of a `pipe_kwargs` command's stdout and stderr (none without a tail)."""
    if tail is None:
        return []
    return [tail.pump(p.stdout, sys.stdout), tail.pump(p.stderr, sys.stderr)]


def join_all(pumps: Sequence[threading.Thread]) -> None:
    for t in pumps:
        t.join(PUMP_JOIN_S)
