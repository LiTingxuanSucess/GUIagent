#!/usr/bin/env python3
"""Resume an Excel sampling run through GEO, accepting only complete web evidence."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import fcntl
import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path
import sys
import time

import openpyxl

import geo_ask
from repair_sample_share import binding as share_binding, observed_candidate

GEO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GEO))
from cn_ai.share_links import is_public_share_url
from cn_ai.search_policy import outbound_prompt

TERMINAL = {"succeeded", "partial", "failed", "cancelled"}
SITES = ("kimi", "deepseek", "doubao", "qianwen", "wenxinyiyan", "yuanbao")
SEARCH_INSTRUCTION = "请联网搜索后回答，并提供搜索来源及链接。"


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_questions(path, count):
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book["300题"]
        rows = [{"question_id": str(row[0]), "category": row[1], "prompt": row[2],
                 "attribute": row[3], "excel_repeats": row[4], "excel_row": index}
                for index, row in enumerate(sheet.iter_rows(values_only=True), 1)
                if row and isinstance(row[0], str) and row[0].startswith("Q")
                and len(row) >= 5 and isinstance(row[2], str) and row[2].strip()]
    finally:
        book.close()
    if len(rows) < count:
        raise ValueError(f"题库只有 {len(rows)} 条问题，无法选取后 {count} 条")
    selected = rows[-count:]
    if len({q["question_id"] for q in selected}) != count:
        raise ValueError("题号重复")
    return selected


def quality(result, site):
    reasons = []
    if not result.get("ok") or not str(result.get("answer") or "").strip():
        reasons.append("missing_answer")
    citations = result.get("citation_details") or result.get("citations") or []
    urls = [c.get("url", "") if isinstance(c, dict) else str(c) for c in citations]
    if result.get("web_search_status") != "used" or not any(u.startswith(("https://", "http://")) for u in urls):
        reasons.append("missing_search_sources")
    internal = "yiyan" if site == "wenxinyiyan" else site
    if result.get("share_status") != "verified" or not is_public_share_url(internal, result.get("url", "")):
        reasons.append("missing_verified_share")
    if result.get("answer_origin") != "web":
        reasons.append("not_web_answer")
    return reasons


def samples_in_scope(state):
    limit = state.get('max_round')
    return [s for s in state['samples'] if limit is None or s.get('round', 1) <= limit]


def current_round(state):
    """Never open a later round while an earlier round has missing evidence."""
    return min((s.get('round', 1) for s in samples_in_scope(state) if not round_settled(state, s)), default=None)


def round_settled(state, sample):
    return sample['status'] == 'complete' or (sample['status'] == 'needs_clarification'
                                             and state.get('allow_clarification_advance', False))


def site_rounds(state):
    sites = state.get('sites') or sorted({s['site'] for s in state['samples']})
    return {site: min((s.get('round', 1) for s in samples_in_scope(state)
                       if s['site'] == site and not round_settled(state, s)), default=None) for site in sites}


def pending_in_current_round(state):
    if state.get('round_policy') == 'per_site':
        rounds = site_rounds(state)
        return [s for s in samples_in_scope(state) if s['status'] == 'pending' and s.get('round', 1) == rounds[s['site']]]
    active_round = current_round(state)
    return [s for s in samples_in_scope(state) if s['status'] == 'pending' and s.get('round', 1) == active_round]


def summary(state):
    state = {**state, 'samples': samples_in_scope(state)}
    counts = Counter(s["status"] for s in state["samples"])
    by_site = {}
    for site in state["sites"]:
        by_site[site] = dict(Counter(s["status"] for s in state["samples"] if s["site"] == site))
    by_round = {}
    for repeat in sorted({s.get('round', 1) for s in state['samples']}):
        rows = [s for s in state['samples'] if s.get('round', 1) == repeat]
        by_round[str(repeat)] = {'total': len(rows), 'counts': dict(Counter(s['status'] for s in rows))}
    active_round = current_round(state)
    platform_rounds = site_rounds(state)
    round_sites = {site: dict(Counter(s['status'] for s in state['samples']
                                     if s['site'] == site and s.get('round',1) == (
                                         platform_rounds[site] if state.get('round_policy') == 'per_site' else active_round))) for site in state['sites']}
    def brief(sample):
        return {key: sample[key] for key in ('question_id','round','site','status','job_id','quality_issues','error_code') if key in sample}
    active_samples = [brief(s) for s in state['samples'] if s['status'] in ('submitted','running','waiting_manual')]
    gaps = [s for s in state['samples'] if s['status'] in ('incomplete','retry_wait','share_pending','needs_clarification')]
    status = "running"
    if counts.get("complete", 0) == len(state["samples"]):
        status = "completed"
    elif all(round_settled(state, s) for s in state['samples']):
        status = 'completed_with_exceptions'
    elif counts.get("waiting_manual") and not (counts.get("running") or counts.get("submitted")):
        status = "waiting_manual"
    elif set(counts) <= {"complete", "incomplete"}:
        status = "needs_review"
    elif not (counts.get('running') or counts.get('submitted') or counts.get('waiting_manual')) and all(
            s.get('share_repair_exhausted') for s in gaps) and not pending_in_current_round(state):
        status = 'needs_review'
    return {"batch_id": state["batch_id"], "status": status, "updated_at": datetime.now().astimezone().isoformat(),
            "total": len(state["samples"]), "counts": dict(counts), "by_site": by_site,
            "attempts": sum(len(s.get("attempts", [])) for s in state["samples"]),
            "waiting_manual_sites": [site for site, rows in by_site.items() if rows.get("waiting_manual")],
            "search_instruction": state.get("search_instruction", ""),
            "concurrency": state.get("concurrency", 2), "site_isolation": state.get("site_isolation", False),
            'sampling_strategy': 'round_by_round', 'current_round': active_round, 'by_round': by_round,
            'round_policy': state.get('round_policy', 'global'), 'site_rounds': platform_rounds,
            'max_round': state.get('max_round'),
            'allow_clarification_advance': state.get('allow_clarification_advance', False),
            'processed': sum(round_settled(state, s) for s in state['samples']),
            'current_round_by_site': round_sites, 'active_samples': active_samples,
            'quality_gaps': [brief(s) for s in gaps[:12]], 'quality_gap_count': len(gaps)}


def export(out, state):
    save_json(out / "manifest.json", state)
    current = summary(state)
    save_json(out / "progress.json", current)
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "采样结果"
    sheet.append(["题号", "轮次", "平台", "问题原文", "类别", "状态", "正文", "搜索来源数",
                  "分享链接", "缺项或错误", "任务ID", "采样时间", "完整结果文件", "实际发送文本",
                  "搜索控件状态", "搜索控件已确认", "实际搜索状态", "已结束尝试数",
                  "分享复核证据", "分享复核次数", "分享复核错误"])
    sources = book.create_sheet("搜索来源")
    sources.append(["题号", "轮次", "平台", "序号", "标题", "来源链接"])
    attempts = book.create_sheet("尝试记录")
    attempts.append(["题号", "轮次", "平台", "尝试序号", "状态", "任务ID", "缺项或错误", "完整结果文件"])
    for sample in state["samples"]:
        result = sample.get("result") or {}
        citations = result.get("citation_details") or result.get("citations") or []
        # Full answers and all raw metadata remain in each UTF-8 JSON file.
        answer = str(result.get("answer") or "")
        if len(answer) > 32000:
            answer = answer[:32000] + "\n[正文较长，完整内容见结果 JSON]"
        row = [sample["question_id"], sample["round"], sample["site"], sample["prompt"],
               sample["category"], sample["status"], answer, len(citations), result.get("url", ""),
               ",".join(sample.get("quality_issues") or []) or sample.get("error_code", ""),
               sample.get("job_id", ""), result.get("timestamp", ""), sample.get("result_file", ""),
               sample.get("submitted_prompt", sample["prompt"]), result.get("web_search_setting", ""),
               result.get("web_search_setting_confirmed", ""), result.get("web_search_status", ""),
               len(sample.get("attempts", [])), sample.get('share_proof_file', ''),
               len(sample.get('share_repairs', [])), sample.get('share_repair_error', '')]
        sheet.append(row)
        for index, attempt in enumerate(sample.get("attempts", []), 1):
            attempts.append([sample["question_id"], sample["round"], sample["site"], index,
                             attempt.get("status", ""), attempt.get("job_id", ""),
                             ",".join(attempt.get("quality_issues", [])), attempt.get("result_file", "")])
        for index, citation in enumerate(citations, 1):
            c = citation if isinstance(citation, dict) else {"url": citation}
            sources.append([sample["question_id"], sample["round"], sample["site"], index,
                            c.get("title", ""), c.get("url", "")])
    for tab in book:
        tab.freeze_panes = "A2"
        tab.auto_filter.ref = tab.dimensions
        # Preserve platform answers as literal text, including strings starting '='.
        for row in tab:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
    temporary = out / "采样结果.tmp.xlsx"
    book.save(temporary)
    os.replace(temporary, out / "采样结果.xlsx")
    print(json.dumps(current, ensure_ascii=False), flush=True)


def initialize(args, out):
    source = args.excel.resolve()
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = out / "manifest.json"
    instruction = SEARCH_INSTRUCTION if getattr(args, "require_search", False) else ""
    if manifest.exists():
        state = json.loads(manifest.read_text(encoding="utf-8"))
        if state["source_sha256"] != fingerprint or state["repeats"] != args.repeats or state["count"] != args.count:
            raise ValueError("已有批次的 Excel、选题数或轮次不同，请使用另一个输出目录")
        if state.get("search_instruction", "") != instruction:
            raise ValueError("联网搜索要求不同，请新建批次，避免混合两种采样口径")
    else:
        state = {"version": 2, "batch_id": args.batch_id, "source": str(source), "source_sha256": fingerprint,
                 "search_instruction": instruction,
                 "count": args.count, "repeats": args.repeats, "sites": [], "samples": []}
    questions = read_questions(source, args.count)
    state['concurrency'] = getattr(args, 'concurrency', 2)
    state['site_isolation'] = getattr(args, 'site_isolation', False)
    state['sampling_strategy'] = 'round_by_round'
    state['round_policy'] = getattr(args, 'round_policy', None) or state.get('round_policy', 'global')
    limit = getattr(args, 'max_round', None)
    state['max_round'] = state.get('max_round') if limit is None else limit
    if state['max_round'] is not None and not 1 <= state['max_round'] <= state['repeats']:
        raise ValueError('执行轮次上限必须在 1 和批次总轮次之间')
    allow = getattr(args, 'allow_clarification_advance', None)
    state['allow_clarification_advance'] = state.get('allow_clarification_advance', False) if allow is None else allow
    known = {(s["question_id"], s["round"], s["site"]) for s in state["samples"]}
    # Sites may be added later without resubmitting already accepted samples.
    for site in args.sites:
        if site not in state["sites"]:
            state["sites"].append(site)
    for repeat in range(1, args.repeats + 1):
        for question in questions:
            for site in args.sites:
                identity = (question["question_id"], repeat, site)
                if identity in known:
                    continue
                submitted_prompt = outbound_prompt(site, question['prompt'])
                state["samples"].append({**question, "submitted_prompt": submitted_prompt,
                                         "api_prompt": question['prompt'], "prompt_policy_version": 3,
                                         "round": repeat, "site": site, "status": "pending",
                                         "request_id": f'{state["batch_id"]}-{question["question_id"]}-r{repeat}-{site}'})
                known.add(identity)
    return state


def prepare_submission(sample):
    """Keep original identity separate from wire text, including on legacy resumes."""
    if sample.get('prompt_policy_version', 0) < 3:
        # Old ids may already refer to an API request carrying a generic
        # suffix. A new stable id avoids changing that request's meaning.
        sample['request_id'] += '-search-v3'
        sample.update(prompt_policy_version=3, api_prompt=sample['prompt'],
                      submitted_prompt=outbound_prompt(sample['site'], sample['prompt']))
    return {'site': sample['site'], 'prompt': sample['api_prompt'], 'request_id': sample['request_id']}


def retain_share_only_samples(state):
    """Migrate old retry gaps without submitting another question."""
    for sample in state['samples']:
        if (sample['status'] in ('pending', 'retry_wait', 'incomplete')
                and sample.get('quality_issues') == ['missing_verified_share']
                and sample.get('attempts') and sample.get('result')):
            last = sample['attempts'][-1]
            sample.update(status='share_pending', job_id=last['job_id'], request_id=last['request_id'])
        if (sample['status'] == 'share_pending' and sample.get('share_repairs')
                and sample.get('share_repair_version', 1) < 3
                and sample.get('share_repair_error') in ('share_verify_process_failed', 'share_verify_timeout', 'share_candidate_missing', 'answer_not_visible')
                and observed_candidate(sample.get('result') or {}, sample['site'])):
            sample.update(share_repair_version=3, share_repair_budget_start=len(sample.get('share_repairs', [])),
                          share_repair_exhausted=False, share_retry_at=0,
                          share_repair_note='保留旧失败记录，使用独立无登录上下文与完整异步加载窗口补验')


def clarification_answer(result):
    """Recognize an explicit request for missing comparison inputs, not a search failure."""
    answer = str(result.get('answer') or '')[:300]
    return (result.get('ok') is True and result.get('answer_origin') == 'web'
            and result.get('web_search_status') != 'used'
            and any(text in answer for text in ('哪两款', '两款车型', '具体车型'))
            and any(text in answer[:180] for text in ('请告诉我', '请提供', '还没说', '无法有效搜索', '需要先知道')))


def retain_clarification_samples(state):
    for sample in state['samples']:
        if (sample['status'] in ('incomplete', 'retry_wait', 'pending') and len(sample.get('attempts', [])) >= 2
                and sample.get('quality_issues') == ['missing_search_sources']
                and clarification_answer(sample.get('result') or {})):
            sample.update(status='needs_clarification', disposition='question_requires_input')
            last = sample['attempts'][-1]
            sample.update(job_id=last['job_id'], request_id=last['request_id'])


def finish_share_repair(sample, proof, proof_file):
    """A proof is usable only for this exact stored answer and question."""
    if proof.get('binding') != share_binding(sample['result'], sample['site']):
        proof = {'ok': False, 'error_code': 'share_proof_mismatch'}
    sample.setdefault('share_repairs', []).append({'proof_file': proof_file, 'ok': proof.get('ok') is True,
                                                  'error_code': proof.get('error_code', ''), 'at': time.time()})
    if proof.get('ok') is True:
        result = dict(sample['result'], url=proof.get('url', ''), share_status='verified', share_error_code='')
        issues = quality(result, sample['site'])
        if not issues:
            result['quality_issues'] = [x for x in result.get('quality_issues', []) if not x.startswith('share_')]
            if str(result.get('optional_stage_error_code', '')).startswith('share_'):
                result.pop('optional_stage_error_code', None)
            sample.update(result=result, status='complete', quality_issues=[], error_code='', share_proof_file=proof_file)
            sample.pop('share_repair_error', None)
            sample.pop('share_repair_exhausted', None)
            return
    attempts = len(sample['share_repairs'])
    sample['share_repair_error'] = proof.get('error_code') or 'share_verify_failed'
    sample['share_repair_exhausted'] = attempts - sample.get('share_repair_budget_start', 0) >= 3 or proof.get('error_code') == 'share_candidate_missing'
    sample['share_retry_at'] = time.time() + min(600, 60 * 2 ** min(attempts, 4))


def poll_share_repairs(state, out, processes):
    """Use a separate bounded process; public verification never blocks ask slots."""
    changed = False
    for key, (process, sample, proof_path, started) in list(processes.items()):
        timed_out = process.poll() is None and time.monotonic() - started >= 55
        if process.poll() is None and time.monotonic() - started < 55:
            continue
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        try:
            proof = json.loads(proof_path.read_text(encoding='utf-8'))
            if proof.get('finished') is False:
                proof.update(ok=False, error_code='share_verify_timeout' if timed_out else 'share_verify_process_failed')
        except (OSError, ValueError):
            proof = {'binding': share_binding(sample['result'], sample['site']), 'ok': False,
                     'error_code': 'share_verify_timeout' if timed_out else 'share_verify_process_failed'}
        if proof.get('ok') is not True:
            proof.update(process_exit_code=process.returncode)
            save_json(proof_path, proof)
        finish_share_repair(sample, proof, str(proof_path.relative_to(out)))
        del processes[key]
        changed = True
    if not processes:
        due = next((s for s in samples_in_scope(state) if s['status'] == 'share_pending'
                    and not s.get('share_repair_exhausted') and time.time() >= s.get('share_retry_at', 0)), None)
        if due:
            proof_path = out / 'results' / (due['request_id'] + f'-share-{len(due.get("share_repairs", [])) + 1}.json')
            proof_path.unlink(missing_ok=True)
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('repair_sample_share.py')),
                                        '--result-file', str(out / due['result_file']), '--site', due['site'],
                                        '--output', str(proof_path)], start_new_session=True,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes[due['request_id']] = (process, due, proof_path, time.monotonic())
    return changed


def finish_sample(sample, job, *, max_attempts, retry_delay):
    result = next((r for r in job.get("results", []) if r.get("site_id") == sample["site"]), {})
    issues = quality(result, sample["site"])
    result_file = "results/" + sample["request_id"] + ".json"
    attempt = {"request_id": sample["request_id"], "job_id": sample["job_id"], "status": job["status"],
               "quality_issues": issues, "result_file": result_file}
    sample.setdefault("attempts", []).append(attempt)
    sample.update(status="complete" if not issues else "incomplete", result=result,
                  quality_issues=issues, error_code=job.get("error_code", ""), result_file=result_file)
    if issues == ['missing_verified_share'] and job['status'] in ('succeeded', 'partial'):
        sample['status'] = 'share_pending'
        return
    if (issues == ['missing_search_sources'] and len(sample['attempts']) >= 2
            and job['status'] == 'succeeded' and clarification_answer(result)):
        sample.update(status='needs_clarification', disposition='question_requires_input')
        return
    # A known terminal answer may be sampled again in a fresh conversation.
    # Never resubmit an uncertain or manually paused send. Every attempt stays
    # in the audit trail, including answers rejected for missing evidence.
    if issues and job["status"] != "cancelled" and len(sample["attempts"]) < max_attempts:
        original = sample["attempts"][0]["request_id"]
        sample.update(status="retry_wait", retry_at=time.time() + retry_delay,
                      request_id=original + f'-a{len(sample["attempts"]) + 1}')
        sample.pop("job_id", None)


def run(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "results").mkdir(exist_ok=True)
    with (out / "runner.lock").open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = initialize(args, out)
        samples = samples_in_scope(state)
        retain_share_only_samples(state)
        retain_clarification_samples(state)
        share_processes = {}
        export(out, state)
        if args.prepare:
            return
        ak, sk = geo_ask.load_aksk(args.keys)

        def call(method, path, body=None):
            response = geo_ask.call(args.base, method, path, ak, sk, body, request_timeout=15)
            if response.get("http_status") not in (200, 202):
                raise RuntimeError(f'GEO HTTP {response.get("http_status")} at {path}')
            return response

        while True:
            changed = False
            try:
                changed = poll_share_repairs(state, out, share_processes)
                for sample in samples:
                    if sample["status"] == "retry_wait" and time.time() >= sample.get("retry_at", 0):
                        sample["status"] = "pending"
                        changed = True
                    if sample["status"] not in ("submitted", "running", "waiting_manual"):
                        continue
                    job = call("GET", "/api/v1/jobs/" + sample["job_id"])
                    save_json(out / "results" / (sample["request_id"] + ".json"), job)
                    status = job.get("status")
                    if status in TERMINAL:
                        finish_sample(sample, job, max_attempts=args.max_attempts, retry_delay=args.retry_delay)
                        changed = True
                    elif status == "waiting_manual" and sample["status"] != "waiting_manual":
                        sample["status"] = "waiting_manual"
                        changed = True
                    elif status == "running" and sample["status"] in ("submitted", "waiting_manual"):
                        sample["status"] = "running"
                        changed = True
                    elif status == "queued" and sample["status"] != "submitted":
                        sample["status"] = "submitted"
                        changed = True

                active = sum(s["status"] in ("submitted", "running") for s in samples)
                blocked = {s["site"] for s in samples if s["status"] == "waiting_manual"}
                # Manual workflow holds the entire account browser. Do not
                # keep adding jobs behind a browser that needs human action.
                manual_holds = sum(s["status"] == "waiting_manual" for s in samples)
                capacity = args.concurrency if args.site_isolation else max(0, args.concurrency - manual_holds)
                active_sites = {s["site"] for s in samples if s["status"] in ("submitted", "running")}
                for sample in pending_in_current_round(state):
                    if active >= capacity:
                        break
                    if sample["status"] != "pending" or sample["site"] in blocked or sample['site'] in active_sites:
                        continue
                    if blocked and sample["site"] in args.exclusive_site:
                        continue
                    if active_sites.intersection(args.exclusive_site) or (active and sample["site"] in args.exclusive_site):
                        break
                    payload = prepare_submission(sample)
                    save_json(out / 'manifest.json', state)
                    job = call("POST", "/api/v1/ask", payload)
                    sample.update(job_id=job["job_id"], status="submitted")
                    # Commit immediately. The stable request_id also covers a
                    # lost HTTP response or a crash before writing this record.
                    save_json(out / "manifest.json", state)
                    changed = True
                    active += 1
                    active_sites.add(sample["site"])
                if changed:
                    export(out, state)
                else:
                    save_json(out / "progress.json", summary(state))
                if all(round_settled(state, s) for s in samples):
                    return
                unfinished = {s["status"] for s in samples} - {"complete", "incomplete"}
                if not unfinished:
                    print("本轮采样结束：缺项未计入完成，请检查尝试记录。", flush=True)
                    return
            except (OSError, RuntimeError) as error:
                print(f"暂时无法查询/提交，保留原 request_id 继续：{type(error).__name__}: {error}", flush=True)
            time.sleep(args.poll)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--excel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sites", nargs="+", choices=SITES, required=True)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument('--max-round', type=int, default=None,
                        help='仅执行到指定轮次；保留后续轮次记录，恢复同一批次时继续沿用')
    parser.add_argument("--batch-id", default="auto-20261022")
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--keys", type=Path, default=GEO / "server/.aksk.json")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument('--site-isolation', action='store_true', help='各平台独立运行，人工待办只阻塞本平台；需服务端同时启用平台锁隔离')
    parser.add_argument('--round-policy', choices=('global', 'per_site'), default=None,
                        help='global 等待六平台本轮完成；per_site 按各平台自己的轮次推进')
    parser.add_argument('--allow-clarification-advance', action=argparse.BooleanOptionalAction, default=None,
                        help='允许已单列的澄清答复参与轮次推进；不计入合格结果数')
    parser.add_argument("--poll", type=float, default=5)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--require-search", action="store_true", help="按用户要求统一追加联网搜索及来源要求")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=60)
    parser.add_argument("--exclusive-site", choices=SITES, action="append", default=[],
                        help="该平台独占批量发问槽位，保证优先复用当前唯一可用账号")
    args = parser.parse_args()
    if min(args.count, args.repeats, args.concurrency, args.poll, args.max_attempts, args.retry_delay) <= 0:
        parser.error("题数、轮次、并发、轮询间隔必须大于 0")
    if args.max_round is not None and not 1 <= args.max_round <= args.repeats:
        parser.error('执行轮次上限必须在 1 和批次总轮次之间')
    run(args)


if __name__ == "__main__":
    main()
