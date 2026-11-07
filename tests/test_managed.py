"""managed lifetime uses the real queues and http requests, without loading models."""

import threading
import time

from fastapi.testclient import TestClient
from test_server import FakeEngine, await_phase, start_job

from cover_engine.managed import ManagedLifetime
from cover_engine.server import create_app


def test_quitting_waits_for_processing_and_a_reopened_client_keeps_engine_alive(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class BlockingEngine(FakeEngine):
        def analyze(self, *args):
            entered.set()
            assert release.wait(5)
            return super().analyze(*args)

    app = create_app(tmp_path, engine=BlockingEngine())
    lifetime = ManagedLifetime(app, grace=0.05)
    with TestClient(lifetime, base_url="http://127.0.0.1:8891") as client:
        job = start_job(client)
        assert entered.wait(2)
        lifetime.disconnected.set()
        time.sleep(0.06)
        assert not lifetime.should_stop()
        release.set()
        await_phase(client, job, "ready")
        time.sleep(0.06)
        assert lifetime.should_stop()
        assert client.get("/health").status_code == 200
        assert not lifetime.should_stop()


def test_idle_engine_only_exits_after_owner_disconnects(tmp_path):
    app = create_app(tmp_path, engine=FakeEngine())
    lifetime = ManagedLifetime(app, grace=0)
    with TestClient(lifetime, base_url="http://127.0.0.1:8891"):
        assert not lifetime.should_stop()
        lifetime.disconnected.set()
        assert lifetime.should_stop()
        lifetime.requests = 1
        assert not lifetime.should_stop()


def test_pending_extraction_and_assessment_prevent_shutdown(tmp_path):
    app = create_app(tmp_path, engine=FakeEngine())
    lifetime = ManagedLifetime(app, grace=0)
    with TestClient(lifetime, base_url="http://127.0.0.1:8891"):
        lifetime.disconnected.set()
        store = app.state.store
        with store.lock:
            store.extractions.items["test"] = {"phase": "queued"}
        assert not lifetime.should_stop()
        with store.lock:
            store.extractions.items.clear()
        with store.assessments.lock:
            store.assessments.records["test"] = {"result": {"status": "running"}}
        assert not lifetime.should_stop()
        with store.assessments.lock:
            store.assessments.records.clear()
        assert lifetime.should_stop()
