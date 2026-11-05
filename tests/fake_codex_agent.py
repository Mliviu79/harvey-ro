"""Scripted stand-in for the `codex` CLI as Harvey's agent drives it, used by
tests/test_agent_codex.py. No model, no network, no login is used.

Usage: python fake_codex_agent.py <scenario_file> <log_path> <codex args...>

Speaks the parts of Codex 0.159.3's app-server protocol (its generated JSON
schema: initialize, config/read, account/read, thread/start,
mcpServerStatus/list, hooks/list, turn/start, turn/interrupt; the
notifications item/started, item/agentMessage/delta, item/completed,
rawResponseItem/completed, rawResponse/completed, thread/tokenUsage/updated,
turn/completed) the agent's driver uses. The effective config it reports is
read from the private config.toml Harvey wrote, so Harvey's MCP server is
there as Harvey configured it. A Harvey tool call is recorded in the
database the way Harvey's MCP server records it (agent_tool_call), as the
Claude CLI's stand-ins do.

The scenario file holds one word, read at each step: answer (the default:
one tool call, then a streamed answer), silent ([SILENT]), task (the
answer file's text), slow (text until interrupted), loop (tool calls until
interrupted), shell (a command item), other_server (another MCP server's
tool), raw_exec (a raw call to exec_command), approval (a server request),
logout (a turn failed as unauthorized); shell_on (config/read reports the
shell tool on), extra_mcp (another MCP server configured), old_version.
Every request is logged to <log_path>, one JSON object a line.
"""

import json
import os
import select
import sys
import time
import tomllib

SCENARIO_FILE, LOG = sys.argv[1], sys.argv[2]
ARGS = sys.argv[3:]


def scenario():
    try:
        with open(SCENARIO_FILE, encoding="utf-8") as f:
            return f.read().strip() or "answer"
    except FileNotFoundError:
        return "answer"


def log(entry):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


VERSION = "0.158.0" if scenario() == "old_version" else "0.159.3"

if ARGS == ["--version"]:
    print(f"codex-cli {VERSION}")
    sys.exit(0)

if ARGS[:2] == ["debug", "models"]:
    sys.stdout.write(json.dumps({"models": [
        {"slug": "gpt-5.5", "visibility": "list", "priority": 1,
         "tool_mode": None, "experimental_supported_tools": []}]}))
    sys.exit(0)

assert ARGS[:1] == ["app-server"], ARGS
HOME = os.environ["CODEX_HOME"]
with open(os.path.join(HOME, "config.toml"), "rb") as f:
    TOML = tomllib.load(f)
HAS_AUTH = os.path.exists(os.path.join(HOME, "auth.json"))
SERVERS = TOML.get("mcp_servers") or {}
HARVEY = SERVERS.get("harvey") or {}
log({"cmd": "app-server", "has_auth": HAS_AUTH, "env_keys": sorted(os.environ),
     "mcp_servers": SERVERS})


def harvey_arg(name):
    args = HARVEY.get("args") or []
    return args[args.index(name) + 1] if name in args else None


def record(tool, arguments):
    """The call as Harvey's MCP server records it."""
    sys.path.insert(0, (HARVEY.get("env") or {})["PYTHONPATH"])
    from harvey.agent import tools
    task = harvey_arg("--task")
    tools.Tools(harvey_arg("--db"), harvey_arg("--chat"),
                None if task is None else int(task)).record(tool, arguments, "firms: 3", 7, "ok")


def effective_config():
    config = {
        "project_doc_max_bytes": 0, "include_environment_context": False,
        "include_permissions_instructions": False, "include_apps_instructions": False,
        "include_collaboration_mode_instructions": False, "web_search": "disabled",
        "forced_login_method": "chatgpt", "cli_auth_credentials_store": "file",
        "approval_policy": "never", "sandbox_mode": "read-only",
        "model_provider": None, "chatgpt_base_url": "https://chatgpt.com/backend-api/",
        "instructions": None, "developer_instructions": None, "openai_base_url": None,
        "notify": [], "hooks": None, "profile": None,
        "features": dict(TOML.get("features") or {}),
        "skills": {"include_instructions": False, "bundled": {"enabled": False}},
        "agents": {"enabled": False},
        "model_catalog_json": TOML.get("model_catalog_json"),
        # (As 0.159.3 reports it: what was written, and its defaults.)
        "mcp_servers": {name: {**entry, "environment_id": "local"}
                        for name, entry in SERVERS.items()},
    }
    if scenario() == "shell_on":
        config["features"]["shell_tool"] = True
    if scenario() == "extra_mcp":
        config["mcp_servers"]["files"] = {"command": "npx", "args": ["files-mcp"]}
    return config


