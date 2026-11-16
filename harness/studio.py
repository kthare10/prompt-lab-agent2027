"""Client for the Pegasus Studio API — the surface a trial drives.

Runs **inside** the Studio container (D3): the L0 graders need `Pegasus.api`,
`pegasus-plan` and `apptainer`, none of which exist on the host. The API itself
answers on localhost:8888 from in there just as it does from outside.

Everything here is deliberately thin. The interesting behaviour lives in
`trial.py`; this module only knows how to speak to Studio and how to parse its
SSE frames.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

from harness.workspace import workflows_dir

BASE_URL = os.environ.get("STUDIO_URL", "http://localhost:8888")
# Studio moved the workspace root to $HOME (#402); `workflows_dir` is the
# one definition, read from the environment the way Studio reads it.
WORKSPACE = workflows_dir()

# A chat turn can run for many minutes across tool rounds, so the read timeout
# has to be open-ended. Connect stays short: an unreachable Studio should fail
# immediately, not after the turn budget.
CHAT_TIMEOUT = httpx.Timeout(None, connect=15.0)
API_TIMEOUT = httpx.Timeout(60.0, connect=15.0)

# Wall-clock ceiling for a single assistant turn. Needed since the output-token
# cap was lifted from Studio's chat router: a reasoning model with no ceiling
# can think for far longer than it ever could when every turn died at 8192
# tokens, and one pathological model must not stall a serial sweep (C2) for
# hours. None disables the ceiling.
#
# 2400 is set from the data, not picked: the longest *successful* trial in run
# m2-n5 (L0 >= 0.5) was a single turn of 1645.3s, and two more scoring 0.771 ran
# 1545.6s and 1304.7s. A 900s ceiling — the first guess here — would have killed
# all three. The ceiling is meant to catch a model that has stopped making
# progress, not to bound normal work, so it clears the known-good ceiling with
# room for the longer turns the lifted token cap now permits. Screens can pass
# something tighter per sweep.
TURN_TIMEOUT_S: float | None = float(os.environ.get("STUDIO_TURN_TIMEOUT", "2400"))


class StudioError(RuntimeError):
    """The API refused, or is in a state a trial must not run against."""


def _sse(response: httpx.Response) -> Iterator[dict[str, Any]]:
    """Yield the JSON payload of each `data:` frame.

    Skips `: ping` keepalives and the terminal `[DONE]`. A frame that isn't
    JSON is skipped rather than raised on — a malformed keepalive must not lose
    a turn that is otherwise fine.
    """
    for line in response.iter_lines():
        line = line.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


@dataclass
class TurnResult:
    """One assistant turn: what it said, what it did, and what it cost."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    permissions: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    frames: list[dict[str, Any]] = field(default_factory=list)
    ttft_s: float | None = None
    duration_s: float = 0.0
    request_id: str = ""
    timed_out: bool = False

    @property
    def tool_names(self) -> list[str]:
        return [c.get("name", "?") for c in self.tool_calls]


