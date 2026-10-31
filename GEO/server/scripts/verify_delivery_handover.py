#!/usr/bin/env python3
"""Read-only handover audit; manifests contain hashes, never questions or answers.

Snapshot only after all old writers have been frozen. Verify a copied backup
with --exact, then verify live data after the candidate loader has migrated it.
The script sends no HTTP, browser, platform, CLI or notification requests.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path


TERMINAL = {"succeeded", "partial", "failed", "cancelled"}
PENDING = {"queued", "running"}
IDENTITY = ("job_id", "request_id", "prompt", "sites", "ak", "callback_url", "created_at")
ANSWER = ("site_id", "ok", "answer", "answer_raw", "answer_origin", "source")


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_job(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("job_id") != path.stem:
        raise ValueError("invalid job identity: " + path.name)
    if value.get("status") not in TERMINAL | PENDING:
        raise ValueError("invalid job status: " + path.name)
    return value


def job_identity(job):
    return digest({key: job.get(key, [] if key == "sites" else "") for key in IDENTITY})


def successful_answers(job):
    rows = []
    for result in job.get("results") or []:
        if result.get("ok") is True and str(result.get("answer") or "").strip():
            rows.append(digest({key: result.get(key, "") for key in ANSWER}))
    return sorted(rows)


def snapshot(data):
    if not (data / "jobs").is_dir():
        raise ValueError("jobs directory is missing")
    jobs = {}
    for path in sorted((data / "jobs").glob("*.json")):
        job = read_job(path)
        jobs[job["job_id"]] = {"identity": job_identity(job), "status": job["status"],
                               "answers": successful_answers(job), "file": file_digest(path)}
    if not jobs:
        raise ValueError("no accepted jobs to snapshot")
    journals = {}
    for path in sorted((data / "web-recovery").rglob("*")):
        if path.is_file():
            journals[str(path.relative_to(data))] = file_digest(path)
    return {"version": 1, "jobs": jobs, "journals": journals}


def verify(data, manifest, exact=False, must_remain_pending=()):
    if manifest.get("version") != 1 or not manifest.get("jobs"):
        raise ValueError("invalid or empty handover manifest")
    problems = []
    current = {}
    for job_id, original in manifest["jobs"].items():
        path = data / "jobs" / (job_id + ".json")
        if not path.is_file():
            problems.append("missing job " + job_id)
            continue
        job = read_job(path)
        current[job_id] = job
        if job_identity(job) != original["identity"]:
            problems.append("changed request identity " + job_id)
        remaining = successful_answers(job)
        for answer in original["answers"]:
            if answer not in remaining:
                problems.append("lost successful answer " + job_id)
                break
            remaining.remove(answer)
        if exact or original["status"] in TERMINAL:
            if file_digest(path) != original["file"]:
                problems.append("changed " + ("snapshot" if exact else "terminal history") + " " + job_id)
        elif job["status"] in {"partial", "failed"}:
            problems.append("pending request prematurely terminal " + job_id)
        elif job["status"] == "succeeded":
            results = job.get("results") or []
            for site in job.get("sites") or []:
                canonical = lambda value: "wenxinyiyan" if value == "yiyan" else value
                if not any(canonical(row.get("site_id")) == canonical(site) and row.get("ok") is True
                           and str(row.get("answer") or "").strip() for row in results):
                    problems.append("missing final platform answer " + job_id)
                    break
    for relative, original_hash in manifest["journals"].items():
        path = data / relative
        job_id = Path(relative).parts[1]
        completed = current.get(job_id, {}).get("status") in {"succeeded", "cancelled"}
        if not path.is_file():
            if exact or not completed:
                problems.append("missing pending journal " + relative)
        elif exact and file_digest(path) != original_hash:
            problems.append("changed snapshot journal " + relative)
    for job_id in must_remain_pending:
        if current.get(job_id, {}).get("status") not in PENDING:
            problems.append("expected pending job changed " + job_id)
    if problems:
        raise ValueError("; ".join(problems))
    return {"verified_jobs": len(current),
            "preserved_successful_answers": sum(len(row["answers"]) for row in manifest["jobs"].values()),
            "original_pending_jobs": sum(row["status"] in PENDING for row in manifest["jobs"].values()),
            "original_journals": len(manifest["journals"]), "exact_backup": exact}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "verify"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--exact", action="store_true")
    parser.add_argument("--must-remain-pending", action="append", default=[])
    args = parser.parse_args()
    if args.action == "snapshot":
        value = snapshot(args.data)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        print(json.dumps({"snapshot_jobs": len(value["jobs"]), "snapshot_journals": len(value["journals"])}))
    else:
        value = json.loads(args.manifest.read_text(encoding="utf-8"))
        print(json.dumps(verify(args.data, value, args.exact, args.must_remain_pending)))


if __name__ == "__main__":
    main()
