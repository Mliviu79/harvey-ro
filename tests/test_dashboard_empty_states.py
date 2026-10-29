"""Empty, offline and error states of the dashboard.

The empty-state text once used the class "copy", which is also the copy
button's class (inline-flex, 22px wide): every empty page rendered its
message one word per line. These tests keep the empty-state markup on
classes of its own, and check the offline/error variants under node.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import harvey.dashboard as dash

WEB = Path(dash.__file__).resolve().parent / "web"


def _css_rules():
    css = re.sub(r"/\*.*?\*/", "", (WEB / "app.css").read_text(encoding="utf-8"), flags=re.S)
    for m in re.finditer(r"([^{}@]+)\{([^{}]*)\}", css):
        yield [s.strip() for s in m.group(1).split(",") if s.strip()], m.group(2)


def _js_function(src, name):
    start = src.index("function " + name + "(")
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
        if depth == 0:
            return src[start:i]


def _empty_state_classes():
    body = _js_function((WEB / "app.js").read_text(encoding="utf-8"), "emptyState")
    return set(re.findall(r'class="([\w-]+)', body)) | set(re.findall(r"' ([\w-]+)' :", body))


def test_empty_state_classes_are_not_shared_with_other_components():
    classes = _empty_state_classes() - {"empty"}
    assert {"glyph", "title", "empty-copy", "empty-action", "boxed"} <= classes
    assert "copy" not in classes                          # the copy button's class
    for selectors, _ in _css_rules():
        for sel in selectors:
            for cls in classes:
                if re.search(r"\." + re.escape(cls) + r"(?![\w-])", sel):
                    # Styled only as part of an empty state, never on its own.
                    assert re.search(r"\.empty\b", sel), (cls, sel)


def test_empty_state_text_is_a_block_that_wraps():
    rules = [body for sels, body in _css_rules() if any(s.endswith(".empty-copy") for s in sels)]
    assert rules
    for body in rules:
        assert not re.search(r"display\s*:\s*(inline-)?(flex|grid)", body)
        assert not re.search(r"(?<![-\w])width\s*:", body)
        assert "nowrap" not in body


def test_offline_and_server_error_states_under_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    parts = [_js_function(app, n) for n in ("escHtml", "icon", "emptyState", "failedOnServer", "offlineState",
                                            "offlineLine", "pauseReason")]
    script = (
        f"require('vm').runInThisContext(require('fs').readFileSync({json.dumps(str(WEB / 'i18n.js'))}, 'utf8'));"
        f"require('vm').runInThisContext({json.dumps(chr(10).join(parts) + chr(10) + 'var _lastFail = null;')});"
        "setLang('en');"
        "const out = {};"
        "_lastFail = 'network'; out.net = offlineState(); out.netLine = offlineLine();"
        "_lastFail = 500; out.srv = offlineState(); out.srvLine = offlineLine();"
        "out.plain = emptyState('', 'T', 'C');"
        "out.pause = pauseReason('paused from dashboard'); out.other = pauseReason('bounce rate 9%');"
        "setLang('ro'); out.roPause = pauseReason('paused from dashboard'); out.roSrv = offlineState();"
        "process.stdout.write(JSON.stringify(out));")
    out = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                    encoding="utf-8", check=True, timeout=30).stdout)
    # Failed reads get a card of their own, a retry button, and say which failure it was.
    assert 'class="empty boxed"' in out["net"] and "loadCurrentTab()" in out["net"]
    assert "can’t reach the server" in out["net"]
    assert "HTTP 500" in out["srv"] and "returned an error" in out["srv"]
    assert "returned an error" in out["srvLine"] and "reach the server" in out["netLine"]
    assert 'class="empty"' in out["plain"] and "empty-action" not in out["plain"]
    assert out["pause"] == "Paused from the dashboard" and out["other"] == "bounce rate 9%"
    assert out["roPause"] == "Oprită din panou" and "Serverul a dat o eroare" in out["roSrv"]
