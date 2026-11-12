"""Shared cross-engine contract fixtures evaluated against the Python engine.

``contracts/cases`` holds engine-neutral cases; ``contracts/cases/README.md`` documents their format. This
module loads them, drives the Python engine through its public interfaces, and checks the same expectations
as the native runner in ``native/tests/contracts.cpp``. Engine-specific code is confined to
:class:`PythonEngine`.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import secrets
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from switchboard.actions import ActionStateError, args_digest
from switchboard.app import App
from switchboard.errors import ToolError, UncertainOutcomeError
from switchboard.jsonschema import check_schema, validate
from switchboard.mcp_server import McpServer
from switchboard.policy import Origin, Tier
from switchboard.tools import Tool, ToolRegistry
from switchboard.util import canonical_json, iso
from tests.conftest import AppFactory

pytestmark = pytest.mark.contracts

ENGINE = "python"
ENGINES = frozenset({"python", "native"})
CASES_DIR = Path(__file__).resolve().parents[2] / "contracts" / "cases"
TOOLS_FILE = CASES_DIR / "synthetic-tools.json"
CONTRACT_README = CASES_DIR.parent / "README.md"
CASE_FORMAT = "switchboard-contract-cases"
TOOLS_FORMAT = "switchboard-contract-tools"
FORMAT_VERSION = 1
PROTOCOL_META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
STATUSES = frozenset({"ok", "pending", "denied", "invalid", "error"})
STATES = frozenset(
    {"in_flight", "succeeded", "failed", "uncertain", "reconciled_applied", "reconciled_not_applied"}
)
ACTION_ID = re.compile(r"[0-9a-f]{32}")
OPS = frozenset(
    {
        "call",
        "approve",
        "deny",
        "reconcile",
        "action_state",
        "recent_actions",
        "handler_calls",
        "kill_switch",
        "restart",
        "tick",
        "insert_stale_action",
        "rpc",
        "check_schema",
        "validate",
        "register",
    }
)
ENVELOPE_KEYS = frozenset(
    {
        "status",
        "tool",
        "action",
        "approval_id",
        "absent",
        "data",
        "instants",
        "patterns",
        "types",
        "message_contains",
    }
)
LISTING_KEYS = frozenset({"listed", "not_listed", "listed_count", "all_listed_match", "none_listed_match"})
RPC_KEYS = LISTING_KEYS | {"error_code", "is_error", "structured_content", "outcome"}
ENTRY_KEYS = frozenset({"entries", "count", "present_fields", "absent_fields"})
MISSING = object()


# Loading


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)


def case_files() -> list[Path]:
    return sorted(path for path in CASES_DIR.glob("*.json") if path.name != TOOLS_FILE.name)


def load_document(path: Path) -> dict[str, Any]:
    """A case file, refused unless its format, version, identifiers, operations and markers are known."""
    document = load_json(path)
    if not isinstance(document, dict) or document.get("format") != CASE_FORMAT:
        raise ValueError(f"{path.name}: not a {CASE_FORMAT} file")
    if document.get("version") != FORMAT_VERSION:
        raise ValueError(f"{path.name}: fixture format version {document.get('version')!r} is not supported")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path.name}: expected a non-empty list of cases")
    ids = [case.get("id") for case in cases]
    if not all(isinstance(name, str) and name for name in ids) or len(set(ids)) != len(ids):
        raise ValueError(f"{path.name}: every case needs a unique, non-empty id")
    for case in cases:
        if set(case.get("known_violation", {})) - ENGINES:
            raise ValueError(f"{path.name}/{case['id']}: known_violation names an unknown engine")
        steps = case.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"{path.name}/{case['id']}: expected a non-empty list of steps")
        for step in steps:
            if step.get("op") not in OPS:
                raise ValueError(f"{path.name}/{case['id']}: unknown operation {step.get('op')!r}")
            divergence = step.get("divergence")
            if divergence is not None and set(divergence) != {"topic", "reason"} | ENGINES:
                raise ValueError(
                    f"{path.name}/{case['id']}: divergence needs topic, reason, python and native"
                )
    return document


def case_params() -> list[Any]:
    params: list[Any] = []
    for path in case_files():
        for case in load_document(path)["cases"]:
            violation = case.get("known_violation", {}).get(ENGINE)
            marks = (
                [pytest.mark.xfail(strict=True, reason=f"known violation: {violation}")] if violation else []
            )
            params.append(pytest.param(case, id=f"{path.stem}/{case['id']}", marks=marks))
    return params


def documented_topics() -> set[str]:
    """Topics in the contract's Known divergences table, without a parenthesized fixture file name."""
    lines = CONTRACT_README.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Known divergences")
    topics: set[str] = set()
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("|"):
            cell = line.strip().strip("|").split("|")[0].strip()
            if cell and set(cell) != {"-"} and cell != "Topic":
                topics.add(cell.split(" (")[0])
    return topics


