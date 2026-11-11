"""An existing indexed page must be submitted only on explicit opt-in.

The browser tests execute the shipped JavaScript, with the Google UI and
Chrome APIs replaced locally. No browser, credentials or HTTP is involved.
"""
import json
from pathlib import Path
import shutil
import subprocess
import threading
from contextlib import contextmanager

import pytest

from gsc_core import bridge, config, store, submit
from gsc_mcp import jobs, tools_submit

PROPERTY = "sc-domain:example.com"
URL = "https://example.com/updated"


class Sender:
    def __init__(self):
        self.calls = []

    def submit(self, property, url, authuser, *, reindex=False):
        self.calls.append(reindex)
        return "submitted" if reindex else "already_indexed"


@pytest.mark.parametrize("reindex", [False, True])
def test_sync_tool_preserves_opt_in_and_quota(monkeypatch, store_conn, reindex):
    store.upsert_site(store_conn, PROPERTY, "example.com", "siteFullUser", [])
    sender = Sender()

    @contextmanager
    def session(*args):
        yield sender

    monkeypatch.setattr(tools_submit.target, "resolve", lambda: object())
    monkeypatch.setattr(tools_submit.bridge, "bridge_session", session)
    result = tools_submit.request_indexing([URL], reindex=reindex)
    assert sender.calls == [reindex]
    assert result["submitted"] == int(reindex)
    assert store_conn.execute("SELECT COUNT(*) FROM quota_slots").fetchone()[0] == int(reindex)


def test_reindex_does_not_bypass_the_quota_gate(store_conn):
    sender = Sender()
    cfg = {**config.DEFAULTS, "reindex": True, "daily_reserve": 10}
    # One outstanding reservation uses the only spendable slot.
    submit.reserve(store_conn, URL, PROPERTY, "default", None,
                   property_slots=11, account_slots=None, daily_reserve=10)
    result = submit.run(store_conn, sender, [URL], properties=[PROPERTY],
                        account="default", job_id=None, cfg=cfg,
                        sleep=lambda _: None)
    assert sender.calls == []
    assert result.stop_reason == "no_quota"


@pytest.mark.parametrize("call", [tools_submit.request_indexing,
                                  tools_submit.start_indexing_job])
def test_invalid_reindex_is_refused_before_opening_anything(monkeypatch, call):
    def forbidden(*a, **k):
        pytest.fail("invalid mode reached the browser or store")
    monkeypatch.setattr(tools_submit.target, "resolve", forbidden)
    monkeypatch.setattr(tools_submit.deps, "connection", forbidden)
    result = call([URL], reindex="true")
    assert result["ok"] is False
    assert result["error"] == "bad_reindex"


def test_background_tool_passes_an_explicit_per_call_mode(monkeypatch, store_conn):
    store.upsert_site(store_conn, PROPERTY, "example.com", "siteFullUser", [])
    seen = []
    monkeypatch.setattr(jobs, "start", lambda urls, cfg: seen.append(cfg) or "job")
    assert tools_submit.start_indexing_job([URL], reindex=True)["ok"]
    assert seen[-1]["reindex"] is True
    assert tools_submit.start_indexing_job([URL])["ok"]
    assert seen[-1]["reindex"] is False


def test_reindex_frame_is_explicit_and_legacy_frames_stay_unchanged():
    original = json.loads(bridge.make_submit("id", PROPERTY, URL, "0"))
    assert "reindex" not in original
    opted_in = json.loads(bridge.make_submit("id", PROPERTY, URL, "0", reindex=True))
    assert opted_in == {**original, "reindex": True}


class Extension:
    def __init__(self, session):
        self.session = session
        self.sent = []

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["type"] == "submit":
            self.session._dispatch(self, {"type": "result", "id": msg["id"],
                                          "outcome": "submitted"})


@pytest.mark.parametrize("supported", [False, True])
def test_old_extension_cannot_silently_ignore_reindex(supported):
    session = bridge.BridgeSession(port=0, token="test-only")
    extension = Extension(session)
    session._claim(extension, capabilities=["reindex"] if supported else [])
    result = session.submit(PROPERTY, URL, "0", reindex=True, timeout=1)
    assert result == ("submitted" if supported else "reindex_unsupported")
    assert len(extension.sent) == int(supported)
    if supported:
        assert extension.sent[0]["reindex"] is True


def test_uncertain_reindex_send_is_not_replayed():
    session = bridge.BridgeSession(port=0, token="test-only")

    class DroppingExtension(Extension):
        def send(self, raw):
            self.sent.append(json.loads(raw))
            raise OSError("connection dropped during send")

    extension = DroppingExtension(session)
    session._claim(extension, capabilities=["reindex"])
    assert session.submit(PROPERTY, URL, "0", reindex=True, timeout=1) == "timeout"
    assert len(extension.sent) == 1


def test_background_job_keeps_its_mode_in_durable_progress(monkeypatch, home):
    finished = threading.Event()

    def execute(conn, job_id, urls, cfg, stop_event, on_progress):
        assert cfg["reindex"] is True
        finished.set()
        return submit.RunResult([], False, None)

    monkeypatch.setattr(jobs, "_execute", execute)
    job_id = jobs.start([URL], {**config.DEFAULTS, "reindex": True})
    assert finished.wait(5)
    assert jobs.join(job_id, 5)
    with store.session() as conn:
        assert store.get_job(conn, job_id)["progress"]["reindex"] is True


@pytest.mark.parametrize("reindex,outcome,expected,clicks", [
    (False, "submitted", "already_indexed", 0),
    (True, "submitted", "submitted", 1),
    (True, "quota_exceeded", "quota_exceeded", 1),
    (True, "timeout", "timeout", 1),
])
def test_shipped_content_script_handles_indexed_pages(reindex, outcome, expected, clicks):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to execute the browser JavaScript")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests" / "extension-reindex.cjs"),
         str(root / "src/gsc_mcp/extension/content.js"),
         json.dumps({"reindex": reindex, "outcome": outcome})],
        capture_output=True, text=True, check=True, timeout=5)
    assert json.loads(result.stdout) == {"outcome": expected, "clicks": clicks}


@pytest.mark.parametrize("supported", [False, True])
def test_worker_checks_the_tab_before_forwarding_reindex(supported):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to execute the browser JavaScript")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/extension-reindex-background.cjs"),
         str(root / "src/gsc_mcp/extension/background.js"), json.dumps(supported)],
        capture_output=True, text=True, check=True, timeout=5)
    value = json.loads(result.stdout)
    assert value["accepted"] is supported
    assert value["jobs"] == ([{"type": "job", "id": "test", "url": URL,
                               "authuser": "0", "reindex": True}] if supported else [])
