#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
subhub — TVBox 订阅管理工作台
============================
- 固定资源(feeds)定时同步: 自动发现新订阅并入待检队列
- 四层检测调度: 定时并发检测全部启用源, 连续失败自动降级, 恢复自动解除
- 订阅生成: 生成多仓聚合 JSON (只含通过检测的源)
- 可视化: http://127.0.0.1:8765/

零第三方依赖, Python 3.10+。
"""
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checker
import db
import merge
from feed_sync import FEED_URL_BLACKLIST, extract_feed_urls  # noqa: F401

PORT = int(os.environ.get("SUBHUB_PORT", "8765"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ---------------- 检测调度 ----------------

class Scheduler(threading.Thread):
    """daemon 线程: 每 interval_minutes 跑一轮 (sync feeds + check all)"""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop_evt = threading.Event()
        self.busy = threading.Event()
        self.last_run = None
        self.last_result = None

    def run(self):
        while not self.stop_evt.is_set():
            interval = max(1, int(db.get_settings().get("interval_minutes", 10)))
            if self.stop_evt.wait(interval * 60):
                break
            try:
                self.round("auto")
            except Exception as e:  # 调度线程绝不能死
                db.log("error", "调度轮异常: %r" % e)

    def round(self, trigger):
        if self.busy.is_set():
            return {"skipped": "busy"}
        self.busy.set()
        t0 = time.time()
        try:
            n_feed = sync_feeds()
            res = check_all()
            self.last_run, self.last_result = int(time.time()), res
            db.log("info", "[%s] 检测完成: %d源, 有效%d, 耗时%.1fs, 新增订阅%d"
                   % (trigger, res["total"], res["ok"], time.time() - t0, n_feed))
            return res
        finally:
            self.busy.clear()


_sched = Scheduler()


def check_all():
    """并发检测所有启用源; 连续失败≥3 自动降级, 恢复则解除。"""
    s = db.get_settings()
    deep = s.get("deep_check", "1") == "1"
    timeout = float(s.get("check_timeout", 12))
    workers = int(s.get("max_workers", 8))
    srcs = [x for x in db.list_sources() if x["enabled"]]
    results = []
    skipped_unreach = 0
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(checker.check_source, x["url"], deep, timeout): x
                for x in srcs}
        for f in concurrent.futures.as_completed(futs):
            src = futs[f]
            try:
                r = f.result()
            except Exception as e:
                r = {"url": src["url"], "http": "-", "type": "-",
                     "verdict": "✗ 检测异常(%s)" % type(e).__name__, "note": ""}
            # 连接层不可达 = 本机网络问题, 不是源的失败: 不记录、不计数、不降级
            # (真实 App 在 CN 网络能拉到的源, 常被沙箱出口误判)
            if "本机不可达" in r["verdict"] and not src.get("user_ok"):
                skipped_unreach += 1
                results.append({"id": src["id"], "name": src["name"],
                                "verdict": r["verdict"] + " ·未计失败",
                                "degraded": bool(src["degraded"]),
                                "user_ok": bool(src.get("user_ok"))})
                continue
            db.add_check(src["id"], r)
            ok_now = r["verdict"].startswith("✓")
            if src.get("user_ok"):
                # 用户在 App 里验证过可用 → 不信 vantage 结论, 永不降级
                streak, degraded = 0, 0
            else:
                streak = src["fail_streak"] + 1 if not ok_now else 0
                degraded = 1 if streak >= 3 else 0
            db.update_source(src["id"], fail_streak=streak, degraded=degraded)
            results.append({"id": src["id"], "name": src["name"],
                            "verdict": r["verdict"], "degraded": degraded,
                            "user_ok": bool(src.get("user_ok"))})
    db.clean_checks()
    ok = sum(1 for r in results if r["verdict"].startswith("✓"))
    if skipped_unreach:
        db.log("warn", "本轮 %d/%d 源连接层不可达(本机网络问题), 未计为失败"
               % (skipped_unreach, len(results)))
    return {"total": len(results), "ok": ok, "items": results,
            "unreachable": skipped_unreach}


# ---------------- 固定资源同步 ----------------

def sync_feeds():
    """拉取所有启用 feed, diff 出新订阅入库。返回新增条数。"""
    added = 0
    for f in db.list_feeds():
        if not f["enabled"]:
            continue
        st, body = checker.fetch(f["url"], timeout=20)
        if st != 200 or not body:
            db.update_feed(f["id"], last_error="HTTP %s" % st)
            db.log("warn", "feed 同步失败: %s (%s)" % (f["name"], st))
            continue
        try:
            items = extract_feed_urls(body, f["url"])
        except Exception as e:
            db.update_feed(f["id"], last_error=str(e))
            continue
        n = 0
        for name, url in items:
            if db.add_source(name or url[:24], url, kind="feed", origin=f["name"]):
                n += 1
        added += n
        db.update_feed(f["id"], last_found=n, last_error=None)
        db.log("info", "feed 同步: %s → 发现%d条, 新增%d条" % (f["name"], len(items), n))
    return added


# ---------------- 订阅生成 ----------------

def build_merged(kind=None):
    """多仓聚合订阅(juhe 格式): 只含 ★有效/👤确认 的源, 按用户排序输出。"""
    st = db.get_settings()
    last = db.last_checks()
    entries = []
    for s in db.list_sources(include_disabled=False):
        cs = last.get(s["id"]) or []
        entries.append(dict(s, last_ok=(cs[0]["ok"] if cs else None)))
    return merge.merge_multi(entries, cap=int(st.get("merge_cap", 50)), kind=kind,
                             include_degraded=st.get("include_degraded", "0") == "1")


def build_single():
    """单仓 tvbox 配置: 选一个主源(其 spider jar 是唯一能被 App 加载的),
    跨源并入"非 csp 且绝对 URL"的点播站 + 直播 + 解析。
    返回 (config_dict, stats_dict)。无法选出主源时 config 为 None。"""
    st = db.get_settings()
    master_id = int(st.get("single_master", "0") or 0)
    srcs = [s for s in db.list_sources() if s["enabled"]]
    master = db.get_source(master_id) if master_id else None
    if master and not master["enabled"]:
        master = None
    if master is None:  # 自动: 排序第一个"上次检测有效"的源
        last = db.last_checks()
        for s in srcs:
            cs = last.get(s["id"]) or []
            if cs and cs[0]["ok"]:
                master = s
                break
    if master is None:
        return None, {"error": "没有可用作主源的订阅(先跑一轮检测)"}
    cfg, _, _ = checker.load_config(master["url"])
    if not isinstance(cfg, dict):
        return None, {"error": "主源无法解析: %s" % master["name"]}
    others = []
    for s in srcs:
        if s["id"] == master["id"]:
            continue
        c2, _, _ = checker.load_config(s["url"])
        others.append({"url": s["url"], "cfg": c2})
    return merge.merge_single(
        dict(master, cfg=cfg), others,
        want_lives=st.get("single_lives", "1") == "1",
        want_parses=st.get("single_parses", "1") == "1")


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "subhub/1.0"

    def log_message(self, *a):  # 静音默认访问日志
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _static(self, path):
        if path in ("/", "/index.html"):
            path = "/index.html"
        fp = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not fp.startswith(STATIC) or not os.path.isfile(fp):
            return self._json({"error": "not found"}, 404)
        with open(fp, "rb") as f:
            b = f.read()
        ctype = ("text/html; charset=utf-8" if fp.endswith(".html")
                 else "application/javascript" if fp.endswith(".js")
                 else "text/css" if fp.endswith(".css") else "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/state":
            srcs, last = db.list_sources(), db.last_checks()
            for s in srcs:
                cs = last.get(s["id"], [])
                s["checks"] = cs
                s["uptime"] = (round(sum(c["ok"] for c in cs) / len(cs) * 100)
                               if cs else None)
                s["last_ok"] = cs[0]["ok"] if cs else None
            self._json({
                "sources": srcs,
                "feeds": db.list_feeds(),
                "settings": db.get_settings(),
                "logs": db.recent_logs(60),
                "scheduler": {"busy": _sched.busy.is_set(),
                              "last_run": _sched.last_run},
            })
        elif p == "/api/subscribe/merged":
            kind = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("kind", [None])[0]
            b = json.dumps(build_merged(kind), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/api/subscribe/tvbox":
            cfg, stats = build_single()
            if cfg is None:
                return self._json(stats, 409)
            b = json.dumps(cfg, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            # 丢弃/合并统计走 header, 不污染订阅 JSON 本身
            self.send_header("X-Subhub-Stats", urllib.parse.quote(json.dumps(stats)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/api/subscribe/tvbox/stats":
            _, stats = build_single()
            self._json(stats)
        elif p.startswith("/api/"):
            self._json({"error": "unknown api"}, 404)
        else:
            self._static(p)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        b = self._body()
        if p == "/api/sources":
            url = (b.get("url") or "").strip()
            if not url.startswith("http"):
                return self._json({"error": "url 必须以 http 开头"}, 400)
            sid = db.add_source(b.get("name") or url[:24], url)
            db.log("info", "手动添加源: %s" % url)
            return self._json({"id": sid})
        if p == "/api/check-all":
            res = _sched.round("manual")
            if "skipped" in res:
                return self._json({"error": "上一轮还在跑"}, 409)
            return self._json(res)
        if p == "/api/feeds/sync":
            n = sync_feeds()
            return self._json({"added": n})
        if p == "/api/feeds":
            url = (b.get("url") or "").strip()
            if not url.startswith("http"):
                return self._json({"error": "url 必须以 http 开头"}, 400)
            db.add_feed(b.get("name") or url[:24], url)
            n = sync_feeds()
            db.log("info", "添加固定资源: %s (新增%d源)" % (url, n))
            return self._json({"added": n})
        if p == "/api/settings":
            for k, v in b.items():
                db.set_setting(k, v)
            db.log("info", "更新设置: %s" % b)
            return self._json({"ok": True})
        m = re.fullmatch(r"/api/sources/(\d+)/move", p)
        if m:
            direction = int((b.get("dir") or -1))
            ok = db.move_source(int(m.group(1)), direction)
            return self._json({"ok": ok})
        m = re.fullmatch(r"/api/sources/(\d+)/check", p)
        if m:
            src = db.get_source(int(m.group(1)))
            if not src:
                return self._json({"error": "no such source"}, 404)
            r = checker.check_source(src["url"],
                                     deep=db.get_settings().get("deep_check") == "1")
            if "本机不可达" in r["verdict"] and not src.get("user_ok"):
                # 连接层不可达=本机网络问题, 不记录失败(Last-good-wins)
                return self._json({"result": dict(r, verdict=r["verdict"] + " ·未计失败")})
            db.add_check(src["id"], r)
            if not src.get("user_ok"):
                ok_now = r["verdict"].startswith("✓")
                streak = src["fail_streak"] + 1 if not ok_now else 0
                db.update_source(src["id"], fail_streak=streak,
                                 degraded=1 if streak >= 3 else 0)
            return self._json({"result": r})
        m = re.fullmatch(r"/api/sources/(\d+)/override", p)
        if m:
            # 用户标记: {ok:1|0} — App 里验证可用的源, 覆盖检测结论
            sid = int(m.group(1))
            src = db.get_source(sid)
            if not src:
                return self._json({"error": "no such source"}, 404)
            body = b or {}
            ok = 1 if body.get("ok") else 0
            db.update_source(sid, user_ok=ok, fail_streak=0, degraded=0)
            db.log("INFO", "用户%s #%d %s" % ("标记可用" if ok else "取消确认", sid, src["name"]))
            return self._json({"ok": True, "source": db.get_source(sid)})
        return self._json({"error": "unknown api"}, 404)

    def do_DELETE(self):
        p = urllib.parse.urlparse(self.path).path
        m = re.fullmatch(r"/api/sources/(\d+)", p)
        if m:
            db.delete_source(int(m.group(1)))
            return self._json({"ok": True})
        m = re.fullmatch(r"/api/feeds/(\d+)", p)
        if m:
            db.delete_feed(int(m.group(1)))
            return self._json({"ok": True})
        return self._json({"error": "unknown api"}, 404)

    def do_PUT(self):
        p = urllib.parse.urlparse(self.path).path
        b = self._body()
        m = re.fullmatch(r"/api/sources/(\d+)", p)
        if m and b:
            db.update_source(int(m.group(1)), **b)
            return self._json({"ok": True})
        return self._json({"error": "unknown api"}, 404)


def seed_if_empty():
    if db.list_sources() or db.list_feeds():
        return
    for name, url in [
        ("饭太硬", "http://www.饭太硬.cc/tv"),
        ("饭太硬备用", "http://www.饭太硬.net/tv"),
        ("摸鱼儿", "http://www.y456y.com"),
        ("王二小", "http://new.王二小放牛娃.top"),
        ("肥猫", "http://肥猫.net/"),
        ("小盒子4K", "http://xhztv.top/4k.json"),
        ("创享影视", "https://ys.cxkjg.cn/api/provide/tvbox"),
        ("影仓", "http://124.71.19.220/c/1"),
        ("老刘备", "https://raw.liucn.cc/box/m.json"),
        ("小马", "https://szyyds.cn/tv/x.json"),
        ("小盒子单仓", "http://xhztv.top/xhz"),
        ("小盒子多仓", "http://xhztv.top/dc"),
        ("拾光多仓", "http://xmbjm.fh4u.org/dc.txt"),
        ("挺好分享多仓", "http://ztha.top/TVBox/GYCK.json"),
        ("心魔在线", "https://gh-proxy.com/raw.githubusercontent.com/yw88075/tvbox/main/yw.json"),
        ("FongMI线路", "https://gh-proxy.com/raw.githubusercontent.com//gaotianliuyun/gao/master/0827.json"),
        ("高天流云js", "https://gh-proxy.com/raw.githubusercontent.com/gaotianliuyun/gao/master/js.json"),
        ("高天流云XYQ", "https://gh-proxy.com/raw.githubusercontent.com/gaotianliuyun/gao/master/XYQ.json"),
        ("健康家用", "https://gitlab.com/noimank/tvbox/-/raw/main/tvbox1.json"),
        ("南风", "https://gh-proxy.com/https://raw.githubusercontent.com/yoursmile66/TVBox/main/XC.json"),
    ]:
        db.add_source(name, url)
    for name, url in [
        ("ause聚合", "https://ause.cc/juhe"),
        ("追剧么配置列表", "https://b.zhuiju.me/p/3"),
    ]:
        db.add_feed(name, url)
    db.log("info", "已写入 20 个种子源 + 2 个固定资源")


def main():
    seed_if_empty()
    _sched.start()
    db.log("info", "subhub 启动于 :%d" % PORT)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("subhub 已启动: http://127.0.0.1:%d  (Ctrl-C 停止)" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