TOOLS: dict[str, Any] = load_json(TOOLS_FILE)


# Comparison


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def shown(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    return text if len(text) <= 2000 else text[:2000] + "..."


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def has_type(value: Any, name: str) -> bool:
    actual = json_type(value)
    return actual == name or (name == "number" and actual == "integer")


def same_value(expected: Any, actual: Any) -> bool:
    """JSON scalar equality: booleans never equal numbers, while 1 equals 1.0."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return expected == actual
    return json_type(expected) == json_type(actual) and bool(expected == actual)


def subset(expected: Any, actual: Any) -> bool:
    """Listed object keys must match; arrays must have the same length and match item by item."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and subset(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(subset(want, have) for want, have in zip(expected, actual, strict=True))
        )
    return same_value(expected, actual)


def match_unordered(expected: Sequence[Any], actual: Sequence[Any]) -> bool:
    """One-to-one subset match in any order."""
    if len(expected) != len(actual):
        return False
    if not expected:
        return True
    head, rest = expected[0], expected[1:]
    return any(
        subset(head, item) and match_unordered(rest, [*actual[:index], *actual[index + 1 :]])
        for index, item in enumerate(actual)
    )


def lookup(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return MISSING
    return current


def instant(text: Any) -> float:
    """Seconds since the epoch for a timestamp with an offset, however the offset is written."""
    check(isinstance(text, str), f"expected a timestamp, got {text!r}")
    moment = datetime.fromisoformat(text)
    check(moment.tzinfo is not None, f"timestamp {text!r} has no offset")
    return moment.timestamp()


def only(expected: Mapping[str, Any], allowed: frozenset[str] | set[str]) -> None:
    unknown = set(expected) - set(allowed)
    check(not unknown, f"unknown expectation(s) {sorted(unknown)}")


def check_paths(value: Any, expected: Mapping[str, Any]) -> None:
    for path, want in expected.get("instants", {}).items():
        have = lookup(value, path)
        check(have is not MISSING, f"{path} is missing in {shown(value)}")
        if want is None:
            check(have is None, f"expected {path} to be null in {shown(value)}")
        else:
            check(
                have is not None and instant(have) == instant(want),
                f"expected {path} at {want} in {shown(value)}",
            )
    for path, pattern in expected.get("patterns", {}).items():
        have = lookup(value, path)
        check(
            isinstance(have, str) and re.fullmatch(pattern, have) is not None,
            f"expected {path} to match {pattern!r} in {shown(value)}",
        )
    for path, name in expected.get("types", {}).items():
        have = lookup(value, path)
        check(have is not MISSING and has_type(have, name), f"expected {path} to be {name} in {shown(value)}")


def check_envelope(envelope: Any, expected: Mapping[str, Any]) -> None:
    """The contract's outcome envelope, plus the step's expectations."""
    only(expected, ENVELOPE_KEYS)
    check(isinstance(envelope, dict), f"expected an outcome envelope, got {shown(envelope)}")
    text = shown(envelope)
    check(
        envelope.get("status") in STATUSES and isinstance(envelope.get("tool"), str),
        f"malformed envelope {text}",
    )
    action = envelope.get("action")
    if "action" in envelope:
        check(
            isinstance(action, dict)
            and set(action) == {"id", "state"}
            and isinstance(action["id"], str)
            and ACTION_ID.fullmatch(action["id"]) is not None
            and action["state"] in STATES,
            f"malformed action in {text}",
        )
    for key in ("status", "tool"):
        if key in expected:
            check(envelope.get(key) == expected[key], f"expected {key} {expected[key]!r} in {text}")
    if "action" in expected:
        if expected["action"] is None:
            check("action" not in envelope, f"expected no action in {text}")
        else:
            check(
                isinstance(action, dict) and action.get("state") == expected["action"],
                f"expected action state {expected['action']!r} in {text}",
            )
    if "approval_id" in expected:
        check(
            ("approval_id" in envelope) == expected["approval_id"], f"approval_id presence differs in {text}"
        )
    for key in expected.get("absent", []):
        check(key not in envelope, f"expected no {key} in {text}")
    if "data" in expected:
        check(
            "data" in envelope and subset(expected["data"], envelope["data"]),
            f"expected data {shown(expected['data'])} in {text}",
        )
    check_paths(envelope, expected)
    message = envelope.get("message", "")
    for part in expected.get("message_contains", []):
        check(
            isinstance(part, str) and part in str(message),
            f"expected the message to contain {part!r} in {text}",
        )


def check_rpc(reply: Any, expected: Mapping[str, Any]) -> None:
    only(expected, RPC_KEYS)
    text = shown(reply)
    check(isinstance(reply, dict) and reply.get("jsonrpc") == "2.0", f"malformed protocol reply {text}")
    if "error_code" in expected:
        check(
            "result" not in reply and same_value(expected["error_code"], lookup(reply, "error.code")),
            f"expected protocol error {expected['error_code']} in {text}",
        )
        return
    result = reply.get("result")
    check("error" not in reply and isinstance(result, dict), f"expected a protocol result in {text}")
    if "is_error" in expected:
        check(
            result.get("isError") is expected["is_error"],
            f"expected isError {expected['is_error']} in {text}",
        )
    if "structured_content" in expected:
        present = "structuredContent" in result
        check(present == expected["structured_content"], f"structuredContent presence differs in {text}")
    if "outcome" in expected:
        content = lookup(result, "content.0.text")
        check(isinstance(content, str), f"expected text content in {text}")
        check_envelope(json.loads(content), expected["outcome"])
    if LISTING_KEYS & set(expected):
        tools = result.get("tools")
        check(isinstance(tools, list), f"expected a tool listing in {text}")
        names = [tool.get("name") for tool in tools]
        for name in expected.get("listed", []):
            check(name in names, f"expected {name} to be listed in {names}")
        for name in expected.get("not_listed", []):
            check(name not in names, f"expected {name} not to be listed in {names}")
        if "listed_count" in expected:
            check(
                len(names) == expected["listed_count"],
                f"expected {expected['listed_count']} tools in {names}",
            )
        for name in names:
            if "all_listed_match" in expected:
                matched = any(fnmatch.fnmatchcase(name, pattern) for pattern in expected["all_listed_match"])
                check(matched, f"{name} matches none of {expected['all_listed_match']}")
            for pattern in expected.get("none_listed_match", []):
                check(not fnmatch.fnmatchcase(name, pattern), f"{name} must not match {pattern}")


def check_entries(entries: list[Any], expected: Mapping[str, Any]) -> None:
    only(expected, ENTRY_KEYS)
    text = shown(entries)
    if "entries" in expected:
        check(
            match_unordered(expected["entries"], entries),
            f"expected entries {shown(expected['entries'])} in {text}",
        )
    if "count" in expected:
        check(len(entries) == expected["count"], f"expected {expected['count']} entries in {text}")
    for entry in entries:
        check(isinstance(entry, dict), f"malformed entry in {text}")
        for field in expected.get("present_fields", []):
            check(field in entry, f"expected {field} in every entry of {text}")
        for field in expected.get("absent_fields", []):
            check(field not in entry, f"expected no {field} in any entry of {text}")


def schema_accepted(schema: Any) -> bool:
    try:
        check_schema(schema)
    except ValueError:
        return False
    return True


def registration_accepted(name: str, schema: Any) -> bool:
    try:
        ToolRegistry().add(Tool(name, "Contract probe.", Tier.READ, schema, lambda _args: {}))
    except ValueError:
        return False
    return True


# The engine


class PythonEngine:
    """The Python reference engine behind the neutral step model."""

    def __init__(self, make_app: AppFactory, tools: Mapping[str, Any]) -> None:
        self._make_app = make_app
        self._tools = tools
        self._app: App | None = None
        self._server: McpServer | None = None
        self.calls: Counter[str] = Counter()

    @property
    def app(self) -> App:
        if self._app is None:
            raise AssertionError("the engine has not started")
        return self._app

    def constant(self, name: str) -> Any:
        if name == "absent_approval_id":
            return "0" * 8
        raise AssertionError(f"unknown engine constant {name!r}")

    def start(self, config: Mapping[str, Any]) -> None:
        """Build an app on the case's data directory (reopened on restart) and add the synthetic tools."""
        app = self._make_app(copy.deepcopy(dict(config)))
        for spec in self._tools["tools"]:
            app.registry.add(
                Tool(
                    spec["name"],
                    f"Contract synthetic {spec['tier']} tool.",
                    Tier(spec["tier"]),
                    copy.deepcopy(self._tools["schema"]),
                    self._handler(spec["name"]),
                    connector="contract",
                )
            )
        self._app = app
        self._server = McpServer(
            tools=lambda: app.registry.exposed(app.ctx.config.mcp_exposure()), call=app.dispatcher.call
        )

    def _handler(self, name: str) -> Callable[[dict[str, Any]], Any]:
        modes: Mapping[str, Any] = self._tools["modes"]

        def handle(args: dict[str, Any]) -> Any:
            self.calls[name] += 1
            mode = modes[args["mode"]]
            kind = mode.get("raises")
            if kind == "failed":
                raise ToolError("the contract provider refused the change before applying it")
            if kind == "uncertain":
                raise UncertainOutcomeError("the contract change may have been applied")
            if kind == "unexpected":
                raise RuntimeError("contract handler failed unexpectedly")
            return copy.deepcopy(mode["returns"])

        return handle

    def call(self, tool: str, args: Mapping[str, Any], origin: str) -> dict[str, Any]:
        return self.app.call(tool, dict(args), Origin(origin)).to_dict()

    def approve(self, approval_id: str) -> dict[str, Any]:
        return self.app.dispatcher.approve(approval_id).to_dict()

    def deny(self, approval_id: str) -> bool:
        return self.app.dispatcher.deny(approval_id)

    def reconcile(self, action_id: str, resolution: str, note: str) -> str | None:
        """The state after an accepted decision, or None when the journal refused it."""
        check(resolution in {"applied", "not_applied"}, f"unknown resolution {resolution!r}")
        try:
            record = self.app.journal.reconcile(action_id, applied=resolution == "applied", note=note)
        except ActionStateError:
            return None
        return record.state

    def action_state(self, action_id: str) -> str | None:
        record = self.app.journal.get(action_id)
        return None if record is None else record.state

    def recent_actions(self, args: Mapping[str, Any]) -> tuple[dict[str, Any], Any]:
        outcome = self.call("actions_recent", args, "assistant")
        data = outcome.get("data")
        return outcome, data.get("actions") if isinstance(data, dict) else None

    def kill_switch(self, on: bool) -> None:
        path = self.app.policy.kill_switch
        if on:
            path.touch(mode=0o600, exist_ok=True)
        else:
            path.unlink(missing_ok=True)

    def tick(self) -> None:
        self.app.tick()

    def insert_stale_action(self, tool: str, args: Mapping[str, Any], started: int, deadline: int) -> str:
        """An in-flight entry as a process that ended mid-call leaves it, written directly to the journal."""
        action_id = secrets.token_hex(16)
        moment = datetime.now(UTC)
        with self.app.ctx.db.session() as conn:
            conn.execute(
                "INSERT INTO action_journal (id, tool, args_digest, args_json, origin, approval_id, state, "
                "owner, started_at, deadline_at) "
                "VALUES (?, ?, ?, ?, 'assistant', NULL, 'in_flight', ?, ?, ?)",
                (
                    action_id,
                    tool,
                    args_digest(args),
                    canonical_json(dict(args)),
                    "contract:ended-process",
                    iso(moment + timedelta(seconds=started)),
                    iso(moment + timedelta(seconds=deadline)),
                ),
            )
        return action_id

    def rpc(self, message: Mapping[str, Any]) -> Any:
        if self._server is None:
            raise AssertionError("the engine has not started")
        return self._server.handle(dict(message))


# The step loop, shared in shape with native/tests/contracts.cpp


class CaseRunner:
    def __init__(self, case: Mapping[str, Any], engine: PythonEngine) -> None:
        self.case = case
        self.engine = engine
        self.saved: dict[str, Any] = {}
        self.started = False
        self.request_id = 0

    def run(self) -> None:
        for number, step in enumerate(self.case["steps"], start=1):
            try:
                self.step(step)
            except Exception as exc:
                raise AssertionError(f"step {number} ({step['op']}): {exc}") from exc

    def expectation(self, step: Mapping[str, Any]) -> dict[str, Any]:
        expected = dict(step.get("expect", {}))
        divergence = step.get("divergence")
        if divergence is not None:
            expected.update(divergence[ENGINE])
        return expected

    def resolve(self, value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$saved"}:
                check(value["$saved"] in self.saved, f"nothing was saved as {value['$saved']!r}")
                return self.saved[value["$saved"]]
            if set(value) == {"$engine"}:
                return self.engine.constant(value["$engine"])
            return {key: self.resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        return value

    def save(self, step: Mapping[str, Any], result: Any) -> None:
        for name, path in step.get("save", {}).items():
            value = lookup(result, path)
            check(value is not MISSING, f"cannot save {path} from {shown(result)}")
            self.saved[name] = value

    def step(self, step: Mapping[str, Any]) -> None:
        op = step["op"]
        expected = self.expectation(step)
        if op == "check_schema":
            only(expected, {"valid", "invalid"})
            for schema in expected.get("valid", []):
                check(schema_accepted(schema), f"definition refused: {shown(schema)}")
            for schema in expected.get("invalid", []):
                check(not schema_accepted(schema), f"definition accepted: {shown(schema)}")
            return
        if op == "validate":
            only(expected, {"valid", "invalid"})
            schema = step["schema"]
            check(schema_accepted(schema), f"validate needs an accepted schema: {shown(schema)}")
            for value in expected.get("valid", []):
                check(not validate(value, schema), f"value refused: {shown(value)}")
            for value in expected.get("invalid", []):
                check(bool(validate(value, schema)), f"value accepted: {shown(value)}")
            return
        if op == "register":
            only(expected, {"accepted", "refused"})
            for item in expected.get("accepted", []):
                check(
                    registration_accepted(item["name"], item["schema"]),
                    f"registration refused: {shown(item)}",
                )
            for item in expected.get("refused", []):
                check(
                    not registration_accepted(item["name"], item["schema"]),
                    f"registration accepted: {shown(item)}",
                )
            return
        expected = self.resolve(expected)
        if op == "restart":
            self.engine.start(step.get("config", self.case.get("config", {})))
            self.started = True
            return
        if not self.started:
            self.engine.start(self.case.get("config", {}))
            self.started = True
        self.engine_step(op, step, expected)

    def engine_step(self, op: str, step: Mapping[str, Any], expected: dict[str, Any]) -> None:
        if op == "call":
            result = self.engine.call(
                step["tool"], self.resolve(step.get("args", {})), step.get("origin", "assistant")
            )
            check_envelope(result, expected)
            self.save(step, result)
        elif op == "approve":
            result = self.engine.approve(self.resolve(step["approval"]))
            check_envelope(result, expected)
            self.save(step, result)
        elif op == "deny":
            only(expected, {"denied"})
            denied = self.engine.deny(self.resolve(step["approval"]))
            check(denied is expected["denied"], f"expected denied {expected['denied']}, got {denied}")
        elif op == "reconcile":
            only(expected, {"accepted", "state"})
            state = self.engine.reconcile(self.resolve(step["action"]), step["resolution"], step["note"])
            check((state is not None) == expected["accepted"], f"expected accepted {expected['accepted']}")
            if "state" in expected:
                check(state == expected["state"], f"expected state {expected['state']!r}, got {state!r}")
        elif op == "action_state":
            only(expected, {"state"})
            state = self.engine.action_state(self.resolve(step["action"]))
            check(state == expected["state"], f"expected state {expected['state']!r}, got {state!r}")
        elif op == "recent_actions":
            outcome, entries = self.engine.recent_actions(self.resolve(step.get("args", {})))
            check(
                outcome.get("status") == "ok" and isinstance(entries, list),
                f"actions_recent: {shown(outcome)}",
            )
            check_entries(entries, expected)
        elif op == "handler_calls":
            for tool, count in expected.items():
                check(
                    self.engine.calls[tool] == count,
                    f"expected {count} {tool} runs, got {self.engine.calls[tool]}",
                )
        elif op == "kill_switch":
            self.engine.kill_switch(bool(step["on"]))
        elif op == "tick":
            self.engine.tick()
        elif op == "insert_stale_action":
            self.saved[step["save"]] = self.engine.insert_stale_action(
                step["tool"],
                self.resolve(step["args"]),
                int(step["started_offset_seconds"]),
                int(step["deadline_offset_seconds"]),
            )
        elif op == "rpc":
            self.request_id += 1
            params = dict(self.resolve(step.get("params", {})))
            params["_meta"] = dict(PROTOCOL_META)
            reply = self.engine.rpc(
                {"jsonrpc": "2.0", "id": self.request_id, "method": step["method"], "params": params}
            )
            check_rpc(reply, expected)
            self.save(step, reply)
        else:
            raise AssertionError(f"unknown operation {op!r}")


# Tests


@pytest.mark.parametrize("case", case_params())
def test_contract_case(case: dict[str, Any], make_app: AppFactory) -> None:
    CaseRunner(case, PythonEngine(make_app, TOOLS)).run()


def test_synthetic_tools_match_their_schema() -> None:
    assert TOOLS.get("format") == TOOLS_FORMAT
    assert TOOLS.get("version") == FORMAT_VERSION
    assert schema_accepted(TOOLS["schema"])
    assert TOOLS["schema"]["properties"]["mode"]["enum"] == list(TOOLS["modes"])
    for name, mode in TOOLS["modes"].items():
        assert (set(mode) == {"returns"} and isinstance(mode["returns"], dict)) or (
            set(mode) == {"raises"} and mode["raises"] in TOOLS["raises"]
        ), name
    assert {tool["tier"] for tool in TOOLS["tools"]} == {tier.value for tier in Tier}


def test_every_divergence_topic_is_documented() -> None:
    topics = documented_topics()
    used = {
        step["divergence"]["topic"]
        for path in case_files()
        for case in load_document(path)["cases"]
        for step in case["steps"]
        if "divergence" in step
    }
    assert used, "the fixtures demonstrate no divergence"
    assert used <= topics, f"add these topics to the Known divergences table: {sorted(used - topics)}"
