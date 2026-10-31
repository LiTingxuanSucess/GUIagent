# -*- coding: utf-8 -*-
"""GEO 抓取结果只读展示：按 题目 → 批次 → 渠道 聚合。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

GEO_DIR = Path(__file__).resolve().parents[1]
EXPORT_DIR = GEO_DIR / "exports"
STATIC_DIR = Path(__file__).resolve().parent / "static"
QUESTIONS_PATH = GEO_DIR / "questions_cn.json"

SITE_ORDER = ["deepseek", "doubao", "yiyan", "yuanbao", "qianwen", "kimi"]
QUESTION_ORDER = ["solver_vendors", "decision_ai_vendors", "ev_cars_global"]

app = FastAPI(title="GEO Results Viewer", version="0.2.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _list_batch_files() -> List[Path]:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    files = list(EXPORT_DIR.glob("cn-ai-*.json"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _load_batch(path: Path) -> Dict[str, Any]:
    data = _load_json(path)
    if not isinstance(data, dict):
        raise ValueError("invalid batch")
    if "batch_id" not in data:
        data["batch_id"] = path.stem
    if "timestamp" not in data:
        data["timestamp"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    results = data.get("results") or []
    if "summary" not in data:
        ok = sum(1 for r in results if r.get("ok"))
        data["summary"] = {
            "total": len(results),
            "ok": ok,
            "failed": len(results) - ok,
            "with_citations": sum(1 for r in results if r.get("citations")),
        }
    return data


def _find_batch(batch_id: str) -> Optional[Path]:
    direct = EXPORT_DIR / f"{batch_id}.json"
    if direct.exists():
        return direct
    for p in _list_batch_files():
        try:
            data = _load_json(p)
            if data.get("batch_id") == batch_id:
                return p
        except Exception:
            continue
    return None


def _question_meta() -> Dict[str, Dict[str, str]]:
    meta: Dict[str, Dict[str, str]] = {}
    if QUESTIONS_PATH.exists():
        try:
            qdata = _load_json(QUESTIONS_PATH)
            for q in qdata.get("questions") or []:
                qid = q.get("id") or ""
                if qid:
                    meta[qid] = {
                        "question_id": qid,
                        "prompt": q.get("prompt") or qid,
                        "label": q.get("label") or q.get("title") or qid,
                    }
        except Exception:
            pass
    return meta


def _sort_questions(qids: List[str]) -> List[str]:
    rest = [q for q in qids if q not in QUESTION_ORDER]
    return [q for q in QUESTION_ORDER if q in qids] + sorted(rest)


def _sort_sites(sids: List[str]) -> List[str]:
    rest = [s for s in sids if s not in SITE_ORDER]
    return [s for s in SITE_ORDER if s in sids] + sorted(rest)


def _channel_brief(r: Dict[str, Any]) -> Dict[str, Any]:
    cites = r.get("citations") or []
    return {
        "site_id": r.get("site_id"),
        "site_name": r.get("site_name") or r.get("site_id"),
        "ok": bool(r.get("ok")),
        "citation_count": len(cites),
        "citation_domains": r.get("citation_domains") or [],
        "source": r.get("source"),
        "url": r.get("url"),
        "timestamp": r.get("timestamp"),
        "run_dir": r.get("run_dir"),
        "error": r.get("error"),
        "answer_preview": (r.get("answer") or r.get("error") or "")[:240],
        "answer_len": len(r.get("answer") or ""),
    }


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/batches")
def list_batches() -> List[Dict[str, Any]]:
    out = []
    for p in _list_batch_files():
        try:
            data = _load_batch(p)
        except Exception as e:
            out.append({"batch_id": p.stem, "file": p.name, "error": str(e)})
            continue
        out.append({
            "batch_id": data.get("batch_id") or p.stem,
            "file": p.name,
            "timestamp": data.get("timestamp"),
            "sites": data.get("sites") or [],
            "questions": data.get("questions") or [],
            "summary": data.get("summary") or {},
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
        })
    return out


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str) -> Dict[str, Any]:
    path = _find_batch(batch_id)
    if not path:
        raise HTTPException(status_code=404, detail="batch not found")
    try:
        return _load_batch(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/latest")
def latest_batch() -> Dict[str, Any]:
    files = _list_batch_files()
    if not files:
        raise HTTPException(status_code=404, detail="no exports")
    return _load_batch(files[0])


@app.get("/api/tree")
def question_tree() -> Dict[str, Any]:
    """题目 → 批次 → 渠道。"""
    qmeta = _question_meta()
    # question_id -> batch_id -> {batch meta, channels: {site_id: brief}}
    tree: Dict[str, Dict[str, Any]] = {}

    for p in _list_batch_files():
        try:
            data = _load_batch(p)
        except Exception:
            continue
        batch_id = data.get("batch_id") or p.stem
        ts = data.get("timestamp") or datetime.fromtimestamp(p.stat().st_mtime).isoformat()
        for r in data.get("results") or []:
            qid = r.get("question_id") or ""
            sid = r.get("site_id") or ""
            if not qid or not sid:
                continue
            if qid not in tree:
                meta = qmeta.get(qid) or {
                    "question_id": qid,
                    "prompt": r.get("prompt") or qid,
                    "label": qid,
                }
                # prefer prompt from result if meta missing
                if not meta.get("prompt") and r.get("prompt"):
                    meta["prompt"] = r["prompt"]
                tree[qid] = {
                    "question_id": qid,
                    "prompt": meta.get("prompt") or qid,
                    "label": meta.get("label") or qid,
                    "batches": {},
                }
            batches = tree[qid]["batches"]
            if batch_id not in batches:
                batches[batch_id] = {
                    "batch_id": batch_id,
                    "timestamp": ts,
                    "channels": {},
                }
            # 同批次同渠道保留更新/更完整的一条
            prev = batches[batch_id]["channels"].get(sid)
            brief = _channel_brief(r)
            brief["_full_key"] = f"{batch_id}::{sid}::{qid}"
            if prev is None or (brief["answer_len"] >= prev.get("answer_len", 0)):
                batches[batch_id]["channels"][sid] = brief
            # keep prompt from any result
            if r.get("prompt") and (
                not tree[qid]["prompt"] or tree[qid]["prompt"] == qid
            ):
                tree[qid]["prompt"] = r["prompt"]

    questions_out = []
    for qid in _sort_questions(list(tree.keys())):
        node = tree[qid]
        batch_list = []
        for bid, b in sorted(
            node["batches"].items(),
            key=lambda x: x[1].get("timestamp") or "",
            reverse=True,
        ):
            ch_map = b["channels"]
            channels = [ch_map[s] for s in _sort_sites(list(ch_map.keys()))]
            ok_n = sum(1 for c in channels if c.get("ok"))
            batch_list.append({
                "batch_id": bid,
                "timestamp": b.get("timestamp"),
                "channel_count": len(channels),
                "ok_count": ok_n,
                "cite_count": sum(1 for c in channels if c.get("citation_count")),
                "channels": channels,
            })
        questions_out.append({
            "question_id": qid,
            "prompt": node.get("prompt") or qid,
            "label": node.get("label") or qid,
            "batch_count": len(batch_list),
            "batches": batch_list,
        })

    return {
        "question_count": len(questions_out),
        "batch_file_count": len(_list_batch_files()),
        "questions": questions_out,
    }


@app.get("/api/result")
def get_result(batch_id: str, site_id: str, question_id: str) -> Dict[str, Any]:
    path = _find_batch(batch_id)
    if not path:
        raise HTTPException(status_code=404, detail="batch not found")
    data = _load_batch(path)
    for r in data.get("results") or []:
        if r.get("batch_id") and r.get("batch_id") != batch_id:
            pass
        if r.get("site_id") == site_id and r.get("question_id") == question_id:
            return r
    raise HTTPException(status_code=404, detail="result not found")
