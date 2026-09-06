"""Private, bounded stdio reference controller for native Codex.

Only initialization, tool listing and allowlisted calls can reach upstream.
Resource helpers are denied here even when the native client advertises them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import adapters
from .mcp_proxy import MAX_BYTES, Bridge


def normalized_tools(tools: Any, allowed: list[str]) -> list[dict]:
    if not isinstance(tools, list):
        raise TypeError("invalid reference tool listing")
    indexed = {}
    for tool in tools:
        if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                or not isinstance(tool.get("inputSchema"), dict) or tool["name"] in indexed):
            raise ValueError("invalid or duplicate reference tool schema")
        indexed[tool["name"]] = tool
    if not set(allowed) <= indexed.keys():
        raise ValueError("required reference tool missing")
    # Descriptions and annotations are model-visible and therefore hash-bound too.
    return [indexed[name] for name in allowed]


class ReferenceBridge(Bridge):
    def __init__(self, url: str, allowed: list[str], *, timeout: float, max_tool_calls: int,
                 expected_tools: list[dict] | None = None, expected_server: str | None = None,
                 journal: Path | None = None):
        if type(max_tool_calls) is not int or max_tool_calls < 1:
            raise ValueError("invalid reference call cap")
        super().__init__(url, allowed, timeout, max_tool_calls)
        self.expected_tools = expected_tools
        self.expected_server = expected_server
        self.journal = journal
        self.ready = False
        self.initialized = False
        self.observed_tools: list[dict] = []
        self.server_sha256: str | None = None

    def record(self, event: dict) -> None:
        if self.journal is not None:
            with self.journal.open("a", encoding="utf-8") as stream:
                stream.write(adapters.canonical(event) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def request(self, method: str, params: dict, ident: int | str | None = 1) -> dict:
        result = super().request(method, params, ident)
        if ident is not None and (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                                 or type(result.get("id")) is not type(ident) or result.get("id") != ident
                                 or ("result" in result) == ("error" in result)):
            raise ValueError("invalid reference response envelope")
        return result

    def handle(self, message: dict) -> dict | None:
        ident, method, params = message.get("id"), message.get("method"), message.get("params", {})
        if (message.get("jsonrpc") != "2.0" or not isinstance(params, dict)
                or (ident is not None and (type(ident) not in (str, int)))):
            raise ValueError("invalid reference request envelope")
        if method not in {"initialize", "notifications/initialized", "ping", "tools/list", "tools/call"}:
            self.record({"event": "rejected", "method": method, "forwarded": False})
            return None if ident is None else self.reject(ident, "Method not permitted")
        if method == "initialize":
            if self.initialized:
                raise ValueError("reference initialization repeated")
            result = self.request(method, params, ident)
            body = result.get("result")
            if not isinstance(body, dict) or not isinstance(body.get("protocolVersion"), str):
                raise ValueError("reference initialization failed")
            info = body.get("serverInfo")
            if not isinstance(info, dict) or not all(isinstance(info.get(k), str) for k in ("name", "version")):
                raise ValueError("reference server identity missing")
            self.server_sha256 = adapters.digest(info)
            if self.expected_server is not None and self.server_sha256 != self.expected_server:
                raise ValueError("reference server identity drift")
            self.headers["MCP-Protocol-Version"] = body["protocolVersion"]
            self.initialized = True
            return {"jsonrpc": "2.0", "id": ident, "result": {
                "protocolVersion": body["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": "exam-reference-filter", "version": "1"},
            }}
        if not self.initialized:
            raise ValueError("reference session not initialized")
        if method == "tools/list":
            self.ready = False
            result = self.request(method, {}, ident)
            body = result.get("result", {})
            if not isinstance(body, dict) or body.get("nextCursor"):
                raise ValueError("reference listing incomplete")
            self.observed_tools = normalized_tools(body.get("tools"), self.allowed)
            if self.expected_tools is not None and self.observed_tools != self.expected_tools:
                raise ValueError("reference tool schema drift")
            self.ready = True
            self.record({"event": "ready", "tools_sha256": adapters.digest(self.observed_tools),
                         "server_sha256": self.server_sha256})
            return {"jsonrpc": "2.0", "id": ident, "result": {"tools": self.observed_tools}}
        if method == "tools/call":
            if ident is None:
                return None  # Notifications must never execute a lookup.
            name = params.get("name")
            if not self.ready or name not in self.allowed or not isinstance(params.get("arguments", {}), dict):
                self.record({"event": "rejected", "method": method, "forwarded": False})
                return self.reject(ident, "Tool not permitted")
            if self.calls >= self.max_tool_calls:
                self.record({"event": "rejected", "method": method, "reason": "call_cap", "forwarded": False,
                             "tool": name, "arguments_sha256": adapters.digest(params.get("arguments", {}))})
                return self.reject(ident, "Reference call limit reached")
            self.calls += 1  # Charge before network I/O; failures consume the cap.
            self.record({"event": "call", "index": self.calls, "tool": name,
                         "arguments_sha256": adapters.digest(params.get("arguments", {}))})
            try:
                result = self.request(method, params, ident)
                body = result.get("result")
                if not isinstance(body, dict) or not isinstance(body.get("content"), list):
                    raise TypeError("invalid reference result")
            except Exception:  # noqa: BLE001 - raw upstream diagnostics stay private
                body = {"isError": True, "content": [{"type": "text", "text": "Reference lookup failed"}]}
            self.record({"event": "result", "index": self.calls, "result_sha256": adapters.digest(body)})
            return {"jsonrpc": "2.0", "id": ident, "result": body}
        if method == "notifications/initialized":
            self.request(method, {}, None)
            return None
        return None if ident is None else {"jsonrpc": "2.0", "id": ident, "result": {}}

    @staticmethod
    def reject(ident: str | int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": message}}


def snapshot(url: str, allowed: list[str], timeout: int, cap: int) -> tuple[list[dict], str]:
    bridge = ReferenceBridge(url, allowed, timeout=timeout, max_tool_calls=cap)
    bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "exam-reference-filter", "version": "1"},
    }})
    bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert bridge.server_sha256 is not None
    return bridge.observed_tools, bridge.server_sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = adapters._strict_json_loads(args.config.read_text())
        journal = Path(config["journal"])
        with journal.open("x", encoding="utf-8"):
            pass
        journal.chmod(0o600)
        bridge = ReferenceBridge(config["url"], config["tools"], timeout=config["timeout"],
                                 max_tool_calls=config["cap"], expected_tools=config["schemas"],
                                 expected_server=config["server_sha256"], journal=journal)
        while True:
            line = sys.stdin.buffer.readline(MAX_BYTES + 1)
            if not line:
                return 0
            if len(line) > MAX_BYTES:
                raise ValueError("reference request exceeds limit")
            message = adapters._strict_json_loads(line.decode("utf-8"))
            if not isinstance(message, dict):
                raise TypeError("invalid reference request")
            response = bridge.handle(message)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
    except Exception:  # noqa: BLE001 - fail closed; never emit endpoint/credential diagnostics
        print("Native reference controller failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
