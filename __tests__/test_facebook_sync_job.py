"""Async Facebook catalog sync: exponential backoff, error classification,
and the job runner (batching, partial failure, resume, idempotent claim)."""
import os, sys
os.environ["ENVIRONMENT"] = "development"
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "secret")
os.environ["FB_CATALOG_ID"] = "123"
os.environ["FB_CATALOG_ACCESS_TOKEN"] = "token"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.config import settings
from src.jobs import facebook_sync as runner
from src.jobs.job_service import JobBusyError
from src.services import facebook_catalog_service as fb
from src.services.facebook_catalog_service import (
    FacebookCatalogError,
    FacebookCatalogService,
    _graph_failure,
    backoff_delay,
)


# ---------------------------------------------------------------- backoff
def test_backoff_is_exponential_capped_and_jittered():
    for attempt, base in [(1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (6, 60), (9, 60)]:
        for _ in range(20):
            d = backoff_delay(attempt)
            assert base <= d <= base * 1.3 + 1e-9


@pytest.mark.parametrize("status,body,retryable", [
    (500, {}, True),
    (503, {"error": {"message": "x"}}, True),
    (429, {}, True),
    (400, {"error": {"code": 17, "message": "rate limit"}}, True),
    (400, {"error": {"code": 4, "message": "app limit"}}, True),
    (400, {"error": {"code": 1, "is_transient": True, "message": "x"}}, True),
    (400, {"error": {"code": 190, "message": "bad token"}}, False),
    (400, {"error": {"code": 100, "message": "invalid param"}}, False),
])
def test_graph_error_classification(status, body, retryable):
    assert _graph_failure(body, status).retryable is retryable


# ----------------------------------------------------- submit_with_retry
def _service():
    return FacebookCatalogService()


@pytest.mark.asyncio
async def test_transient_errors_retry_with_growing_delays_then_succeed():
    svc, sleeps, retries, calls = _service(), [], [], {"n": 0}

    async def flaky(client, requests):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise FacebookCatalogError("rate limited", retryable=True)
        return "handle-1"

    async def fake_sleep(s):
        sleeps.append(s)

    async def on_retry(attempt, delay, err):
        retries.append(attempt)

    with patch.object(svc, "_submit", flaky):
        handle = await svc.submit_with_retry([{}], on_retry=on_retry, sleep=fake_sleep)

    assert handle == "handle-1"
    assert retries == [1, 2]
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]


@pytest.mark.asyncio
async def test_non_retryable_error_fails_fast():
    svc, sleeps = _service(), []

    async def bad(client, requests):
        raise FacebookCatalogError("invalid token", retryable=False)

    async def fake_sleep(s):
        sleeps.append(s)

    with patch.object(svc, "_submit", bad), pytest.raises(FacebookCatalogError):
        await svc.submit_with_retry([{}], sleep=fake_sleep)
    assert sleeps == []


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts():
    svc, sleeps, calls = _service(), [], {"n": 0}

    async def always(client, requests):
        calls["n"] += 1
        raise FacebookCatalogError("down", retryable=True)

    async def fake_sleep(s):
        sleeps.append(s)

    with patch.object(svc, "_submit", always), pytest.raises(FacebookCatalogError):
        await svc.submit_with_retry([{}], sleep=fake_sleep)
    assert calls["n"] == settings.fb_sync_max_attempts
    assert len(sleeps) == settings.fb_sync_max_attempts - 1


@pytest.mark.asyncio
async def test_retry_after_is_a_floor_for_the_wait():
    svc, sleeps, calls = _service(), [], {"n": 0}

    async def breaker_open_then_ok(client, requests):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FacebookCatalogError("paused", retryable=True, retry_after=30.0)
        return "h"

    async def fake_sleep(s):
        sleeps.append(s)

    with patch.object(svc, "_submit", breaker_open_then_ok):
        await svc.submit_with_retry([{}], sleep=fake_sleep)
    assert sleeps[0] >= 30.0


# ------------------------------------------------------------ job runner
class FakeJobs:
    def __init__(self, doc):
        self.doc = doc

    async def get(self, job_id):
        return self.doc

    async def claim(self, job_id):
        stale = self.doc["heartbeat_at"] < datetime.utcnow() - timedelta(seconds=90)
        if self.doc["status"] == "queued" or (self.doc["status"] in ("running", "retrying") and stale):
            self.doc["status"] = "running"
            return self.doc
        return None

    async def patch(self, job_id, fields=None, *, event=None):
        self.doc["heartbeat_at"] = datetime.utcnow()
        self.doc.update(fields or {})
        if event:
            self.doc["events"].append({"level": event[0], "message": event[1]})


class FakeProducts:
    synced, errored = [], []

    def __init__(self, ids=None):
        self.ids = ids or [f"p{i}" for i in range(5)]

    async def list_active_ids(self):
        return list(self.ids)

    async def get_many(self, ids):
        return [SimpleNamespace(id=i, active=True) for i in ids]

    async def mark_facebook_synced(self, ids):
        FakeProducts.synced.extend(ids)

    async def set_facebook_error_many(self, ids, msg):
        FakeProducts.errored.extend(ids)


