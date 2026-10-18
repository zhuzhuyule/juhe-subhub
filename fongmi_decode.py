#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FongMi Decoder 忠实移植 —— 把一条订阅按 App 的真实解析路径走一遍
=================================================================
和 checker.py 的区别: 这里**只做解析、不做判定**, 目标是回答
「按 FongMi/影视仓 的代码走, 这条配置到底能解出什么」。

分派顺序严格照 Decoder.java:31（顺序本身就是逻辑, 改了就错）:

    if (Json.isObj(data))            return fix(url, data);   // ① 严格 JSON 直通
    if (data.contains("**"))         data = base64(data);     // ② 含 ** 才试 base64
    if (data.startsWith("2423"))     data = cbc(...);         // ③ 解完 ② 再看 2423
    return fix(url, data);                                    // ④ 相对路径归一

注意 ① 用的是 org.json.JSONObject（严格），L3 用的是 Gson（lenient）。
两个解析器不是同一个 —— 带 // 注释的配置在 ① 判 false、在 L3 判 true，
所以它会掉进 ②/③ 再被捞回来。这个差异是整套方案最容易漏的点。

层:
  L0 取数    GET / 零自定义头 / 30s / 全信任 TLS / 跟重定向 / 校验 Content-Length
  L1 容器    ①②③④ 分派, 打印每层中间态
  L3 结构    严格 JSON vs lenient, 各顶层键的规模
  L4 站点    type 分布 / csp_ / jar 后缀语法(;md5; ;pk; img+) / ext 是 URL 还是密文
  L5 jar     下载 + md5 校验 + zip 条目 + stub dex + .guard 壳识别
  L6 ext     App 层不碰 ext, 非 http 的原样丢给爬虫 → 密文是正常现象, 不是配置坏了

用法:
  python3 fongmi_decode.py <url> [<url> ...]
  python3 fongmi_decode.py --state state/sources.json      # 跑 state 里全部源
  python3 fongmi_decode.py <url> --follow                  # 遇到 urls(Depot) 递归展开
  python3 fongmi_decode.py <url> --json out.json --quiet    # 机器可读
