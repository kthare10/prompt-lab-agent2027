"""Drive Studio's chat when the engine is OpenCode rather than the builtin loop.

On `studio-webapp` main the chat panel is a client of `opencode serve`, and
`studio-api/routers/opencode.py` is a **verbatim byte proxy** to it: nothing on
the Python side parses OpenCode's payloads. So measuring what a user actually
has means speaking OpenCode's own v1 API through that proxy, not Studio's chat
vocabulary.

The surface was read from a running server's `/doc`, not from the SDK — the
generated client at 1.18.29 trails its own server (no `POST
/permission/{id}/reply`, and its `Event` union still says `permission.updated`
where the server emits `permission.asked`). A throwaway container running the
image's own `opencode serve` produced the 162-path document this is built
against.

What a turn looks like here:

    POST /api/opencode/session                      -> {id: "ses_…"}
    GET  /api/opencode/event                        (SSE, global, filter by sessionID)
    POST /api/opencode/session/{id}/prompt_async    -> returns immediately
    ... message.part.updated × n ...                -> text, tool, reasoning parts
    session.idle                                    -> the turn is over

The stream is opened **before** the prompt is sent. `prompt_async` returns as
soon as the turn is admitted, so a stream opened afterwards races the first
part, and the parts are the whole record — there is no second copy to reconcile
from except `GET /session/{id}/message`, which is a poor substitute for having
watched it happen.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import urllib.parse
import time
import uuid
from typing import Any

import httpx

from harness.studio import (API_TIMEOUT, BASE_URL, Studio, StudioError,
                            TURN_TIMEOUT_S, TurnResult, _sse)

PREFIX = "/api/opencode"

# A tool part carries its call and its result in one object, mutated in place as
# the state advances. Studio's chat sent those as two different frames, and the
# grader counts them separately, so the states are split back out here.
_DONE = ("completed", "error")


class OpenCode(Studio):
    """A Studio whose chat half speaks OpenCode. The workflow half is unchanged.

    `generate`, `build`, `plan` and the rest are Studio's own API either way —
    only the conversation moved — so this subclasses rather than replaces, and
    a trial can switch engines without the grading pipeline noticing.
    """

    def __init__(self, base_url: str = BASE_URL) -> None:
        super().__init__(base_url)
        self._sessions: dict[str, str] = {}      # conversation_id -> session id

    # ── plumbing ──────────────────────────────────────────────────────────

    def _call(self, method: str, path: str, **kw) -> Any:
        """Like `Studio._json`, but tolerant of an empty body.

        `POST /session/{id}/prompt_async` answers **204 with no content** — the
        turn is admitted, there is nothing to say about it yet — and calling
        `.json()` on that raises `JSONDecodeError: line 1 column 1 (char 0)`,
        which the sweep records as a harness error. Several of the engine's
        other POSTs answer the same way.
        """
        r = self._client.request(method, self._url(path), **kw)
        if r.status_code >= 400:
            raise StudioError(f"{method} {path} → {r.status_code}: {r.text[:400]}")
        if r.status_code == 204 or not (r.content or b"").strip():
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def engine(self) -> str:
        return "opencode"

    def oc_health(self) -> dict:
        """The engine's own health, through Studio's `_studio` namespace."""
        return self._json("GET", f"{PREFIX}/_studio/status")

    def directory(self) -> str:
        """Where the engine's tools resolve a relative path.

        Recorded per trial because it is not ~/work by default, and the
        mechanical grader resolves what a model wrote against ~/work. A model
        that writes `workflows/x/gen.py` lands somewhere different depending on
        this, which is a grading fact, not a model one.
        """
        return str(self._json("GET", f"{PREFIX}/path").get("directory") or "")

    def session(self, conversation_id: str, *, agent: str = "general",
                directory: str | None = None) -> str:
        """Create (once) the session this conversation runs in.

        `directory` is a **query parameter**, not a body field, and it roots the
        engine's file tools, AGENTS.md lookup and skill discovery. The chat panel
        passes the workflow's own folder for a workflow-scoped conversation and
        **nothing at all** for an unscoped one, which is what a user has when
        they ask for a workflow that does not exist yet — so the harness's
        default is no directory, because that is the posture the tasks describe.
        Sending it in the body, as the first version of this did, is silently
        ignored: the session roots at the server default and every relative path
        the model writes lands in $HOME.
        """
        if conversation_id not in self._sessions:
            body: dict[str, Any] = {"title": conversation_id}
            if agent and agent != "general":
                body["agent"] = agent
            path = f"{PREFIX}/session"
            if directory:
                path += f"?directory={urllib.parse.quote(directory, safe='')}"
            self._sessions[conversation_id] = self._call("POST", path, json=body)["id"]
        return self._sessions[conversation_id]

    # ── the turn ──────────────────────────────────────────────────────────

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
        session_dir: str | None = None,
    ) -> TurnResult:
        """One turn, to `session.idle`, in the same shape the builtin returns.

        Only the last message is sent: OpenCode keeps the conversation itself,
        so replaying the history the way `/api/chat/stream` wants would restate
        every earlier turn into a session that already has them.
        """
        if timeout_s == -1.0:
            timeout_s = TURN_TIMEOUT_S
        session_id = self.session(conversation_id, agent=agent, directory=session_dir)
        result = TurnResult(request_id=uuid.uuid4().hex)
        result.meta = {"session_id": session_id, "engine": "opencode",
                       "session_dir": session_dir or ""}

        events: queue.Queue = queue.Queue()
        stop_reading = threading.Event()

        def read_events() -> None:
            try:
                with httpx.Client(timeout=httpx.Timeout(None, connect=15.0)) as client:
                    with client.stream("GET", self._url(f"{PREFIX}/event")) as r:
                        if r.status_code >= 400:
                            events.put({"_harness": f"event stream → {r.status_code}"})
                            return
                        for frame in _sse(r):
                            if stop_reading.is_set():
                                return
                            events.put(frame)
            except Exception as e:                       # noqa: BLE001
                events.put({"_harness": f"{type(e).__name__}: {e}"})

        reader = threading.Thread(target=read_events, daemon=True)
        reader.start()
        # The stream has to be live before the prompt is admitted, and there is
        # no readiness signal short of `server.connected`. Wait for it, briefly.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                first = events.get(timeout=0.5)
            except queue.Empty:
                continue
            if first.get("type") == "server.connected":
                break
            events.put(first)
            break

        started = time.monotonic()
        prompt = messages[-1]["content"] if messages else ""
        body: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
        if model:
            body["model"] = {"providerID": provider or "", "modelID": model}
        if agent and agent != "general":
            body["agent"] = agent
        try:
            self._call("POST", f"{PREFIX}/session/{session_id}/prompt_async", json=body)
        except StudioError:
            stop_reading.set()
            raise

        tools: dict[str, dict] = {}       # part id -> the call as first seen
        roles: dict[str, str] = {}        # message id -> role
        try:
            while True:
                if timeout_s is not None and time.monotonic() - started > timeout_s:
                    result.timed_out = True
                    break
                try:
                    frame = events.get(timeout=1.0)
                except queue.Empty:
                    continue
                if "_harness" in frame:
                    result.error = frame["_harness"]
                    break
                if self._absorb(frame, session_id, result, tools, roles,
                                started, auto_approve):
                    break
        finally:
            stop_reading.set()

        result.duration_s = time.monotonic() - started
        if result.timed_out:
            # Same contract as the builtin: never leave a turn running, or the
            # next trial's C2/D5 guard refuses to start (4e63fa6).
            self.stop(result.request_id, session_id=session_id)
        return result

    def _absorb(self, frame: dict, session_id: str, result: TurnResult,
                tools: dict[str, dict], roles: dict[str, str],
                started: float, auto_approve: bool) -> bool:
        """Fold one event into the result. Returns True when the turn is over."""
        etype = frame.get("type") or ""
        props = frame.get("properties") or {}
        if props.get("sessionID") not in (None, session_id):
            return False                                  # another session's turn
        result.frames.append(frame)

        if etype == "message.updated":
            info = props.get("info") or {}
            if info.get("id"):
                roles[info["id"]] = info.get("role") or ""
        elif etype == "message.part.updated":
            part = props.get("part") or {}
            if part.get("sessionID") not in (None, session_id):
                return False
            ptype = part.get("type")
            if ptype == "text" and not part.get("synthetic"):
                # The prompt comes back as a text part on the *user* message, so
                # a filter on part type alone captures the task's own words and
                # reports them as the model's answer — which then reaches the
                # judge, and counts toward `narrated_not_written`. Roles arrive
                # on `message.updated`; a part whose message has not been seen
                # yet is held and resolved when it is.
                if result.ttft_s is None:
                    result.ttft_s = time.monotonic() - started
                _keep_text(tools, part)
                result.text = _assistant_text(tools, roles)
            elif ptype == "tool":
                self._absorb_tool(part, result, tools)
            elif ptype == "step-finish":
                # A turn is many steps, and this fires once per step. `update`
                # kept only the last one, which made `output` close to
                # meaningless as a cost figure: qwen-3.8-27b's 270-tool-call
                # t05a recorded 216 output tokens, the final step's alone.
                #
                # `input`/`output` keep that last-step meaning, because every
                # trial measured so far carries it and the per-step figures are
                # not retained anywhere — old rows cannot be recomputed, so
                # redefining these two would silently mix two quantities in one
                # column. The totals arrive beside them instead, NULL for every
                # row that predates this.
                #
                # Summing `input` across steps is what the provider bills, since
                # each step resends the conversation; `input_peak` is the
                # largest single context, which is what a context ceiling bites.
                usage = part.get("tokens") or {}
                inp = usage.get("input") or 0
                out = usage.get("output") or 0
                tok = result.meta.setdefault("tokens", {})
                tok["input"], tok["output"] = inp, out
                tok["input_total"] = tok.get("input_total", 0) + inp
                tok["output_total"] = tok.get("output_total", 0) + out
                tok["input_peak"] = max(tok.get("input_peak", 0), inp)
                tok["steps"] = tok.get("steps", 0) + 1
        elif etype == "permission.asked":
            result.permissions.append(props)
            # Always answered, whatever the posture. D2's "gate off" means the
            # model is not interrupted; on the builtin engine that was Studio
            # never asking, and there is no equivalent here — the engine asks on
            # its own and parks the turn until someone replies. Unattended that
            # is a hang, not a denial: one unanswered `external_directory`
            # prompt cost a whole trial the full 2400s ceiling after three tool
            # calls. The posture stays visible in the record as the number of
            # prompts raised, which is the thing the two postures actually
            # differ in here.
            #
            # `always` rather than `once` so a model reading six templates is
            # asked once, not six times.
            self.resolve_permission(result.request_id, props.get("id", ""),
                                    "approve" if auto_approve else "always")
        elif etype == "question.asked":
            # The builtin engine had no structured question — models asked in
            # prose and the simulated user answered. Recorded rather than
            # answered here, and rejected so the turn can finish: answering it
            # would put words in the user's mouth that the sim-user bank never
            # scripted, and leaving it pends the session forever.
            result.permissions.append({"question": props})
            try:
                self._call("POST", f"{PREFIX}/question/{props.get('id','')}/reject", json={})
            except StudioError:
                pass
        elif etype == "session.error":
            err = props.get("error") or {}
            result.error = (err.get("data") or {}).get("message") or err.get("name") or "error"
        elif etype == "session.idle":
            result.text = _assistant_text(tools, roles)
            return True
        result.text = _assistant_text(tools, roles)
        return False

    def _absorb_tool(self, part: dict, result: TurnResult, tools: dict[str, dict]) -> None:
        """Split one mutating tool part into a call and, later, its result."""
        pid = part.get("id") or part.get("callID") or ""
        state = part.get("state") or {}
        name = part.get("tool") or "?"
        if pid not in tools:
            call = {"id": part.get("callID") or pid, "name": name,
                    "arguments": state.get("input") or {}}
            tools[pid] = call
            result.tool_calls.append(call)
        else:
            # Arguments stream in: a pending part can carry none and the running
            # one carry them all, so the call is corrected in place.
            if state.get("input"):
                tools[pid]["arguments"] = state["input"]
        if state.get("status") == "completed" and pid in tools:
            # Evidence it exists. OpenCode's tool set is open — built-ins, plus
            # `skill`, plus whatever the three configured MCP servers expose —
            # so a fixed list of known names will always be short, and calling a
            # dispatched tool "unknown" reports a protocol failure that did not
            # happen.
            tools[pid]["dispatched"] = True
        if state.get("status") in _DONE and not part.get("_recorded"):
            part["_recorded"] = True
            out = state.get("output")
            if state.get("status") == "error":
                out = f"Error: {state.get('error')}"
            if not any(r.get("_pid") == pid for r in result.tool_results):
                result.tool_results.append(
                    {"name": name, "result": out if out is not None else "", "_pid": pid})

    # ── the guards the sweep depends on ───────────────────────────────────

    def current_turn(self) -> dict:
        """C2/D5: is the engine busy? `/session/status` maps only busy sessions."""
        try:
            status = self._json("GET", f"{PREFIX}/session/status")
        except StudioError:
            return {"active": False}
        busy = [s for s, v in (status or {}).items() if v]
        return {"active": bool(busy), "turn_id": busy[0] if busy else None}

    def assert_container_free(self) -> None:
        """C2/D5, with the one difference this engine makes.

        The builtin engine had a single turn slot: a busy one meant another
        trial was running, and starting anyway would cancel it mid-sentence.
        OpenCode's sessions are per-conversation, so a busy session at the start
        of a trial is not a competing trial — it is an *orphan*, a turn whose
        client died and left the engine working. This session saw one: a trial
        killed mid-turn left its session busy for the full 2400s ceiling, and
        the next trial would have refused to start because of it.

        Under D5 there is never a second trial against one container, so a busy
        session here is stale by definition. It is aborted and named rather than
        being allowed to block the run — and if one somehow will not clear, the
        assertion below still refuses to start.
        """
        status = {}
        try:
            status = self._json("GET", f"{PREFIX}/session/status") or {}
        except StudioError:
            pass
        for session_id, state in status.items():
            print(f"[opencode] aborting stale session {session_id} ({state})", flush=True)
            try:
                self._call("POST", f"{PREFIX}/session/{session_id}/abort", json={})
            except StudioError:
                pass
        if status:
            time.sleep(2.0)
        super().assert_container_free()

    def assert_turn_was_ours(self, conversation_id: str) -> None:
        """No-op: OpenCode sessions are per-conversation by construction.

        The builtin engine had one shared turn slot per container, so a second
        trial cancelled the first mid-sentence and the victim graded as a model
        failure. Sessions are separate here, which removes the failure rather
        than detecting it — but `current_turn` still guards the container,
        because two trials at once would still contend for one workspace.
        """

    def resolve_permission(self, request_id: str, call_id: str, decision: str) -> dict:
        reply = {"approve": "once", "always": "always"}.get(decision, "reject")
        return self._call("POST", f"{PREFIX}/permission/{call_id}/reply",
                          json={"reply": reply}) or {}

    # Every gate the engine has, from its own `PermissionConfig` schema. Listed
    # rather than set with the bare `"permission": "allow"` shorthand: a key the
    # harness has not heard of should stay at the engine's default and show up
    # as a prompt, not be silently waved through by a wildcard.
    PERMISSIONS = ("read", "edit", "glob", "grep", "list", "bash", "task",
                   "external_directory", "todowrite", "question", "webfetch",
                   "websearch", "lsp", "doom_loop", "skill")

    def set_command_approval(self, require: bool) -> bool:
        """D2's gate, enforced rather than reported.

        On the builtin engine "gate off" meant Studio never asked. OpenCode asks
        on its own, so the equivalent is its `permission` block: every gate set
        to `allow` and the engine never stops. Answering prompts as they arrive
        (which the client also does, as a backstop) is not the same posture — it
        is "asked and approved", and it costs a round trip per prompt in the
        middle of a turn. One unanswered `external_directory` prompt on the file
        `pegasus-scaffold` tells models to read cost a whole trial its 2400s
        ceiling.

        The config is read once at startup and no endpoint reloads it, so this
        writes the file and restarts the engine.
        """
        want = "ask" if require else "allow"
        config = os.path.join(self.directory_of("config"), "opencode.json")
        try:
            with open(config) as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise StudioError(f"cannot read {config}: {e}") from e

        block = {k: want for k in self.PERMISSIONS}
        if doc.get("permission") == block:
            return require                       # already in this posture
        doc["permission"] = block
        tmp = f"{config}.harness"
        with open(tmp, "w") as fh:
            json.dump(doc, fh, indent=2)
        os.replace(tmp, config)                  # atomic: the engine reads this
        self.restart_engine()
        return require

    def directory_of(self, key: str) -> str:
        return str(self._json("GET", f"{PREFIX}/path").get(key) or "")

    def restart_engine(self, wait_s: float = 90.0) -> None:
        """Restart the agent server and wait for it to answer again."""
        self._call("POST", f"{PREFIX}/_studio/restart", json={})
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            time.sleep(2.0)
            try:
                if self.oc_health().get("running"):
                    self._sessions.clear()       # sessions do not survive it
                    return
            except StudioError:
                continue
        raise StudioError("the engine did not come back after a restart")

    def stop(self, request_id: str, verify_s: float = 30.0,
             session_id: str | None = None) -> bool:
        if session_id:
            try:
                self._call("POST", f"{PREFIX}/session/{session_id}/abort", json={})
            except StudioError:
                pass
        deadline = time.monotonic() + verify_s
        while time.monotonic() < deadline:
            if not self.current_turn().get("active"):
                return True
            time.sleep(1.0)
        return not self.current_turn().get("active")


def _keep_text(tools: dict, part: dict) -> None:
    """Text parts are cumulative per id, not deltas — the newest of each wins."""
    tools.setdefault("_text", {})[part.get("id")] = (
        part.get("messageID") or "", part.get("text") or "")


def _assistant_text(tools: dict, roles: dict[str, str]) -> str:
    """Every assistant text part, newest of each id, in arrival order.

    A part whose message role is not yet known is kept: `message.updated` for an
    assistant message can arrive after its first part, and dropping it there
    would lose the opening sentence of every turn.
    """
    return "".join(text for msg, text in (tools.get("_text") or {}).values()
                   if roles.get(msg, "assistant") != "user")