class FakeLogs:
    entries = []

    async def log(self, *, component, level, message, context=None):
        FakeLogs.entries.append((level, message))


def _job(**over):
    doc = dict(status="queued", heartbeat_at=datetime.utcnow(), product_ids=[], only_product_ids=None,
               total=0, processed=0, failed_count=0, chunks_total=0, chunks_done=0, next_chunk=0,
               attempt=0, failed_product_ids=[], last_error=None, events=[], created_by="a@b.c")
    doc.update(over)
    return doc


def _run(doc, fb_cls, products=None):
    FakeProducts.synced, FakeProducts.errored, FakeLogs.entries = [], [], []

    async def no_refresh(_):
        return 0

    async def no_sleep(_):
        return None

    return patch.multiple(
        runner,
        BackgroundJobService=lambda: FakeJobs(doc),
        ProductService=lambda: products or FakeProducts(),
        FacebookCatalogService=fb_cls,
        SystemLogService=FakeLogs,
        refresh_review_statuses=no_refresh,
        is_configured=lambda: True,
        PAUSE_BETWEEN_BATCHES_SECONDS=0,
    )


class OkFB:
    batches = []

    async def submit_chunk(self, products, *, on_retry=None, sleep=None):
        OkFB.batches.append([p.id for p in products])
        return "h"


@pytest.mark.asyncio
async def test_job_sends_all_products_in_batches_and_succeeds():
    OkFB.batches = []
    doc = _job()
    with patch.object(settings, "fb_sync_chunk_size", 2), _run(doc, OkFB):
        await runner.run_facebook_sync_job("job1")
    assert OkFB.batches == [["p0", "p1"], ["p2", "p3"], ["p4"]]
    assert doc["status"] == "succeeded"
    assert doc["processed"] == 5 and doc["failed_count"] == 0
    assert doc["chunks_total"] == 3 and doc["chunks_done"] == 3
    assert FakeProducts.synced == ["p0", "p1", "p2", "p3", "p4"]
    assert FakeLogs.entries[-1][0] == "info"


class OneBadBatchFB:
    calls = 0

    async def submit_chunk(self, products, *, on_retry=None, sleep=None):
        OneBadBatchFB.calls += 1
        if OneBadBatchFB.calls == 2:
            await on_retry(1, 2.0, FacebookCatalogError("rate limited", retryable=True))
            raise FacebookCatalogError("still rate limited", retryable=True)
        return "h"


@pytest.mark.asyncio
async def test_failed_batch_is_recorded_and_job_ends_partial_with_alert():
    OneBadBatchFB.calls = 0
    doc = _job()
    with patch.object(settings, "fb_sync_chunk_size", 2), _run(doc, OneBadBatchFB):
        await runner.run_facebook_sync_job("job1")
    assert doc["status"] == "partial"
    assert doc["processed"] == 3 and doc["failed_count"] == 2
    assert doc["failed_product_ids"] == ["p2", "p3"]
    assert FakeProducts.errored == ["p2", "p3"]
    assert any("retrying in" in e["message"] for e in doc["events"])
    assert any(e["level"] == "error" for e in doc["events"])
    assert FakeLogs.entries[-1][0] == "error"  # system log error -> Telegram alert


class AllFailFB:
    async def submit_chunk(self, products, *, on_retry=None, sleep=None):
        raise FacebookCatalogError("invalid token")


@pytest.mark.asyncio
async def test_job_fails_when_nothing_could_be_sent():
    doc = _job()
    with patch.object(settings, "fb_sync_chunk_size", 10), _run(doc, AllFailFB):
        await runner.run_facebook_sync_job("job1")
    assert doc["status"] == "failed"
    assert doc["processed"] == 0 and doc["failed_count"] == 5


@pytest.mark.asyncio
async def test_resumes_from_the_next_unfinished_batch():
    OkFB.batches = []
    ids = [f"p{i}" for i in range(5)]
    doc = _job(status="running", heartbeat_at=datetime.utcnow() - timedelta(minutes=5),
               product_ids=ids, total=5, chunks_total=3, processed=2, chunks_done=1, next_chunk=1)
    with patch.object(settings, "fb_sync_chunk_size", 2), _run(doc, OkFB):
        await runner.run_facebook_sync_job("job1")
    assert OkFB.batches == [["p2", "p3"], ["p4"]]  # batch 1 not repeated
    assert doc["status"] == "succeeded" and doc["processed"] == 5
    assert any("Resumed" in e["message"] for e in doc["events"])


@pytest.mark.asyncio
async def test_finished_job_is_a_noop_and_live_job_is_left_pending():
    OkFB.batches = []
    done = _job(status="succeeded")
    with _run(done, OkFB):
        await runner.run_facebook_sync_job("job1")
    assert OkFB.batches == []

    live = _job(status="running", heartbeat_at=datetime.utcnow(), product_ids=["p0"])
    with _run(live, OkFB), pytest.raises(JobBusyError):
        await runner.run_facebook_sync_job("job1")
    assert OkFB.batches == []
