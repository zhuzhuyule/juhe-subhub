#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""subhub worker —— 单轮任务：同步固定资源 → 检测 → 生成订阅与静态面板。

没有 HTTP 服务、没有 SQLite：全部状态在 JSON 文件里，进程跑完即退出。
适合 GitHub Action / cron / 一次性容器（"worker 模式"）。

用法:
  python3 run_once.py                          # 输出到 docs/
  python3 run_once.py --deep --workers 6
  python3 run_once.py --out /tmp/site --strict # 严格模式: 未检测过的源不进订阅

状态文件（都在 state/ 下, 按「谁写」拆分, 每个文件只有一个写者）:
  sources.json    机器只 append  [{id,name,url,kind,origin,added_at}]
  overrides.json  只有人写         {url: {sort_order,enabled,user_ok}}
  results.json    只有机器写       {url: {last_ok,last_verdict,play,...}}
  feeds.json      只有人写         [{name,url,enabled}]
  history.jsonl   只有机器写       每行一轮: {ts,total,ok,unreachable,items:[...]}
"""
import argparse
import concurrent.futures
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import checker          # noqa: E402
import merge            # noqa: E402
import static_page      # noqa: E402
from feed_sync import extract_feed_urls  # noqa: E402

STATE_DIR = os.path.join(HERE, "state")
REPO_ROOT = HERE                     # 扁平布局: 仓库根就是代码目录
HISTORY_KEEP = 120          # 保留最近 120 轮


# ---------------- 状态读写 ----------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------- 状态三分：每个文件只有一个写者 ----------------
# 之前所有东西都塞在 sources.json 里, 机器每轮整份重写, 人手改的 sort_order/enabled
# 会跟机器写的 last_ok 混在同一个 diff 里, 冲突和误覆盖都从这里来。
# 现在按「谁写」拆开, 冲突面归零:
#
#   sources.json    机器 append 新源(id/name/url/kind/origin/added_at), 不删不改
#   overrides.json  只有人写 —— 顺序、启停、置顶全在这
#   results.json    只有机器写 —— 检测结果、试播
#   history.jsonl   只有机器写 —— 每轮汇总

CFG_FIELDS = ("id", "name", "url", "kind", "origin", "added_at")
OV_FIELDS = ("sort_order", "enabled", "user_ok")
RES_FIELDS = ("last_ok", "last_ts", "last_verdict", "last_unreachable",
              "fail_streak", "degraded", "http", "type", "sites", "lives",
              "parses", "jar", "ms", "play")

DEFAULTS = {"sort_order": None, "enabled": True, "user_ok": False}


def load_state():
    """三份文件合成一份运行时视图: 身份 + 人工覆盖 + 上次结果。"""
    srcs = load_json(os.path.join(STATE_DIR, "sources.json"), [])
    if not srcs:
        srcs = bootstrap_from_db()
    ov = load_json(os.path.join(STATE_DIR, "overrides.json"), {})
    res = load_json(os.path.join(STATE_DIR, "results.json"), {})
    out = []
    for s in srcs:
        d = dict(s)
        o = ov.get(s["url"]) or {}
        d["sort_order"] = o.get("sort_order")
        if d["sort_order"] is None:
            d["sort_order"] = s.get("id") or 0      # 没排过的按加入顺序
        d["enabled"] = o.get("enabled", True)
        d["user_ok"] = bool(o.get("user_ok", False))
        for k in RES_FIELDS:
            if k in (res.get(s["url"]) or {}):
                d[k] = res[s["url"]][k]
        out.append(d)
    return out


def save_state(sources):
    """写回两份机器文件。overrides.json 一个字节都不碰。"""
    save_json(os.path.join(STATE_DIR, "sources.json"),
              [{k: s.get(k) for k in CFG_FIELDS} for s in sources])
    save_json(os.path.join(STATE_DIR, "results.json"),
              {s["url"]: {k: s[k] for k in RES_FIELDS if k in s} for s in sources})



def bootstrap_from_db():
    """state/sources.json 不存在但本地有 subhub.db 时, 从库里导出一次。"""
    dbp = os.path.join(HERE, "subhub.db")
    if not os.path.exists(dbp):
        return []
    import sqlite3
    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    srcs = [dict(r) for r in c.execute(
        "SELECT * FROM sources ORDER BY sort_order, id").fetchall()]
    feeds = [dict(r) for r in c.execute("SELECT * FROM feeds").fetchall()]
    out = []
    for s in srcs:
        out.append({"id": s["id"], "name": s["name"], "url": s["url"],
                    "kind": s.get("kind") or "manual",
                    "origin": s.get("origin"), "sort_order": s.get("sort_order") or s["id"],
                    "enabled": bool(s.get("enabled", 1)),
                    "user_ok": bool(s.get("user_ok", 0)),
                    "last_ok": None, "fail_streak": 0, "degraded": False})
    save_state(out)
    # 顺序/启停/置顶属于人工文件, 迁移时一并落成 overrides.json
    save_json(os.path.join(STATE_DIR, "overrides.json"),
              {s["url"]: {k: s[k] for k in OV_FIELDS if k in s} for s in out})
    if feeds:
        save_json(os.path.join(STATE_DIR, "feeds.json"),
                  [{"name": f["name"], "url": f["url"], "enabled": bool(f.get("enabled", 1))}
                   for f in feeds])
    print("从 subhub.db 导出 %d 个源 / %d 个固定资源" % (len(out), len(feeds)))
    return out


# ---------------- 固定资源同步 ----------------

def sync_feeds(sources, feeds, timeout=20):
    """拉取每个固定资源, 新 URL 入队。返回新增数。"""
    known = {s["url"] for s in sources}
    next_id = max([s.get("id") or 0 for s in sources] + [0]) + 1
    added = 0
    for f in feeds:
        if not f.get("enabled", True):
            continue
        st, body = checker.fetch(f["url"], timeout=timeout)
        if st != 200 or not body:
            print("  [feed] %s 拉取失败: %s" % (f.get("name"), st))
            continue
        try:
            items = extract_feed_urls(body, f["url"])
        except Exception as e:                       # noqa: BLE001
            print("  [feed] %s 解析失败: %s" % (f.get("name"), e))
            continue
        n = 0
        for name, url in items:
            if url in known:
                continue
            sources.append({"id": next_id, "name": (name or url[:24]), "url": url,
                            "kind": "feed", "origin": f.get("name"),
                            "added_at": int(time.time())})
            known.add(url)
            next_id += 1
            n += 1
        added += n
        print("  [feed] %s → 新增 %d 条" % (f.get("name"), n))
    return added


# ---------------- 检测 ----------------

def run_checks(sources, deep, workers, timeout):
    """并发检测。连接层不可达 = 本机网络问题, 不改动源状态(last-good-wins)。"""
    targets = [s for s in sources if s.get("enabled", True)]
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(checker.check_source, s["url"], deep, timeout): s
                for s in targets}
        for fut in concurrent.futures.as_completed(futs):
            s = futs[fut]
            try:
                r = fut.result()
            except Exception as e:                   # noqa: BLE001
                r = {"url": s["url"], "verdict": "✗ 检测异常(%s)" % type(e).__name__,
                     "http": "-", "sites": None, "lives": None, "jar": None, "ms": None}
            results[s["url"]] = r

    now = int(time.time())
    unreachable = 0
    for s in targets:
        r = results[s["url"]]
        unreachable_this = "本机不可达" in r.get("verdict", "")
        s["last_ts"] = now
        s["last_verdict"] = r.get("verdict", "")
        if unreachable_this and not s.get("user_ok"):
            unreachable += 1
            s["last_unreachable"] = True
            continue                                  # 不动 last_ok / fail_streak
        s["last_unreachable"] = False
        ok_now = r.get("verdict", "").startswith("✓")
        s["last_ok"] = 1 if ok_now else 0
        s["sites"], s["lives"], s["jar"] = r.get("sites"), r.get("lives"), r.get("jar")
        s["ms"] = r.get("ms")
        if s.get("user_ok"):
            s["fail_streak"], s["degraded"] = 0, False
        else:
            s["fail_streak"] = 0 if ok_now else (s.get("fail_streak") or 0) + 1
            s["degraded"] = s["fail_streak"] >= 3
    return results, unreachable


# ---------------- 生成产物 ----------------

def pick_master(sources, master_url=None):
    """主源: 显式指定 > 排序第一个有效/确认的源。"""
    if master_url:
        for s in sources:
            if s["url"] == master_url and s.get("enabled", True):
                return s
    for s in sorted(sources, key=merge.rank_key):
        if not s.get("enabled", True):
            continue
        if s.get("user_ok") or s.get("last_ok") == 1:
            return s
    return None


def build_tvbox(sources, master=None, want_lives=True, want_parses=True):
    """主源必须能真正解析出 spider jar —— 按排序依次尝试, 跳过拿不到配置的源。"""
    cands = []
    if master is not None:
        cands.append(master)
    for s in sorted(sources, key=lambda x: (x.get("sort_order") or 0, x.get("id") or 0)):
        if s.get("enabled", True) and (s.get("user_ok") or s.get("last_ok") == 1) \
                and s not in cands:
            cands.append(s)
    if not cands:
        return None, {"error": "没有可用作主源的订阅"}
    master, mcfg = None, None
    tried = []
    for s in cands:
        cfg, _, _ = checker.load_config(s["url"])
        tried.append(s.get("name"))
        if isinstance(cfg, dict):
            master, mcfg = s, cfg
            break
    if master is None:
        return None, {"error": "候选主源都无法解析: %s" % ", ".join(tried[:4])}
    others = []
    for s in sources:
        if s["url"] == master["url"] or not s.get("enabled", True):
            continue
        c2, _, _ = checker.load_config(s["url"])
        others.append({"url": s["url"], "cfg": c2})
    out, stats = merge.merge_single(dict(master, cfg=mcfg), others, want_lives, want_parses)
    stats["master_tried"] = tried
    return out, stats


def uptime_from_history(history, url, window=30):
    """最近 N 轮的 0/1 序列 + 百分比(只统计真正检测过的轮次)。"""
    seq = []
    for run in history[-window:]:
        for it in run.get("items") or []:
            if it.get("url") == url and it.get("ok") is not None:
                seq.append(1 if it["ok"] else 0)
    pct = round(sum(seq) / len(seq) * 100) if seq else None
    return seq, pct


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="subhub 单轮 worker")
    ap.add_argument("--out", default=os.path.join(REPO_ROOT, "docs"),
                    help="产物目录(默认 <repo>/docs)")
    ap.add_argument("--deep", action="store_true", help="深度检测(jar/直播/接口抽样)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--strict", action="store_true",
                    help="未检测过的源不进订阅(默认: 未知≠坏, 首次运行也能出完整订阅)")
    ap.add_argument("--no-feeds", action="store_true", help="跳过固定资源同步")
    ap.add_argument("--play", action="store_true",
                    help="L4 试播: 对有效源取真实 m3u8 验证可播(慢, 依赖 ffprobe 可选)")
    ap.add_argument("--base-url", default=os.environ.get("SUBHUB_BASE_URL", ""),
                    help="部署后的绝对前缀(如 https://u.github.io/repo/subhub)，"
                         "写进面板的订阅地址框；不给则用相对地址+JS 补齐")
    args = ap.parse_args()

    t0 = time.time()
    sources = load_state()
    if not sources:
        print("没有源: 请先准备 state/sources.json")
        return 1
    feeds = load_json(os.path.join(STATE_DIR, "feeds.json"), [])
    history = []
    hpath = os.path.join(STATE_DIR, "history.jsonl")
    if os.path.exists(hpath):
        with open(hpath, encoding="utf-8") as f:
            history = [json.loads(ln) for ln in f if ln.strip()]

    print("== 源 %d 个, 固定资源 %d 个" % (len(sources), len(feeds)))
    if feeds and not args.no_feeds:
        sync_feeds(sources, feeds)

    print("== 检测中 (deep=%s, workers=%d)…" % (args.deep, args.workers))
    results, unreachable = run_checks(sources, args.deep, args.workers, args.timeout)
    ok = sum(1 for s in sources
             if s.get("enabled", True) and s.get("last_ok") == 1
             and not s.get("last_unreachable"))
    print("   有效 %d / 共 %d | 本机不可达(未计失败) %d"
          % (ok, len([s for s in sources if s.get("enabled", True)]), unreachable))

    # L4 试播（可选）: 只对检测有效的源抽一条真实 m3u8
    if args.play:
        valid = [s for s in sources if s.get("enabled", True)
                 and s.get("last_ok") == 1 and not s.get("last_unreachable")]
        print("== 试播 %d 个有效源…" % len(valid))
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(checker.probe_play, s["url"], args.timeout): s for s in valid}
            for f in concurrent.futures.as_completed(futs):
                s = futs[f]
                try:
                    s["play"] = f.result()
                except Exception as e:                 # noqa: BLE001
                    s["play"] = {"ok": False, "note": "异常 %s" % type(e).__name__}
        playable = sum(1 for s in valid if (s.get("play") or {}).get("ok"))
        print("   可播 %d / %d" % (playable, len(valid)))

    # 历史 + 每源 uptime
    run = {"ts": int(time.time()), "total": len(sources), "ok": ok,
           "unreachable": unreachable,
           "items": [{"id": s.get("id"), "name": s.get("name"), "url": s["url"],
                      "ok": (None if s.get("last_unreachable") else s.get("last_ok")),
                      "verdict": s.get("last_verdict"), "sites": s.get("sites")}
                     for s in sources]}
    history.append(run)
    history = history[-HISTORY_KEEP:]
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(hpath, "w", encoding="utf-8") as f:
        for h in history:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")

    # 多仓聚合: 全量 + 精选(只含人工种子, 不受自动发现的源影响)
    entries = []
    for s in sources:
        e = dict(s)
        if args.strict and s.get("last_ok") is None and not s.get("user_ok"):
            e["last_ok"] = 0                      # 严格模式: 未知按无效处理
        entries.append(e)
    merged = merge.merge_multi(entries, cap=50)
    save_json(os.path.join(args.out, "merged.json"), merged)
    merged_manual = merge.merge_multi(entries, cap=50, kind="manual")
    save_json(os.path.join(args.out, "merged-manual.json"), merged_manual)

    # 单仓配置
    master = pick_master(sources)
    tvbox, stats = build_tvbox(sources, master)

    # 状态快照
    for s in sources:
        s["spark"], s["uptime"] = uptime_from_history(history, s["url"])
    # 面板按同一套权重排(人工置顶 → 有效 → 失效), 与订阅文件顺序一致;
    # 只排副本, state/sources.json 的存储顺序不动, 免得手改过的行序被悄悄重写
    shown = sorted(sources, key=merge.rank_key)
    status = {
        "generated_at": int(time.time()),
        "generated_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "elapsed_sec": round(time.time() - t0, 1),
        "deep": args.deep,
        "stats": {
            "total": len(sources),
            "ok": ok,
            "unreachable": unreachable,
            "degraded": sum(1 for s in sources if s.get("degraded")),
            "user_ok": sum(1 for s in sources if s.get("user_ok")),
            "merged": len(merged["urls"]),
            "merged_manual": len(merged_manual["urls"]),
            "sites": stats.get("sites"),
            "lives": stats.get("lives"),
            "playable": sum(1 for s in sources if (s.get("play") or {}).get("ok")),
        },
        "tvbox_stats": stats,
        "sources": shown,
        "history": [{"ts": h["ts"], "ok": h["ok"], "total": h["total"],
                     "unreachable": h["unreachable"]} for h in history[-30:]],
    }
    save_json(os.path.join(args.out, "status.json"), status)
    if tvbox:
        save_json(os.path.join(args.out, "tvbox.json"), tvbox)
    else:
        print("   单仓配置跳过: %s" % stats.get("error"))

    # 静态面板
    html = static_page.render(status, base_url=args.base_url or None)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    save_state(sources)
    print("== 产物: %s" % args.out)
    print("   merged.json %d 条 | merged-manual.json %d 条 | tvbox.json %s | 耗时 %.1fs"
          % (len(merged["urls"]), len(merged_manual["urls"]),
             ("%d 站" % stats["sites"]) if tvbox else "无", time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