class Studio:
    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=API_TIMEOUT)

    # ── plumbing ──────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _json(self, method: str, path: str, **kw) -> Any:
        r = self._client.request(method, self._url(path), **kw)
        if r.status_code >= 400:
            raise StudioError(f"{method} {path} → {r.status_code}: {r.text[:400]}")
        return r.json()

    def health(self) -> dict:
        return self._json("GET", "/api/health")

    # ── preconditions (C1, C3) ────────────────────────────────────────────

    def llm_config(self) -> dict:
        return self._json("GET", "/api/llm/config")

    def ensure_llm_config(self) -> dict:
        """C1: chat_stream reads the *global* config before it looks at any
        per-request override, so an empty `llm_config` fails every turn no
        matter what provider/model the trial passes. Confirm one exists."""
        cfg = self.llm_config()
        if not cfg.get("provider"):
            raise StudioError(
                "No global LLM config. Activate a provider first:\n"
                "  PUT /api/llm/provider-configs {provider_id, name, base_url, "
                "default_model, is_active: true}"
            )
        return cfg

    def provider_name(self, provider_id: str) -> str:
        """What the provider is called, as the user named it in Studio.

        Recorded per trial so a board never has to keep a map of opaque ids —
        `custom-mtd4viar` means nothing to a reader, and a map is one more place
        that goes stale the first time a provider is added.
        """
        try:
            configs = self._json("GET", "/api/llm/provider-configs").get("configs") or []
        except StudioError:
            return ""
        for cfg in configs:
            if cfg.get("provider_id") == provider_id:
                return str(cfg.get("name") or "")
        return ""

    def command_approval(self) -> bool:
        return bool(self._json("GET", "/api/chat/command-approval")["require_approval"])

    def set_command_approval(self, require: bool) -> bool:
        return bool(
            self._json(
                "PUT", "/api/chat/command-approval", json={"require_approval": require}
            )["require_approval"]
        )

    def resolve_permission(self, request_id: str, call_id: str, decision: str) -> dict:
        return self._json(
            "POST",
            "/api/chat/permission",
            json={"request_id": request_id, "call_id": call_id, "decision": decision},
        )

    # ── the C2 guard ──────────────────────────────────────────────────────

    def current_turn(self) -> dict:
        return self._json("GET", "/api/chat/turn")

    def assert_container_free(self) -> None:
        """C2: `ChatTurnManager.start()` cancels whatever turn is in flight, so
        a second trial against one Studio does not error — it silently truncates
        the first, which then grades as a model failure. Refuse to start."""
        turn = self.current_turn()
        if turn.get("active"):
            raise StudioError(
                f"A chat turn is already active on {self.base_url} "
                f"(turn_id={turn.get('turn_id')}, conversation={turn.get('conversation_id')}). "
                "Starting another would cancel it — take an exclusive lease (D5)."
            )

    def assert_turn_was_ours(self, conversation_id: str) -> None:
        """Post-flight half of the same guard.

        A finished turn stays queryable for ~120s, so if the retained turn
        belongs to a different conversation, something else used this container
        while our trial was running and the trial must be quarantined rather
        than scored.
        """
        turn = self.current_turn()
        seen = turn.get("conversation_id")
        if seen is not None and seen != conversation_id:
            raise StudioError(
                f"Turn ownership changed mid-trial: expected conversation "
                f"{conversation_id!r}, container reports {seen!r}. Quarantine this trial."
            )

    # ── chat ──────────────────────────────────────────────────────────────

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        provider: str | None = None,
        model: str | None = None,
        agent: str = "general",
        conversation_id: str,
        auto_approve: bool = True,
        timeout_s: float | None = -1.0,
    ) -> TurnResult:
        """Run one assistant turn to completion, collecting every SSE frame.

        ``timeout_s`` caps the turn's wall clock; -1.0 means TURN_TIMEOUT_S,
        None means no ceiling. On expiry the turn is **stopped server-side**
        before returning (see below) and ``TurnResult.timed_out`` is set —
        the partial text and tool calls collected so far are kept, so a
        timed-out turn is still gradable rather than lost.
        """
        if timeout_s == -1.0:
            timeout_s = TURN_TIMEOUT_S
        request_id = uuid.uuid4().hex
        body: dict[str, Any] = {
            "messages": messages,
            "agent": agent,
            "conversation_id": conversation_id,
            "request_id": request_id,
        }
        if provider:
            body["provider"] = provider
        if model:
            body["model"] = model

        result = TurnResult(request_id=request_id)
        started = time.monotonic()
        # A finite read timeout bounds the other failure shape: a turn that
        # goes silent rather than slow. The in-loop elapsed check below cannot
        # fire while iter_lines() is blocked waiting for a frame that never
        # comes.
        chat_timeout = (
            CHAT_TIMEOUT if timeout_s is None
            else httpx.Timeout(timeout_s, connect=15.0)
        )
        try:
            with httpx.Client(timeout=chat_timeout) as client:
                with client.stream("POST", self._url("/api/chat/stream"), json=body) as r:
                    if r.status_code >= 400:
                        r.read()
                        raise StudioError(f"chat/stream → {r.status_code}: {r.text[:400]}")
                    for frame in _sse(r):
                        if timeout_s is not None and time.monotonic() - started > timeout_s:
                            result.timed_out = True
                            break
                        result.frames.append(frame)
                        if "content" in frame:
                            if result.ttft_s is None:
                                result.ttft_s = time.monotonic() - started
                            result.text += frame["content"]
                        elif "tool_call" in frame:
                            result.tool_calls.append(frame["tool_call"])
                        elif "tool_result" in frame:
                            result.tool_results.append(frame["tool_result"])
                        elif "permission_request" in frame:
                            req = frame["permission_request"]
                            result.permissions.append(req)
                            # C3: the turn is parked in await_decision until this is
                            # answered. Unattended, silence is a hang, not a denial.
                            if auto_approve:
                                self.resolve_permission(request_id, req.get("id", ""), "approve")
                        elif "meta" in frame:
                            result.meta = frame["meta"]
                        elif "error" in frame:
                            result.error = frame["error"]
        except httpx.ReadTimeout:
            # The turn went silent rather than slow: no frame arrived within
            # the ceiling, so the in-loop check above never got to run. Same
            # outcome, same bookkeeping — not a harness fault.
            result.timed_out = True
        except Exception:
            # Any other abnormal exit — a dropped connection, a proxy closing
            # the stream (nginx caps /api/ at proxy_read_timeout 300s) — leaves
            # the turn running in the container, and the next trial then
            # refuses to start (C2/D5). Stop it before the exception leaves.
            self.stop(request_id)
            raise
        result.duration_s = time.monotonic() - started
        if result.timed_out:
            # C2/D5: abandoning the stream client-side leaves the turn active in
            # the container, and the next trial then refuses to start (or gets
            # cancelled by it). Stop it server-side before handing back.
            self.stop(request_id)
        return result

    def stop(self, request_id: str, verify_s: float = 30.0) -> bool:
        """Stop a turn, and confirm it actually stopped.

        This used to post and hope. It did not check the response, swallowed a
        StudioError, and never looked at whether the turn cleared — so a stop
        that failed was indistinguishable from one that worked.

        The cost of that: on 2026-09-04 a `turn_timeout` on t05a left
        `turn_id=d57e101d86c0` active, and the C2/D5 guard then refused to start
        every following trial. **229 of 252 Stage 1 cells died in seconds**,
        each recorded as `harness_error`. The guard was right; the cleanup was
        not.

        Returns whether the container is free afterwards.
        """
        try:
            self._json("POST", "/api/chat/stop", json={"request_id": request_id})
        except StudioError as e:
            log_stop = f"stop request failed: {e}"
        else:
            log_stop = ""

        deadline = time.monotonic() + verify_s
        while time.monotonic() < deadline:
            try:
                if not self.current_turn().get("active"):
                    return True
            except StudioError:
                pass                      # transient; keep checking until deadline
            time.sleep(1.0)

        raise StudioError(
            "Turn did not stop and the container is still busy"
            + (f" ({log_stop})" if log_stop else "")
            + ". Every following trial would fail the C2/D5 guard, so the sweep "
            "is stopping here rather than burning the rest of the matrix."
        )

    # ── workflow projects ─────────────────────────────────────────────────

    def projects(self) -> list[dict]:
        return self._json("GET", "/api/workflows/projects")["projects"]

    def project(self, project_id: str) -> dict | None:
        return next((p for p in self.projects() if p["project_id"] == project_id), None)

    def params(self, project_id: str) -> dict:
        """The generator's argparse schema, introspected the way Studio's own
        UI reads it — `dest`, `flag`, `type`, `required`, `default`, `choices`.

        The Generate button renders a form from this and passes the answers as
        `args`. A generator with a required argument is therefore normal and
        supported; calling generate with no args at all is what is not.
        """
        return self._json(
            "GET", f"/api/workflows/projects/{project_id}/params",
            timeout=httpx.Timeout(120.0, connect=15.0),
        )

    def generate(self, project_id: str, args: list[str] | None = None) -> dict:
        return self._json(
            "POST",
            f"/api/workflows/projects/{project_id}/generate",
            json={"args": args or []},
            timeout=httpx.Timeout(600.0, connect=15.0),
        )

    def build(self, project_id: str, tag: str = "latest", timeout_s: float = 3600.0) -> dict:
        """Build the project's container image, draining the SSE stream.

        An image build is the only stage that can take tens of minutes, and it is
        where a wrong base image actually fails — `GLIBC_2.38 not found` appears
        here, not at generate. Grading t03 without it would score the definition's
        text and never find out whether it builds.
        """
        lines: list[str] = []
        status, error = "unknown", None
        with httpx.Client(timeout=httpx.Timeout(timeout_s, connect=15.0)) as client:
            with client.stream(
                "POST", self._url(f"/api/workflows/projects/{project_id}/build"),
                json={"tag": tag, "push": False},
            ) as r:
                if r.status_code >= 400:
                    r.read()
                    raise StudioError(f"build → {r.status_code}: {r.text[:300]}")
                for frame in _sse(r):
                    if line := frame.get("line"):
                        lines.append(str(line))
                    if phase := frame.get("phase"):
                        lines.append(f"── {phase} ──")
                    if frame.get("status"):
                        status = str(frame["status"])
                    if frame.get("error"):
                        error = str(frame["error"])
        return {
            "status": "built" if status not in ("error",) and not error else "error",
            "raw_status": status,
            "error": error,
            "output": "\n".join(lines)[-4000:],
        }

    def plan(
        self, project_id: str, site: str = "condorpool", output_site: str = "local"
    ) -> dict:
        return self._json(
            "POST",
            f"/api/workflows/projects/{project_id}/plan",
            json={"site": site, "output_site": output_site},
            timeout=httpx.Timeout(900.0, connect=15.0),
        )
