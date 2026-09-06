"""API tests: validation, lifecycle endpoints, configs (no network)."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
from pipeline import JobManager


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by a JobManager rooted in a temp directory."""
    jm = JobManager(str(tmp_path / "data"))
    monkeypatch.setattr(app_module, "job_manager", jm)
    with TestClient(app_module.app) as test_client:
        test_client.jm = jm  # handy handle for assertions
        yield test_client


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Thunderstore Archive" in r.text


def test_create_job_requires_valid_community(client):
    for bad in ("", "../etc", "a/b", "a b", ".."):
        r = client.post("/api/jobs", data={"community": bad, "config": "{}"})
        assert r.status_code == 400, bad
        assert r.json()["detail"]  # message present


def test_create_job_config_must_be_object(client):
    r = client.post("/api/jobs",
                    data={"community": "riskofrain2", "config": "[1,2]"})
    assert r.status_code == 400
    r = client.post("/api/jobs",
                    data={"community": "riskofrain2", "config": "not json"})
    assert r.status_code == 400


def test_job_crud_and_status_flow(client):
    r = client.post("/api/jobs", data={
        "community": "riskofrain2",
        "config": json.dumps({"export_formats": ["md"], "delay": 0}),
    })
    assert r.status_code == 200
    job = r.json()
    jid = job["id"]
    assert job["status"] == "pending"
    assert job["steps"]

    assert client.get(f"/api/jobs/{jid}/status").json()["status"] == "pending"
    # unknown job -> 404s
    assert client.get("/api/jobs/nope/status").status_code == 404
    assert client.post("/api/jobs/nope/start").status_code == 404
    assert client.post("/api/jobs/nope/stop").status_code == 404
    assert client.delete("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope/logs").status_code == 404

    # no output yet -> the download/reveal endpoints 404 cleanly
    assert client.get(f"/api/jobs/{jid}/download-pdfs").status_code == 404
    assert client.post(f"/api/jobs/{jid}/reveal").status_code == 404
    assert client.get(f"/api/jobs/{jid}/download-log").status_code == 404

    # stop on a not-started job is harmless
    assert client.post(f"/api/jobs/{jid}/stop").json() == {"ok": True}

    assert client.delete(f"/api/jobs/{jid}").status_code == 200
    assert client.get(f"/api/jobs/{jid}/status").status_code == 404


def test_start_and_delete_routes_await_lifecycle(client):
    """start/delete are async in the manager; routes must wire them through."""
    jm = client.jm

    async def fake_run(job_id):
        await asyncio.sleep(0.01)
        jm.jobs[job_id]["status"] = "done"

    jm._run = fake_run

    r = client.post("/api/jobs", data={
        "community": "valheim", "config": json.dumps({"export_formats": ["md"]}),
    })
    jid = r.json()["id"]
    assert client.post(f"/api/jobs/{jid}/start").status_code == 200
    assert jm.jobs[jid]["status"] == "running"
    # wait for the fake pipeline to finish
    async def wait_done():
        for _ in range(200):
            if jm.jobs[jid]["status"] == "done":
                return True
            await asyncio.sleep(0.01)
        return False
    assert asyncio.run(wait_done())
    # resume on a finished job restarts it
    assert client.post(f"/api/jobs/{jid}/resume").status_code == 200
    assert jm.jobs[jid]["status"] == "running"
    assert asyncio.run(wait_done())

    assert client.delete(f"/api/jobs/{jid}").status_code == 200
    assert jid not in jm.jobs


def test_categories_route_rejects_bad_community_without_network(client):
    r = client.get("/api/communities/..%2F..%2Fetc/categories")
    # either rejected (encoded slash) or 400 — but never a network call
    assert r.status_code in (400, 404)
    r = client.get("/api/communities/a%20b/categories")
    assert r.status_code == 400


def test_configs_endpoints_validate_and_roundtrip(client, tmp_path):
    assert client.post("/api/configs/..%2F..%2Fetc", json={"a": 1}).status_code in (400, 404)
    assert client.post("/api/configs/a%20b", json={"a": 1}).status_code == 400
    assert client.get("/api/configs/a%20b").status_code == 400

    r = client.post("/api/configs/riskofrain2", json={"workers_api": 6, "tags": ["x"]})
    assert r.status_code == 200
    r = client.get("/api/configs/riskofrain2")
    assert r.status_code == 200
    assert r.json() == {"workers_api": 6, "tags": ["x"]}

    # non-object bodies rejected
    r = client.post("/api/configs/riskofrain2", content="[1,2,3]",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_list_jobs_sorted_and_meta_files(client):
    jid_a = client.post("/api/jobs", data={"community": "aaa", "config": "{}"}).json()["id"]
    jid_b = client.post("/api/jobs", data={"community": "bbb", "config": "{}"}).json()["id"]
    jobs = client.get("/api/jobs").json()
    ids = [j["id"] for j in jobs]
    assert ids == [jid_b, jid_a]  # newest first
