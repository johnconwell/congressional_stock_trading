"""capitol_refresh.py: driving a Capitol API server's refresh (no network)."""
import requests

import capitol_refresh as cr


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeServer:
    """Replays /api/status bodies; `down` = number of initial GETs that fail to connect."""

    def __init__(self, statuses, *, down=0, post_status=200):
        self.statuses, self.down, self.post_status = list(statuses), down, post_status
        self.posts = 0

    def get(self, url, timeout=None):
        assert url.endswith("/api/status")
        if self.down:
            self.down -= 1
            raise requests.ConnectionError("refused")
        body = self.statuses[0] if len(self.statuses) == 1 else self.statuses.pop(0)
        return Resp(body)

    def post(self, url, timeout=None):
        assert url.endswith("/api/refresh")
        self.posts += 1
        return Resp({"message": "started"}, self.post_status)


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def __call__(self):
        return self.t


def refresher(server, logs=None):
    clock = Clock()
    return cr.CapitolRefresher("http://capitol:3000/", session=server, sleep=clock.sleep, clock=clock,
                               log=(logs.append if logs is not None else (lambda m: None)))


IDLE = {"fetchRunning": False, "cacheStatus": "complete", "totalTrades": 100, "fetchError": None}
RUNNING = {"fetchRunning": True, "fetchProgress": "2026: 3/40", "cacheStatus": "in_progress", "totalTrades": 101}
DONE = {"fetchRunning": False, "cacheStatus": "complete", "totalTrades": 120, "fetchError": None}


def test_incremental_refresh_waits_for_completion():
    server = FakeServer([IDLE, RUNNING, RUNNING, DONE], down=2)  # server still starting at first
    r = refresher(server)
    assert r.run(timeout=3600, poll=10) == cr.EXIT_COMPLETE
    assert server.posts == 1
    assert r.last_status["totalTrades"] == 120


def test_fetch_already_running_on_an_empty_cache_is_not_restarted():
    server = FakeServer([RUNNING, RUNNING, DONE])
    assert refresher(server).run(timeout=3600, poll=10) == cr.EXIT_COMPLETE
    assert server.posts == 0


def test_timeout_while_still_fetching():
    logs = []
    server = FakeServer([RUNNING])
    assert refresher(server, logs).run(timeout=60, poll=10) == cr.EXIT_TIMEOUT
    assert any("resumes" in m for m in logs)


def test_fetch_error_and_incomplete_cache():
    failed = {**DONE, "fetchError": "socket hang up"}
    assert refresher(FakeServer([IDLE, failed])).run(timeout=60, poll=10) == cr.EXIT_ERROR
    stuck = {**DONE, "cacheStatus": "in_progress"}
    assert refresher(FakeServer([IDLE, stuck])).run(timeout=60, poll=10) == cr.EXIT_INCOMPLETE


def test_server_never_answers():
    server = FakeServer([IDLE], down=10_000)
    assert refresher(server).run(timeout=60, poll=10, startup_timeout=30) == cr.EXIT_ERROR


def test_refresh_request_rejected():
    assert refresher(FakeServer([IDLE], post_status=500)).run(timeout=60) == cr.EXIT_ERROR


def test_github_output(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    cr.write_github_output(cr.EXIT_COMPLETE, DONE)
    cr.write_github_output(cr.EXIT_TIMEOUT, {})
    assert out.read_text() == "complete=true\ntrades=120\ncomplete=false\ntrades=0\n"
