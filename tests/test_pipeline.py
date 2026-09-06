"""Tests for pipeline helpers and the JobManager lifecycle."""
import asyncio
import json
import logging

import pytest

import pipeline as p


# ── filename / config helpers ──────────────────────────────────────────────────


def test_file_stem_sanitizes_unsafe_names():
    assert p.file_stem("A/B") == "A_B"
    assert p.file_stem('a:b*?"<>|') == "a_b______"  # ':' and 6 chars after 'b'
    assert p.file_stem("  ") == "Unnamed"
    assert p.file_stem("..") == "Unnamed"
    assert p.file_stem("") == "Unnamed"
    assert p.file_stem("CON") == "_CON"  # Windows reserved device name
    assert p.file_stem("com1") == "_com1"
    assert p.file_stem("normal name") == "normal name"
    assert p.file_stem("trailing. ") == "trailing"


def test_unique_stems_dedupe_collisions():
    stems = p._unique_stems(["A/B", "A_B", "c/d", "c_d"])
    assert len(stems) == 4
    assert len(set(stems.values())) == 4
    assert stems["A/B"] != stems["A_B"]
    assert stems["c/d"] != stems["c_d"]
    assert stems["A/B"] == "A_B"
    assert stems["A_B"] == "A_B_2"
    assert stems["c_d"] == "c_d_2"


def test_split_part_stem_never_clobbers_reserved_stems():
    # A group literally called "Mods_part1" exists -> splitting "Mods" must
    # not produce a file that overwrites it.
    reserved = {"Mods", "Mods_part1", "Other"}
    assert p._split_part_stem("Mods", 1, reserved) == "Mods_part1_"
    assert p._split_part_stem("Mods", 2, reserved) == "Mods_part2"
    # The claimed name stays reserved for the rest of the run.
    assert p._split_part_stem("Mods", 1, reserved) == "Mods_part1__"
    # Unrelated groups keep their plain stems (seeded stems are reserved for
    # their own future parts, and those parts then stay reserved too).
    assert p._split_part_stem("Other", 1, reserved) == "Other_part1"
    assert p._split_part_stem("Other", 1, reserved) == "Other_part1_"


def test_cfg_int_clamps_and_logs(caplog):
    assert p._cfg_int({}, "workers_api") == 4          # default
    assert p._cfg_int({"workers_api": 2}, "workers_api") == 2
    assert p._cfg_int({"workers_api": 0}, "workers_api") == 1   # no Semaphore(0) hang
    assert p._cfg_int({"workers_api": -3}, "workers_api") == 1
    assert p._cfg_int({"workers_api": 999}, "workers_api") == 64
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        assert p._cfg_int({"workers_api": "abc"}, "workers_api") == 4
    assert any("workers_api" in r.message for r in caplog.records)


def test_cfg_float_handles_nan_and_negatives(caplog):
    assert p._cfg_float({}, "delay") == 0.2
    assert p._cfg_float({"delay": -1}, "delay") == 0.0
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        assert p._cfg_float({"delay": float("nan")}, "delay") == 0.2
    assert any("NaN" in r.message for r in caplog.records)


# ── extraction robustness ───────────────────────────────────────────────────────


def _pkg(owner="Owner", name="Mod", versions=None, categories=None):
    return {
        "owner": owner, "name": name, "full_name": "{}-{}".format(owner, name),
        "versions": versions if versions is not None else [{
            "version_number": "1.0.0", "changelog": "first", "readme": "readme text",
            "dependencies": ["Dep-Pack"],
        }],
        "categories": categories if categories is not None else ["Misc"],
    }


def test_extract_mod_tolerates_missing_version_number():
    pkg = _pkg(versions=[{"version_number": None, "changelog": "c"},
                         {"changelog": "also no version number"}])
    mod = p._extract_mod(pkg)
    assert mod["metadata"]["version"] == "N/A"
    assert "N/A" in mod["changelog"]
    assert "also no version number" in mod["changelog"]