def readline(timeout=None):
    if timeout is not None and not select.select([0], [], [], timeout)[0]:
        return None
    buf = b""
    while True:
        c = os.read(0, 1)
        if not c:
            return buf.decode() if buf else ""
        if c == b"\n":
            return buf.decode()
        buf += c


THREAD = "thr-agent"
TURNS = {"n": 0}
USAGE = {"total": {"inputTokens": 0, "cachedInputTokens": 0, "cacheWriteInputTokens": 0,
                   "outputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": 0}}


def note(method, **params):
    send({"method": method, "params": {"threadId": THREAD, **params}})


def tokens(turn, input_tokens, cached, output):
    total = USAGE["total"]
    total["inputTokens"] += input_tokens
    total["cachedInputTokens"] += cached
    total["outputTokens"] += output
    total["totalTokens"] += input_tokens + output
    last = {"inputTokens": input_tokens, "cachedInputTokens": cached,
            "cacheWriteInputTokens": 0, "outputTokens": output,
            "reasoningOutputTokens": 0, "totalTokens": input_tokens + output}
    note("thread/tokenUsage/updated", turnId=turn,
         tokenUsage={"last": last, "total": dict(total), "modelContextWindow": 272000})


def completed(turn, status="completed", error=None):
    note("turn/completed", turn={"id": turn, "items": [], "status": status, "error": error,
                                 "durationMs": 400})


def interrupted(turn, timeout):
    """An interrupt arrived (answered): the turn ends interrupted."""
    line = readline(timeout)
    if not line:
        return False
    msg = json.loads(line)
    log({"method": msg.get("method"), "params": msg.get("params")})
    if msg.get("method") == "turn/interrupt":
        send({"id": msg["id"], "result": {}})
        completed(turn, "interrupted")
        return True
    return False


def tool_call(turn, k):
    item = {"type": "mcpToolCall", "id": f"call-{k}", "server": "harvey", "tool": "counts",
            "status": "inProgress", "arguments": {"filter": {"county": "Cluj"}}}
    note("item/started", turnId=turn, item=item)
    send({"method": "rawResponseItem/completed", "params": {
        "threadId": THREAD, "turnId": turn,
        "item": {"type": "function_call", "name": "counts", "namespace": "mcp__harvey__",
                 "arguments": "{}", "call_id": f"call-{k}"}}})
    record("counts", item["arguments"])
    send({"method": "rawResponseItem/completed", "params": {
        "threadId": THREAD, "turnId": turn,
        "item": {"type": "function_call_output", "call_id": f"call-{k}", "output": "{}"}}})
    note("item/completed", turnId=turn, item={**item, "status": "completed",
                                               "result": {"content": []}})
    note("rawResponse/completed", turnId=turn)


def message(turn, item_id, pieces, phase="final_answer"):
    item = {"type": "agentMessage", "id": item_id, "text": "", "phase": phase}
    note("item/started", turnId=turn, item=item)
    for piece in pieces:
        note("item/agentMessage/delta", turnId=turn, itemId=item_id, delta=piece)
    note("item/completed", turnId=turn, item={**item, "text": "".join(pieces)})


