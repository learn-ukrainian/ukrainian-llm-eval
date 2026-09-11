"""Native AGY PreToolUse gate; invoked as a file in an isolated child home."""
from __future__ import annotations

import fcntl
import json
import sys
import time
from pathlib import Path
from typing import Any


def decide(call: Any, controls: dict[str, Any], count: int) -> tuple[bool, bool]:
    """Return (allowed, reference) without executing a candidate tool."""
    if not isinstance(call, dict) or time.monotonic() >= controls["deadline"]:
        return False, False
    name, args = call.get("name"), call.get("args", {})
    if not isinstance(args, dict):
        return False, False
    if name == "finish":
        return True, False
    reference = (name == "call_mcp_tool" and args.get("ServerName") == "sources"
                 and args.get("ToolName") in controls["tools"] and isinstance(args.get("Arguments"), dict))
    return reference and count < controls["max_tool_calls"], reference


def main() -> int:
    try:
        path = Path(sys.argv[1])
        controls = json.loads(path.read_text())
        payload = json.loads(sys.stdin.read(2_000_001))
        call = payload.get("toolCall")
        with path.with_suffix(".state").open("a+", encoding="utf-8") as state:
            fcntl.flock(state, fcntl.LOCK_EX)
            state.seek(0)
            count = int(state.read() or "0")
            allowed, reference = decide(call, controls, count)
            if allowed and reference:
                state.seek(0)
                state.truncate()
                state.write(str(count + 1))
                state.flush()
            receipt = {"call": call, "decision": "allow" if allowed else "deny", "count_before": count}
            with path.with_suffix(".jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(receipt, ensure_ascii=False, allow_nan=False) + "\n")
            print(json.dumps({"decision": receipt["decision"], "reason": "Evaluator reference allowlist and call cap"}))
        return 0
    except Exception:  # noqa: BLE001 -- malformed input must never grant permission
        print('{"decision":"deny","reason":"Evaluator gate failed"}')
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