def test_extract_mod_tolerates_non_dict_versions():
    pkg = _pkg(versions=[None, "junk", {"version_number": "2.0", "changelog": ""}])
    mod = p._extract_mod(pkg)
    assert mod["metadata"]["version"] == "2.0"


def test_extract_mod_reads_category_from_dict_entries():
    pkg = _pkg(categories=[{"name": "Library", "slug": "library"}])
    assert p._extract_mod(pkg)["metadata"]["category"] == "Library"
    pkg = _pkg(categories=[])
    assert p._extract_mod(pkg)["metadata"]["category"] == "Uncategorized"


def test_extract_all_isolates_malformed_packages(caplog):
    good = _pkg(owner="Good", name="One")
    no_name = {"id": 1, "versions": []}
    good2 = _pkg(owner="Good", name="Two", versions=[{
        "changelog": "no version number key", "readme": ""}])
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        mods, skipped = p._extract_all([good, no_name, good2])
    assert len(mods) == 2
    assert skipped == 1
    assert any("Skipping" in r.message for r in caplog.records)


# ── JobManager ──────────────────────────────────────────────────────────────────


def test_create_meta_and_persistence(tmp_path):
    jm = p.JobManager(str(tmp_path))
    jid = jm.create("riskofrain2", {"delay": 0.5})
    meta = jm.meta(jid)
    assert meta["status"] == "pending"
    assert meta["community"] == "riskofrain2"
    assert meta["steps"] == p.STEPS
    # persisted across instances
    jm2 = p.JobManager(str(tmp_path))
    assert jid in jm2.jobs
    assert jm2.jobs[jid]["params"] == {"delay": 0.5}
    assert jm2.logs[jid] == []


def test_zombie_running_job_resets_to_stopped(tmp_path):
    dir_ = str(tmp_path)
    jm = p.JobManager(dir_)
    jid = jm.create("riskofrain2", {})
    jm.jobs[jid]["status"] = "running"
    jm._save_jobs()
    jm2 = p.JobManager(dir_)
    assert jm2.jobs[jid]["status"] == "stopped"
    assert jm2.logs[jid][-1] == p.END_MARKER


def test_corrupt_jobs_json_is_backed_up_not_silently_lost(tmp_path, caplog):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    bad = jobs_dir / "jobs.json"
    bad.write_text("{definitely not json")
    with caplog.at_level(logging.ERROR, logger="pipeline"):
        jm = p.JobManager(str(tmp_path))
    assert jm.jobs == {}
    assert len(list(jobs_dir.glob("jobs.json.bak-*"))) == 1
    assert any("Unreadable" in r.message for r in caplog.records)


def test_malformed_job_records_are_dropped_with_log(tmp_path, caplog):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "jobs.json").write_text(json.dumps({
        "good-job": {"community": "x", "status": "done",
                     "created_at": "2026-01-01T00:00:00", "params": {}},
        "broken-job": "just a string",
    }))
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        jm = p.JobManager(str(tmp_path))
    assert set(jm.jobs) == {"good-job"}
    assert any("Ignoring malformed job record" in r.message for r in caplog.records)


def test_meta_lists_export_files_only(tmp_path):
    jm = p.JobManager(str(tmp_path))
    jid = jm.create("riskofrain2", {})
    out = jm.output_dir / "riskofrain2"
    out.mkdir(parents=True)
    (out / "Misc.md").write_text("hello")
    (out / "Misc.pdf").write_text("x" * 4096)
    (out / "notes.txt").write_text("hi")
    (out / "ignore.bin").write_text("hi")
    names = {f["name"] for f in jm.meta(jid)["files"]}
    assert names == {"Misc.md", "Misc.pdf", "notes.txt"}


