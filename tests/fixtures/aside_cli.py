"""External Aside CLI test double, using the observed CLI event contract.

Run as a real child process so timeout, cancellation and ownership checks
exercise OS pipes and session cleanup rather than mocked project modules.
"""
import json
import os
from pathlib import Path
import sys
import time

root = Path(__file__).parent
scenario = json.loads((root / "scenario.json").read_text())
args = sys.argv[1:]


def record(value):
    with (root / "calls.jsonl").open("a") as stream:
        stream.write(json.dumps(value) + "\n")


if args[0] == "session":
    action, session_id = args[1:]
    record({"action": action, "session_id": session_id})
    if action == "delete":
        time.sleep(scenario.get("delete_delay", 0))
        if scenario.get("delete_fails"):
            sys.exit(1)
        (root / (session_id + ".active")).unlink(missing_ok=True)
    sys.exit(0)

session_id = "fake-" + str(os.getpid())
(root / (session_id + ".active")).write_text(str(os.getpid()))
print("\x1b[2mcreated new session: " + session_id + "\x1b[0m", file=sys.stderr, flush=True)
prompt = sys.stdin.read()
record({"action": "exec", "prompt": prompt, "pid": os.getpid()})
path = Path(args[args.index("--log-dump") + 1])
if scenario.get("hang"):
    while True:
        time.sleep(0.1)
if scenario.get("flood"):
    path.write_bytes(b"x" * (9 * 1024 * 1024))
    while True:
        time.sleep(0.1)
if scenario.get("malformed"):
    path.write_text("not json\n")
    sys.exit(0)

if "events" in scenario:
    events = scenario["events"]
else:
    listing = "# List Prompt" in prompt
    payload = {
        "items": scenario.get("list_items" if listing else "detail_items", []),
        "errors": [],
        "failure_reason": None,
    }
    if scenario.get("login_required"):
        payload = {"items": [], "errors": ["Login required: sign in in Aside"],
                   "failure_reason": "login_required"}
    events = [{"type": "message_end", "message": {
        "role": "assistant", "stopReason": "stop",
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }}, {"type": "agent_end"}]
with path.open("w") as stream:
    for event in events:
        stream.write(json.dumps(event) + "\n")
# A transcript can contain perfectly valid, unrelated JSON. It is never the result.
print('{"items":[{"url":"https://wrong.example/transcript"}]}', flush=True)
sys.exit(scenario.get("exit_code", 0))