def run_turn(turn, prompt):
    kind = scenario()
    TURNS["n"] += 1
    n = TURNS["n"]
    if kind == "shell":
        note("item/started", turnId=turn, item={"type": "commandExecution", "id": "c1",
                                                 "command": "rm -rf /", "commandActions": [],
                                                 "cwd": "/", "status": "inProgress"})
        return
    if kind == "other_server":
        note("item/started", turnId=turn, item={"type": "mcpToolCall", "id": "x1",
                                                 "server": "files", "tool": "read",
                                                 "status": "inProgress", "arguments": {}})
        return
    if kind == "raw_exec":
        send({"method": "rawResponseItem/completed", "params": {
            "threadId": THREAD, "turnId": turn,
            "item": {"type": "function_call", "name": "exec_command", "arguments": "{}"}}})
        return
    if kind == "approval":
        send({"id": "srv-1", "method": "mcpServer/elicitation/request",
              "params": {"threadId": THREAD, "turnId": turn, "serverName": "harvey",
                         "mode": "form", "message": "Allow?", "requestedSchema": {}}})
        return
    if kind == "logout":
        completed(turn, "failed", {"message": "unauthorized",
                                   "codexErrorInfo": "unauthorized"})
        return
    if kind == "slow":
        message_item = {"type": "agentMessage", "id": f"s{n}", "text": "", "phase": None}
        note("item/started", turnId=turn, item=message_item)
        for _ in range(400):
            note("item/agentMessage/delta", turnId=turn, itemId=f"s{n}", delta=".")
            if interrupted(turn, 0.05):
                return
        completed(turn)
        return
    if kind == "loop":
        k = 0
        while True:
            tool_call(turn, f"{n}-{k}")
            k += 1
            if interrupted(turn, 0.05):
                return
    tool_call(turn, f"{n}-0")
    tokens(turn, 900, 300, 20)
    if kind == "silent":
        pieces = ["[SILENT]"]
    elif kind == "task":
        with open(SCENARIO_FILE + ".answer", encoding="utf-8") as f:
            pieces = [f.read()]
    else:
        pieces = [f"Mesajul {n}: ", "trei firme."]
    message(turn, f"n{n}", ["Mă uit în date."], phase="commentary")
    message(turn, f"m{n}", pieces)
    note("rawResponse/completed", turnId=turn)
    tokens(turn, 1200, 1000, 40)
    completed(turn)


while True:
    line = readline()
    if not line:
        break
    msg = json.loads(line)
    method, params, mid = msg.get("method"), msg.get("params"), msg.get("id")
    if method is None:
        log({"response_to_server_request": msg})
        continue
    log({"method": method, "params": params})
    if mid is None:
        continue
    result = {}
    if method == "initialize":
        result = {"userAgent": f"harvey/{VERSION} (fake)", "codexHome": HOME}
    elif method == "config/read":
        result = {"config": effective_config(), "origins": {}}
    elif method == "account/read":
        result = {"account": {"type": "chatgpt", "email": "x@example.com", "planType": "plus"}
                  if HAS_AUTH else None, "requiresOpenaiAuth": True}
    elif method == "thread/start":
        result = {"thread": {"id": THREAD}, "model": params.get("model") or "gpt-5.5",
                  "modelProvider": "openai", "approvalPolicy": "never",
                  "sandbox": {"type": "readOnly", "networkAccess": False},
                  "instructionSources": [], "cwd": params.get("cwd")}
    elif method == "mcpServerStatus/list":
        data = [{"name": name, "tools": {"counts": {"name": "counts", "inputSchema": {}}},
                 "serverInfo": {"name": name, "version": "1"}, "authStatus": "unsupported",
                 "resources": [], "resourceTemplates": []} for name in SERVERS]
        result = {"data": data, "nextCursor": None}
    elif method == "hooks/list":
        result = {"data": [{"cwd": params["cwds"][0], "hooks": [], "warnings": [],
                            "errors": []}]}
    elif method == "turn/start":
        turn = f"turn-{TURNS['n'] + 1}"
        send({"id": mid, "result": {"turn": {"id": turn, "items": [], "status": "inProgress"}}})
        note("turn/started", turn={"id": turn, "items": [], "status": "inProgress"})
        prompt = "".join(i.get("text", "") for i in params.get("input", []) if isinstance(i, dict))
        log({"prompt": prompt})
        run_turn(turn, prompt)
        continue
    elif method == "turn/interrupt":
        result = {}
    send({"id": mid, "result": result})
    time.sleep(0)