def test_delete_cancels_a_running_task(tmp_path):
    async def scenario():
        jm = p.JobManager(str(tmp_path))
        jid = jm.create("riskofrain2", {})
        jm.jobs[jid]["status"] = "running"
        started = asyncio.Event()

        async def stuck():
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(stuck())
        jm._ctrl[jid] = {"task": task}
        await started.wait()
        await jm.delete(jid)
        assert task.done()
        assert jid not in jm.jobs
        assert jid not in jm.logs
        assert jid not in jm._ctrl
        assert not (jm.jobs_dir / jid).exists()
        # and the deletion is persisted
        jm2 = p.JobManager(str(tmp_path))
        assert jid not in jm2.jobs

    asyncio.run(scenario())


def test_start_awaits_stale_task_before_restarting(tmp_path):
    async def scenario():
        jm = p.JobManager(str(tmp_path))
        jid = jm.create("riskofrain2", {})
        jm.jobs[jid]["status"] = "stopped"  # user restarted quickly after stop
        started = asyncio.Event()

        async def stale():
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(stale())
        jm._ctrl[jid] = {"task": task}
        await started.wait()

        ran = asyncio.Event()

        async def fake_run(_job_id):
            ran.set()

        jm._run = fake_run  # avoid any network in this test
        await jm.start(jid)
        assert task.cancelled()          # old run was reaped
        assert jm.jobs[jid]["status"] == "running"
        await asyncio.wait_for(ran.wait(), timeout=2)
        await jm.delete(jid)

    asyncio.run(scenario())


def test_full_pipeline_run_offline(tmp_path, monkeypatch):
    """Runs the whole 6-step pipeline against a stubbed Thunderstore client."""
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_packages(self, community):
            return [
                {"owner": "OwnerA", "name": "ModOne", "full_name": "OwnerA-ModOne",
                 "versions": [{"version_number": "1.0.0", "changelog": "fixes",
                               "readme": "README of ModOne with several words here.",
                               "dependencies": []}],
                 "categories": ["Misc", "Tools"]},
                {"owner": "OwnerB", "name": "ModTwo", "full_name": "OwnerB-ModTwo",
                 "versions": [{"version_number": "2.1.0", "changelog": "",
                               "readme": "", "dependencies": []}],
                 "categories": ["Misc"]},
                # malformed record: no owner/name/full_name at all
                {"versions": []},
                # malformed version record: changelog without a version number
                {"owner": "OwnerC", "name": "ModThree", "full_name": "OwnerC-ModThree",
                 "versions": [{"changelog": "orphan changelog", "readme": "r"}],
                 "categories": ["Misc"]},
            ]

        async def get_categories(self, community, raise_on_error=False):
            return [{"name": "Misc", "slug": "misc"}]

    monkeypatch.setattr(p, "ThunderstoreClient", FakeClient)
    jm = p.JobManager(str(tmp_path))

    async def scenario():
        jid = jm.create("riskofrain2", {"export_formats": ["txt"], "delay": 0})
        await jm.start(jid)
        for _ in range(400):  # up to ~20 s
            if jm.jobs[jid]["status"] in ("done", "error", "stopped"):
                return jm.jobs[jid], jid
            await asyncio.sleep(0.05)
        return jm.jobs[jid], jid

    job, jid = asyncio.run(scenario())
    assert job["status"] == "done", job
    out = jm.output_dir / "riskofrain2"
    files = sorted(f.name for f in out.iterdir())
    assert "Misc.txt" in files
    content = (out / "Misc.txt").read_text()
    assert "README of ModOne" in content
    assert "OwnerC/ModThree" in content  # malformed version tolerated
    # working files written
    assert (jm.jobs_dir / jid / "mods_index.json").exists()
    assert (jm.jobs_dir / jid / "category_groups.json").exists()
    # log ends with the terminal marker, and the skip was reported in the log
    assert p.END_MARKER in jm.logs[jid]
    assert any("skipped" in line for line in jm.logs[jid])
    assert (jm.jobs_dir / jid / "pipeline.log").exists()
