"""A stand-in for the Claude CLI's ``auth`` commands, shaped like what
2.1.318 printed with no login (the shapes only: fictional pages, codes and
accounts). Run as ``fake_claude_login.py SCENARIO LOG [claude's args]``.

``auth login``: needs a terminal; prints "Opening browser to sign in…",
then the page (an OSC 8 hyperlink and the address again), then "Paste code
here if prompted > ". A code with no "#" is "Invalid code" and it asks
again; GOOD_CODE signs in (a file of its own in CLAUDE_CONFIG_DIR) and
exits 0; any other code is "Login failed" and exit 1. ``auth status``
answers JSON from that file, ``auth logout`` removes it. Every call is a
JSON line in LOG: its args, its config folder, whether stdin is a terminal
it controls, and whether a credential reached it (never its value).
"""

import json
import os
import sys
import time

GOOD_CODE = "fictional-code#fictional-state"
QUERY = "code=true&client_id=fictional-client&state=QUERYSECRET"
PAGES = {"--claudeai": "https://claude.com/cai/oauth/authorize?" + QUERY,
         "--console": "https://platform.claude.com/oauth/authorize?" + QUERY}
CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
               "HARVEY_BRAIN_ANTHROPIC_KEY", "ANTHROPIC_BASE_URL")


def main() -> int:
    scenario, log_path, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    home = os.environ.get("CLAUDE_CONFIG_DIR", "")
    try:
        with open("/dev/tty"):
            ctty = True
    except OSError:
        ctty = False
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": args, "home": home, "tty": os.isatty(0), "ctty": ctty,
                            "credential": any(k in os.environ for k in CREDENTIALS),
                            "claude_mds_off": os.environ.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS"),
                            }) + "\n")
    signed = os.path.join(home, "fictional-login")
    if args[:1] == ["--version"]:
        print("2.1.318 (Claude Code)")
        return 0
    if args[:2] == ["auth", "status"]:
        if os.path.exists(signed):
            with open(signed, encoding="utf-8") as f:
                kind = f.read()
            print(json.dumps({"loggedIn": True, "authMethod":
                              "console" if kind == "--console" else "claude.ai",
                              "email": "someone@example.com", "orgName": "Acme Example",
                              "subscriptionType": "" if kind == "--console" else "max"}))
            return 0
        print(json.dumps({"loggedIn": False, "authMethod": "none"}))
        return 1
    if args[:2] == ["auth", "logout"]:
        if scenario == "logout_fails":
            return 1
        if os.path.exists(signed):
            os.remove(signed)
        print("Successfully logged out from your Anthropic account.")
        return 0
    if args[:2] != ["auth", "login"]:
        return 2
    if not os.isatty(0):
        print("Error: a terminal is needed to sign in", file=sys.stderr)
        return 2
    out = sys.stdout
    with open(os.path.join(home, ".claude.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    out.write("Opening browser to sign in…\r\n")
    out.flush()
    if scenario == "no_url":
        time.sleep(3600)
        return 1
    if scenario == "exit_early":
        return 1
    page = PAGES.get(args[2] if len(args) > 2 else "--claudeai", PAGES["--claudeai"])
    if scenario == "bad_url":
        page = "http://login.example.com/oauth/authorize?" + QUERY
    out.write(f"If the browser didn't open, visit: \x1b]8;;{page}\x07{page}\x1b]8;;\x07\r\n"
              "Paste code here if prompted > ")
    out.flush()
    if scenario == "chatty":
        # (A terminal UI drawing itself again and again: more output than
        # Harvey keeps before any code is asked for.)
        out.write(("\x1b[2K\rWaiting for the sign-in… " + "." * 60 + "\r\n") * 1200)
        out.write("Paste code here if prompted > ")
        out.flush()
    asked_again = False
    while True:
        line = sys.stdin.readline()
        if not line:
            return 1
        code = line.strip()
        if scenario == "sticky_invalid" and asked_again:
            # (Its earlier message drawn again above the answer to the next code.)
            out.write("Invalid code. Please make sure the full code was copied.\r\n")
            out.flush()
            time.sleep(1)
        if "#" not in code:
            asked_again = True
            out.write("Invalid code. Please make sure the full code was copied.\r\n")
            out.flush()
            continue
        if code != GOOD_CODE:
            out.write("Login failed: That code wasn't accepted. It may be incomplete, expired, "
                      "already used, or from an earlier sign-in attempt. Sign in again to get "
                      "a new code.\r\n")
            out.flush()
            return 1
        with open(signed, "w", encoding="utf-8") as f:
            f.write(args[2] if len(args) > 2 else "--claudeai")
        out.write("Login successful.\r\n")
        out.flush()
        if scenario == "linger":
            time.sleep(3600)
        return 0


if __name__ == "__main__":
    sys.exit(main())
