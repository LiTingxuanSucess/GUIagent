#!/usr/bin/env python3
"""Send a durable sampling progress report; intended for launchd every 600s."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import fcntl
import json
from pathlib import Path
import time

from request_monitor import LABELS, SITES, atomic_json, send_feishu, stamp

ISSUES = {"missing_answer": "缺正文", "missing_search_sources": "缺确认的搜索来源",
          "missing_verified_share": "缺已验证的分享链接", "not_web_answer": "非网页回答"}
ACTIVE = {"submitted", "running", "waiting_manual"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def report(config, previous, now):
    out = Path(config["batch_dir"])
    state = read_json(out / "manifest.json")
    heartbeat = read_json(out / "progress.json")
    counts = Counter(s["status"] for s in state["samples"])
    total, done = len(state["samples"]), counts["complete"]
    from sample_excel import round_settled
    finished = bool(total) and all(round_settled(state, sample) for sample in state['samples'])
    same_batch = previous.get("batch_id") == state["batch_id"]
    baseline = previous.get("complete", done) if same_batch else done
    last_change = previous.get("last_progress_at", now) if same_batch else now
    if done != baseline:
        last_change = now
    lines = ["GEO 采样任务进展", datetime.fromtimestamp(now).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
             f"批次：{state['batch_id']}",
             f"完成：{done}/{total}（{done / total:.2%}）；较上次通知新增 {done-baseline} 次" if total else "题库为空",
             "计入标准：正文 + 实际搜索来源 + 已验证的分享链接。",
             f"状态：执行/排队 {counts['running']+counts['submitted']}，待人工 {counts['waiting_manual']}，"
             f"待运行 {counts['pending']}，待重试 {counts['retry_wait']}，待补分享 {counts['share_pending']}，"
             f"需补充信息 {counts['needs_clarification']}，缺项 {counts['incomplete']}。"]
    if state.get('sampling_strategy') == 'round_by_round':
        active_round = min((s.get('round',1) for s in state['samples'] if s['status']!='complete'),default=None)
        if active_round is not None:
            rows=[s for s in state['samples'] if s.get('round',1)==active_round]
            if state.get('round_policy') == 'per_site':
                from sample_excel import site_rounds
                rounds = site_rounds(state)
                lines.append('各平台独立推进轮次：' + '；'.join(
                    f'{SITES.get(site,site)} 第{value}遍' if value is not None else f'{SITES.get(site,site)} 已处理完'
                    for site,value in rounds.items()))
            else:
                lines.append(f"当前第 {active_round} 遍：{sum(s['status']=='complete' for s in rows)}/{len(rows)} 次平台采样；"
                             "本轮 100 题在六个平台完成后，才进入下一遍。")
        if state.get('allow_clarification_advance'):
            lines.append('需补充信息的澄清答复单列为已处理异常，可推进轮次，不计入合格完成数。')
    if same_batch and done == baseline and not finished:
        lines.append(f"停滞：自监控记录的上次新增起，已 {int((now-last_change)/60)} 分钟没有新增完成。")
    age = max(0, now-stamp(heartbeat.get("updated_at")))
    if age > 120:
        lines.append(f"异常：采样心跳已 {int(age/60)} 分钟未更新，控制器可能已退出。")
    lines.append("平台完成数：" + "；".join(
        f"{SITES.get(site,site)} {sum(s['status']=='complete' for s in state['samples'] if s['site']==site)}/"
        f"{sum(s['site']==site for s in state['samples'])}" for site in state["sites"]))
    lines.append("当前任务与卡点：")
    active = [s for s in state["samples"] if s["status"] in ACTIVE]
    holds = 0
    for sample in active[:20]:
        jid = sample.get("job_id", "")
        prefix = f"{SITES.get(sample['site'],sample['site'])} {sample['question_id']} 第{sample['round']}遍（任务 {jid}）"
        try:
            job = read_json(Path(config["jobs_dir"]) / (jid + ".json"))
        except (OSError, ValueError):
            lines.append(prefix + "：任务记录不可读，状态尚未确认。")
            continue
        manual = (job.get("manual_states") or {}).get(sample["site"], {})
        if manual.get('assist_status'):
            assist_code = ''
            if manual.get('assist_call_id'):
                try:
                    call = read_json(Path(config['jobs_dir']).parent / 'cursor-calls' / (manual['assist_call_id']+'.json'))
                    assist_code = call.get('process_error_code') or call.get('error_code','')
                except (OSError,ValueError):
                    pass
            lines.append(prefix + f"：Cursor 辅助 {manual['assist_status']}；" + LABELS.get(assist_code,assist_code)
                         + (f" [{assist_code}]" if assist_code else ''))
        if manual.get('auto_retry_at') and job.get('status') == 'queued':
            lines.append(prefix + f"：繁忙刷新重试 {manual.get('refresh_attempts',0)} 次；下次重试 {manual['auto_retry_at']}。")
        if job.get("status") == "waiting_manual":
            holds += 1
            code = manual.get("error_code", "operator_required")
            since = stamp(job.get("started_at") or job.get("created_at")) or now
            lines.append(prefix + f"：待处理，任务已持续 {int(max(0,now-since)/60)} 分钟；"
                         f"{LABELS.get(code,code)} [{code}]；阶段 {manual.get('send_state','未知')}；"
                         + manual.get("reason", "请检查原页面") + "。")
            if code == "platform_risk_control":
                lines.append("处理：在该账号原 Chrome 页面完成安全验证，再点击人工处理页的“已完成验证，继续执行”。")
            elif manual.get("send_state") == "sending":
                lines.append("处理：核对原问题是否已提交及回答是否生成，确认原轮次后续采；发送状态未确认时不重复发送。")
        else:
            since = stamp(job.get("started_at") or job.get("created_at")) or now
            lines.append(prefix + f"：{job.get('status','未知')}，已 {int(max(0,now-since)/60)} 分钟。")
    if holds:
        if state.get('site_isolation'):
            lines.append(f"平台隔离已开启（最多 {state.get('concurrency',6)} 路）：{holds} 个待处理平台暂停，其余平台继续运行。")
        else:
            lines.append(f"资源卡点：{holds} 个待处理任务保留账号浏览器；占满可用账号后其余平台也会排队。")
    elif not active and done < total:
        lines.append("当前没有执行中的任务；请检查重试队列、缺项记录和采样控制器。")
    rejected = [s for s in state["samples"] if s["status"] != "complete" and s.get("quality_issues")]
    for sample in rejected[:6]:
        issues = "、".join(ISSUES.get(v, v) for v in sample["quality_issues"])
        lines.append(f"缺项：{SITES.get(sample['site'],sample['site'])} {sample['question_id']} 第{sample['round']}遍：{issues}。")
    if finished:
        lines.append("本批次已全部完成，后续停止重复推送完成通知。" if done == total else
                     f"本批次采样已结束：合格 {done} 条，需补充信息 {counts['needs_clarification']} 条单列，不计合格数。")
    if config.get("manual_url"):
        lines.append("人工处理：" + config["manual_url"])
    next_state = {"batch_id": state["batch_id"], "complete": done, "total": total,
                  "last_progress_at": last_change, "last_sent_at": now, "finished": finished}
    return "\n".join(lines), next_state


def tick(config, state_path, *, sender=send_feishu, now=None, force=False):
    now = time.time() if now is None else now
    try:
        previous = read_json(state_path)
    except (OSError, ValueError):
        previous = {}
    # launchd RunAtLoad and StartInterval may coincide. Only acknowledged
    # deliveries advance the baseline or suppress the next report.
    if not force and now-previous.get("last_sent_at", 0) < 590:
        return False
    try:
        text, next_state = report(config, previous, now)
    except (OSError, ValueError, KeyError) as error:
        text = "GEO 采样任务监控异常：无法读取批次或进度文件（" + type(error).__name__ + "），当前完成数未确认。"
        sender(config, text)
        return True
    if previous.get("finished") and previous.get("batch_id") == next_state["batch_id"] and next_state["finished"]:
        return False
    sender(config, text)
    atomic_json(state_path, next_state)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix(".lock").open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            from sampling_alert import deliver
            try:
                deliver(args.config)
            except Exception as error:
                print('Recovery alert retry deferred: ' + type(error).__name__, flush=True)
            sent = tick(read_json(args.config), args.state, force=args.force)
            print("Feishu sampling report accepted (code=0)" if sent else "No report due", flush=True)
        except Exception as error:
            # Neither config, webhook URLs nor raw HTTP responses enter logs.
            print("Sampling report failed: " + type(error).__name__, flush=True)
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