"""
import argparse
import base64
import hashlib
import io
import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import checker
import crypto_aes

TIMEOUT = 30          # FongMi 常量 TIMEOUT = 30s
UA = "okhttp/5.5.0"   # FongMi OkHttp.newCall 的默认 UA
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

_MARK_RE = re.compile(r"[A-Za-z0-9]{8}\*\*")


# ---------------- L0 取数 ----------------

def http_get(url, timeout=TIMEOUT):
    """零自定义头 GET。返回 dict(status, body, hdrs, final, cl_ok, err)。"""
    out = {"status": None, "body": b"", "hdrs": {}, "final": url,
           "cl_ok": None, "err": None}
    try:
        req = urllib.request.Request(checker.idna(url),
                                     headers={"User-Agent": UA,
                                              "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            out["status"] = r.status
            out["final"] = r.geturl()
            out["hdrs"] = {k.lower(): v for k, v in r.headers.items()}
            body = r.read(checker.MAX_BYTES)
            # OkHttp 会透明解 gzip; urllib 不会。不解的话 26 条里 19 条会"解不开"。
            if out["hdrs"].get("content-encoding", "").lower() == "gzip":
                import gzip
                try:
                    body = gzip.decompress(body)
                except Exception:                            # noqa: BLE001
                    pass
            out["body"] = body
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        out["err"] = "HTTPError"
        out["hdrs"] = {k.lower(): v for k, v in (e.headers or {}).items()}
        return out
    except Exception as e:                                   # noqa: BLE001
        out["status"] = "ERR:%s" % type(e).__name__
        out["err"] = type(e).__name__
        return out
    cl = out["hdrs"].get("content-length")
    if cl and cl.isdigit():
        # 截断的响应会伪装成"一张图片", 这个校验必须做
        out["cl_ok"] = (int(cl) == len(out["body"]))
    return out


# ---------------- L1 容器 ----------------

def is_obj(text):
    """org.json.JSONObject 的严格等价: 必须 { 开头且严格可解析。"""
    s = text.lstrip("\ufeff").strip()
    if not s.startswith("{"):
        return False
    try:
        return isinstance(json.loads(s), dict)
    except ValueError:
        return False


def b64_wrap(text):
    """② 含 ** 就走 base64: 标记后第 10 个字符起到结尾。"""
    m = _MARK_RE.search(text)
    if not m:
        return None, None
    body = text[m.end():]
    try:
        raw = base64.b64decode(body, validate=False)
    except Exception:                                        # noqa: BLE001
        return None, None
    return raw.decode("utf-8", "ignore"), {
        "marker": m.group(0), "at_char": m.start(),
        "body_chars": len(body), "out_chars": len(raw),
    }


_JS_RE = re.compile(r"""(?:\.\.?/|https?://)[^\s"'<>\\]*?\.js(?:\?[^\s"'<>\\]*)?""")


def fix_refs(text, base):
    """④ 把 js 爬虫的相对引用展开成绝对地址（FongMi Decoder.fix）。"""
    n = [0]

    def sub(m):
        u = m.group(0)
        if u.startswith(("http://", "https://")):
            return u
        n[0] += 1
        return urllib.parse.urljoin(base, u)

    return _JS_RE.sub(sub, text), n[0]


def l1(raw, url):
    """①②③④ 分派。返回 (text, trace[])。"""
    tr = []
    text = raw.decode("utf-8", "ignore").lstrip("\ufeff").strip()
    magic = raw[:4]
    tr.append("魔数 %s%s" % (magic.hex(), _ascii_hint(magic)))

    if is_obj(text):
        tr.append("isObj=true → 明文直通")
        return text, tr
    tr.append("isObj=false")

    if "**" in text:
        dec, info = b64_wrap(text)
        if dec is not None:
            tr.append("contains('**')=true → base64: 标记 %s @ 字符 %d, %d → %d 字符"
                      % (info["marker"], info["at_char"],
                         info["body_chars"], info["out_chars"]))
            text = dec
        else:
            tr.append("contains('**')=true 但 base64 解不开")
    else:
        tr.append("contains('**')=false")

    if text.lstrip().startswith("2423"):
        dec = checker.decode_2423(text)
        if dec is None:
            tr.append("startsWith('2423') → AES/CBC 解密失败")
        else:
            tr.append("startsWith('2423') → AES/CBC/PKCS5 → %d 字符" % len(dec))
            text = dec
    else:
        tr.append("startsWith('2423')=false")

    text, n = fix_refs(text, url)
    tr.append("fix() 相对路径展开 %d 处 → %d 字符" % (n, len(text)))
    return text, tr


def _ascii_hint(b):
    if all(32 <= c < 127 for c in b):
        return " (%r)" % b.decode("ascii", "replace")
    return ""


# ---------------- L3 结构 ----------------

def l3(text):
    """严格 JSON vs Gson lenient。返回 (cfg, info)。"""
    info = {}
    try:
        cfg = json.loads(text)
        info["strict"] = "OK"
    except ValueError as e:
        info["strict"] = "FAIL: %s" % e
        cfg, how = checker.parse_config(text)
        info["lenient"] = how or "FAIL"
    else:
        info["lenient"] = "n/a"
    if not isinstance(cfg, dict):
        return None, info
    info["keys"] = list(cfg.keys())
    for k in ("sites", "lives", "parses", "rules", "flags", "doh", "ijk",
              "ads", "hosts", "urls"):
        v = cfg.get(k)
        if isinstance(v, list):
            info[k] = len(v)
    return cfg, info


# ---------------- L4 站点 ----------------

def split_jar(jar):
    """jar 后缀语法: ;md5;HASH 校验 / ;pk;KEY AES-ECB / img+ 前缀。"""
    d = {"raw": jar, "url": jar, "md5": None, "pk": None, "img": False}
    if jar.startswith("img+"):
        d["img"] = True
        d["url"] = jar[4:]
    parts = d["url"].split(";")
    d["url"] = parts[0]
    for i, p in enumerate(parts[1:], 1):
        if p == "md5" and i + 1 < len(parts):
            d["md5"] = parts[i + 1]
        elif p == "pk" and i + 1 < len(parts):
            d["pk"] = parts[i + 1]
    return d


def l4(cfg):
    sites = [s for s in (cfg.get("sites") or []) if isinstance(s, dict)]
    types = {}
    csp = 0
    ext_url = ext_cipher = ext_none = 0
    ext_samples = []
    jars = {}
    for s in sites:
        types[s.get("type")] = types.get(s.get("type"), 0) + 1
        api = str(s.get("api") or "")
        if api.startswith("csp_"):
            csp += 1
        ext = str(s.get("ext") or "")
        if not ext:
            ext_none += 1
        elif ext.startswith("http"):
            ext_url += 1
        else:
            ext_cipher += 1
            if len(ext_samples) < 3:
                ext_samples.append(ext[:36])
        j = str(s.get("jar") or "")
        if j:
            jars[j] = jars.get(j, 0) + 1
    spider = str(cfg.get("spider") or "")
    return {
        "sites": len(sites), "types": types, "csp": csp,
        "ext_url": ext_url, "ext_cipher": ext_cipher, "ext_none": ext_none,
        "ext_samples": ext_samples,
        "spider": split_jar(spider) if spider else None,
        "site_jars": jars,
    }


# ---------------- L5 jar ----------------

def l5(jarinfo, timeout=TIMEOUT):
    """下载 jar → 校验 md5 → 看条目 → 识别 stub dex + .guard 壳。"""
    out = {"url": jarinfo["url"]}
    r = http_get(jarinfo["url"], timeout)
    body = r["body"]
    out["status"] = r["status"]
    out["bytes"] = len(body)
    if r["status"] != 200 or not body:
        out["err"] = r["err"] or "空响应"
        return out
    if jarinfo["md5"]:
        got = hashlib.md5(body).hexdigest()
        out["md5"] = got
        out["md5_ok"] = (got == jarinfo["md5"].lower())
    if body[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(body))
            ents = [(i.filename, i.file_size) for i in zf.infolist()]
            out["entries"] = ents
            dex = zf.read("classes.dex") if "classes.dex" in zf.namelist() else b""
            out["dex_bytes"] = len(dex)
            out["dex_csp"] = dex.count(b"csp_")
            out["guard"] = [n for n, _ in ents if n.endswith(".guard")]
            out["so"] = [n for n, _ in ents if n.endswith(".so")]
            # stub 壳: dex 极小 + 带 .guard + 带 .so → 类名静态扫不到是正常的
            out["stub"] = bool(out["guard"] and out["so"]
                               and len(dex) < 200 * 1024 and not out["dex_csp"])
        except Exception as e:                               # noqa: BLE001
            out["err"] = "zip: %s" % e
    else:
        out["err"] = "非 zip(%s)" % body[:8].hex()
    return out


# ---------------- 主流程 ----------------

def decode(url, do_jar=True, timeout=TIMEOUT):
    t0 = time.time()
    res = {"url": url}
    r = http_get(url, timeout)
    res["l0"] = {
        "status": r["status"], "bytes": len(r["body"]),
        "content_length": r["hdrs"].get("content-length"),
        "cl_ok": r["cl_ok"], "server": r["hdrs"].get("server"),
        "ct": r["hdrs"].get("content-type"),
        "etag": r["hdrs"].get("etag"),
        "final": r["final"] if r["final"] != url else None,
        "err": r["err"],
    }
    if r["status"] != 200 or not r["body"]:
        res["ok"] = False
        res["ms"] = int((time.time() - t0) * 1000)
        return res

    text, trace = l1(r["body"], r["final"] or url)
    res["l1"] = trace

    cfg, info = l3(text)
    res["l3"] = info
    if not isinstance(cfg, dict):
        res["ok"] = False
        res["ms"] = int((time.time() - t0) * 1000)
        return res

    res["l4"] = l4(cfg)
    sp = res["l4"]["spider"]
    if sp:
        # jar 可以是相对路径(老刘备就是 ./fty.jar), App 按订阅 URL 补全
        sp["url"] = urllib.parse.urljoin(r["final"] or url, sp["url"])
    if do_jar and sp:
        res["l5"] = l5(sp, timeout)

    if res["l4"]["ext_cipher"]:
        res["l6"] = ("%d 个站点 ext 是密文(非 http) → App 层不碰, 原样交给爬虫内部解, "
                     "样本: %s" % (res["l4"]["ext_cipher"],
                                   ", ".join(res["l4"]["ext_samples"])))
    res["ok"] = True
    res["ms"] = int((time.time() - t0) * 1000)
    return res


def print_one(res, name=None):
    head = name or res["url"]
    print("\n" + "=" * 78)
    print("%s\n  %s" % (head, res["url"]))
    print("=" * 78)
    a = res["l0"]
    print("L0 取数   status=%s bytes=%d CL=%s%s server=%s ct=%s"
          % (a["status"], a["bytes"], a["content_length"],
             "" if a["cl_ok"] is None else ("✓" if a["cl_ok"] else " ✗截断!"),
             a["server"], a["ct"]))
    if a["final"]:
        print("          重定向 → %s" % a["final"])
    if a["etag"]:
        print("          etag=%s  (判断有没有真更新看这个, 别看 Last-Modified)" % a["etag"])
    if not res.get("ok"):
        print("  ✗ %s" % (a.get("err") or "配置拿不到/解不开"))
        return
    for line in res["l1"]:
        print("L1 容器   " + line)
    s = res["l3"]
    print("L3 结构   严格 JSON: %s%s"
          % (s.get("strict"), "" if s.get("lenient") in (None, "n/a")
             else " → lenient: %s" % s["lenient"]))
    keys = [k for k in ("sites", "lives", "parses", "rules", "flags", "doh",
                        "ijk", "ads", "hosts", "urls") if k in s]
    print("          顶层键 %s" % (", ".join("%s=%s" % (k, s[k]) for k in keys) or "—"))
    q = res["l4"]
    print("L4 站点   %d 个 | type %s | csp_* %d"
          % (q["sites"], q["types"], q["csp"]))
    print("          ext: http %d / 密文 %d / 空 %d"
          % (q["ext_url"], q["ext_cipher"], q["ext_none"]))
    sp = q["spider"]
    if sp:
        print("          spider %s%s%s"
              % (sp["url"][:70],
                 "  ;md5;" if sp["md5"] else "",
                 "  ;pk;" if sp["pk"] else ""))
    j = res.get("l5")
    if j:
        if j.get("err"):
            print("L5 jar    ✗ %s (%s bytes)" % (j["err"], j.get("bytes")))
        else:
            print("L5 jar    %d 字节  md5=%s  校验=%s"
                  % (j["bytes"], j.get("md5"),
                     "通过" if j.get("md5_ok") else
                     ("未给 md5" if j.get("md5_ok") is None else "不匹配!")))
            print("          条目 %s" % ", ".join(
                "%s(%d)" % (n, sz) for n, sz in (j.get("entries") or [])[:6]))
            if j.get("stub"):
                print("          ⚠ stub 壳: dex %d 字节/csp_ 出现 %d 次 + %s + %s"
                      % (j["dex_bytes"], j["dex_csp"],
                         ",".join(j["guard"]), ",".join(j["so"])))
                print("             → 真 dex 在 .guard 里由 native 解出, "
                      "静态扫类名必然为 0, 不代表配置造假")
    if res.get("l6"):
        print("L6 ext    " + res["l6"])
    print("          耗时 %d ms" % res["ms"])


def main():
    ap = argparse.ArgumentParser(description="FongMi Decoder 忠实移植（只解析不判定）")
    ap.add_argument("urls", nargs="*", help="订阅 URL")
    ap.add_argument("--state", help="从 state/sources.json 读全部源")
    ap.add_argument("--follow", action="store_true",
                    help="顶层是 urls(Depot) 时递归展开每个子配置")
    ap.add_argument("--json", dest="json_out", help="把结果写成 JSON")
    ap.add_argument("--timeout", type=float, default=TIMEOUT)
    ap.add_argument("--no-jar", action="store_true", help="跳过 jar 下载(L5)")
    ap.add_argument("--quiet", action="store_true", help="只打汇总表")
    a = ap.parse_args()

    jobs = [(None, u) for u in a.urls]
    if a.state:
        for s in json.load(open(a.state, encoding="utf-8")):
            if s.get("enabled", True) and s.get("url"):
                jobs.append((s.get("name"), s["url"]))
    if not jobs:
        ap.error("给 URL 或用 --state")

    results, extra = [], []
    for name, url in jobs:
        r = decode(url, do_jar=not a.no_jar, timeout=a.timeout)
        r["name"] = name
        if not a.quiet:
            print_one(r, name)
        results.append(r)
        # Depot 递归
        if a.follow and r.get("ok"):
            cfg = None
            raw = http_get(url, a.timeout)["body"].decode("utf-8", "ignore")
            text, _ = l1(raw.encode(), url)
            cfg, _ = l3(text)
            if isinstance(cfg, dict) and isinstance(cfg.get("urls"), list):
                for u in cfg["urls"][:30]:
                    if isinstance(u, str) and u.startswith("http"):
                        rr = decode(u, do_jar=not a.no_jar, timeout=a.timeout)
                        rr["name"] = (name or "depot") + " › " + u.rsplit("/", 1)[-1][:18]
                        extra.append(rr)
                        if not a.quiet:
                            print_one(rr, rr["name"])

    allr = results + extra
    if not a.quiet:
        print("\n" + "=" * 78)
        print("汇总")
        print("=" * 78)
        print("%-22s %-6s %-7s %-6s %-6s %-6s %s"
              % ("源", "HTTP", "字节", "sites", "csp", "ext密", "容器/说明"))
    for r in allr:
        if not r.get("ok"):
            print("%-22s %-6s %-7s %-6s %-6s %-6s %s"
                  % (str(r.get("name") or r["url"])[:22], r["l0"]["status"],
                     r["l0"]["bytes"], "-", "-", "-",
                     r["l0"].get("err") or "拿不到/解不开"))
            continue
        q = r["l4"]
        how = " / ".join(x for x in r["l1"] if "→" in x or "true" in x)[:44]
        print("%-22s %-6s %-7s %-6s %-6s %-6s %s"
              % (str(r.get("name") or r["url"])[:22], r["l0"]["status"],
                 r["l0"]["bytes"], q["sites"], q["csp"], q["ext_cipher"], how))

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(allr, f, ensure_ascii=False, indent=1)
        print("\nJSON → %s" % a.json_out)


if __name__ == "__main__":
    main()
