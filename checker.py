#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox 订阅源四层检测(库版, 供 subhub 调用)
==========================================
L0 连通层  HTTP 200 且 body 非空。连接类错误只代表本机 vantage。
L1 结构层  tvbox JSON / m3u / 直链列表 / 嵌套聚合; hex 加密内容;
           图片占位(geo-gate 症状)。
L2 信息层  sites/lives/parses/jar 非空; 有 csp_* 但无 jar → 不可用。
L3 内容层  deep=True 时抽样: jar 可下载、直播频道数、点播接口条数。

检测结论受探测 vantage 影响(geo/UA 分流)，只能"判有效"不能"判死"。
"""
import base64
import concurrent.futures
import gzip
import hashlib
import http.client
import json
import re
import shutil
import socket
import ssl
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import crypto_aes

TIMEOUT = 12
MAX_BYTES = 32 * 1024 * 1024
OKHTTP_UA = "okhttp/3.12.13"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def idna(url):
    """中文/IDN 域名转 punycode, 否则 urllib 直接 UnicodeEncodeError"""
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ""
        if host and not host.isascii():
            host = host.encode("idna").decode("ascii")
            url = urllib.parse.urlunsplit(
                (p.scheme, host + (":%d" % p.port if p.port else ""),
                 p.path, p.query, p.fragment))
    except Exception:
        pass
    return url


def fetch(url, timeout=TIMEOUT, ua=None):
    """-> (status, bytes)。status: 200 / HTTPError.code / 'ERR:类型名'

    两个必须做的处理(都会让结论错得很难看):
      - gzip: OkHttp 透明解压, urllib 不解。不解会把正常配置读成乱码。
      - 截断: len(body) != Content-Length 时重开连接再读一次。截断的响应
        会伪装成"一张图片"或"一段乱码", 直接据此判死是误判。
    """
    url = idna(url)
    for attempt in (0, 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua or OKHTTP_UA})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                body = r.read(MAX_BYTES)
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                    try:
                        body = gzip.decompress(body)
                    except Exception:                      # noqa: BLE001
                        pass
                cl = r.headers.get("Content-Length") or ""
                if attempt == 0 and cl.isdigit() and len(body) != int(cl):
                    continue                               # 截断 → 重读
                return r.status, body
        except urllib.error.HTTPError as e:
            return e.code, b""
        except Exception as e:                             # noqa: BLE001
            return "ERR:%s" % type(e).__name__, b""
    return "ERR:Truncated", b""


# ---------------- DoH 钉 IP（绕开本机 fake-IP / DNS 污染） ----------------

DOH_ENDPOINTS = ("https://doh.pub/dns-query", "https://cloudflare-dns.com/dns-query")


def doh_resolve(host, timeout=6):
    """DoH 解析 A 记录。返回 IP 列表（失败为空）。"""
    if not host or re.fullmatch(r"[\d.]+", host):
        return [host] if host else []
    for base in DOH_ENDPOINTS:
        try:
            req = urllib.request.Request(
                "%s?name=%s&type=A" % (base, urllib.parse.quote(host)),
                headers={"accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
            ips = [a["data"] for a in d.get("Answer", []) if a.get("type") == 1]
            if ips:
                return ips
        except Exception:                              # noqa: BLE001
            continue
    return []


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip=None, **kw):
        self._pin = ip
        super().__init__(host, **kw)

    def connect(self):
        sock = socket.create_connection((self._pin or self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, ip=None, **kw):
        self._pin = ip
        super().__init__(host, **kw)

    def connect(self):
        self.sock = socket.create_connection((self._pin or self.host, self.port), self.timeout)


def fetch_doh(url, timeout=TIMEOUT, ua=None, max_hops=5):
    """DoH 解析后直连 IP（SNI/Host 仍是域名），手动跟重定向（跨域重新解析）。"""
    cur = idna(url)
    for _ in range(max_hops):
        p = urllib.parse.urlsplit(cur)
        host = p.hostname
        ips = doh_resolve(host)
        if not ips:
            return "ERR:DoHFail", b""
        for ip in ips[:2]:
            try:
                port = p.port or (443 if p.scheme == "https" else 80)
                if p.scheme == "https":
                    conn = _PinnedHTTPS(host, ip=ip, port=port, timeout=timeout, context=CTX)
                else:
                    conn = _PinnedHTTP(host, ip=ip, port=port, timeout=timeout)
                path = (p.path or "/") + ("?" + p.query if p.query else "")
                conn.request("GET", path, headers={"User-Agent": ua or OKHTTP_UA,
                                                   "Host": host, "Accept": "*/*",
                                                   "Connection": "close"})
                r = conn.getresponse()
                body = r.read(MAX_BYTES)
                loc = r.getheader("Location")
                if r.status in (301, 302, 303, 307, 308) and loc:
                    cur = urllib.parse.urljoin(cur, loc)
                    break
                return r.status, body
            except Exception:                          # noqa: BLE001
                continue
        else:
            return "ERR:ConnectFail", b""
    return "ERR:TooManyRedirects", b""


def strip_json_comments(text):
    """TVBox 配置普遍是 JSONC: // 注释、/* */ 块、尾逗号、
    字符串内裸控制符, 甚至尾部粘 HTML 广告页。逐个容错。"""
    out, in_str, esc = [], False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            elif ord(ch) < 0x20:
                continue
        elif ch == '"':
            in_str = True
        out.append(ch)
    text = "".join(out)
    text = re.sub(r"(^|\s)//[^\n]*", r"\1", text, flags=re.M)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r",(\s*[\]}])", r"\1", text)
    return text


def parse_config(text):
    """-> (cfg, how) 或 (None, None)。严格 → 容错 → 截首段 三级。"""
    try:
        return json.loads(text), "strict"
    except ValueError:
        pass
    try:
        return json.loads(strip_json_comments(text)), "jsonc"
    except ValueError:
        pass
    try:  # 尾部粘了 HTML 广告页之类, 只取第一个 JSON 值
        cfg, _ = json.JSONDecoder().raw_decode(text.lstrip())
        return cfg, "prefix"
    except ValueError:
        return None, None


def looks_hex_obfuscated(text):
    """整篇只含 hex 字符 → 上游内容加密, 只能 App 内验证"""
    return len(text) > 200 and re.fullmatch(r"[0-9a-fA-F\r\n\t ]+", text) is not None


def decode_2423(text):
    """FongMi `Decoder.cbc` 等价实现: 解 "2423" 开头的加密配置。

    密钥和 IV 都在密文里（不是秘密）:
      key = 解密后文本中 $# 与 #$ 之间的标签, 补 '0' 到 16 字节
      iv  = 解密后文本末 13 个字符, 补 '0' 到 16 字节
      正文 = "2324" 之后、去掉末尾 26 个 hex 字符（13 字节 IV）的部分
    成功返回明文文本, 失败返回 None。
    """
    s = re.sub(r"\s+", "", text).lower()
    if not s.startswith("2423"):
        return None
    try:
        decoded = bytes.fromhex(s).decode("utf-8", "ignore").lower()
        i, j = decoded.find("$#"), decoded.find("#$")
        if i < 0 or j <= i + 2:
            return None
        key = (decoded[i + 2:j] + "0" * 16)[:16].encode()
        iv = (decoded[-13:] + "0" * 16)[:16].encode()
        body = s[s.index("2324") + 4: len(s) - 26]
        if not body or len(body) % 32:
            return None
        return crypto_aes.aes_cbc_decrypt(bytes.fromhex(body), key, iv).decode(
            "utf-8", "ignore")
    except Exception:                                  # noqa: BLE001
        return None


def decode_base64_wrap(text):
    """Base64 包装: 8 位字母数字 + '**' 之后是 base64 正文（FongMi 同款）。"""
    m = re.search(r"[A-Za-z0-9]{8}\*\*", text)
    if not m:
        return None
    try:
        return base64.b64decode(text[m.end():], validate=False).decode("utf-8", "ignore")
    except Exception:                                  # noqa: BLE001
        return None


_JS_REF_RE = re.compile(r"""(?:\.\.?/|https?://)[^\s"'<>\\]*?\.js(?:\?[^\s"'<>\\]*)?""")


def fix_refs(text, base):
    """FongMi Decoder.fix: js 爬虫的相对引用展开成绝对地址。

    配置里写 "./js/xx.js" 或 "../spider.js" 时, App 会按订阅 URL 补全;
    不补全的话这些 js 站点在离机看就是坏路径。实测老刘备命中 189 处。
    """
    n = [0]

    def sub(m):
        u = m.group(0)
        if u.startswith(("http://", "https://")):
            return u
        n[0] += 1
        return urllib.parse.urljoin(base, u)

    return _JS_REF_RE.sub(sub, text), n[0]


def _is_strict_obj(text):
    """org.json.JSONObject 的严格等价: { 开头且严格可解析（不认注释）。"""
    s = text.lstrip("\ufeff").strip()
    if not s.startswith("{"):
        return False
    try:
        return isinstance(json.loads(s), dict)
    except ValueError:
        return False


def decode_container(text, base):
    """FongMi Decoder.java:31 的分派顺序 —— 顺序本身就是逻辑, 改了就错。

        ① 严格 JSON 直通（带 // 注释的配置会在这里判 false）
        ② 含 "**" 才试 base64（标记前那截可能是图片/任意噪声）
        ③ 解完 ② 还要再看 "2423"（AES-CBC）
        ④ fix() 相对路径归一

    返回 (text, how[list], trace[list])。
    """
    how, trace = [], []
    if _is_strict_obj(text):
        return text, ["plain"], ["isObj=true → 明文直通"]
    if "**" in text:
        dec = decode_base64_wrap(text)
        if dec is not None:
            text = dec
            how.append("base64")
            trace.append("含 ** → base64")
    if text.lstrip().startswith("2423"):
        dec = decode_2423(text)
        if dec is not None:
            text = dec
            how.append("aes-cbc")
            trace.append("2423 → AES-CBC")
    text, n = fix_refs(text, base)
    if n:
        how.append("fix")
        trace.append("fix() 展开 %d 处相对 js 引用" % n)
    return text, how, trace


def probe_sites(sites, base, limit=2):
    """点播站抽样。csp_* 依赖本地 jar 离机不可探; 其余试 ac=videolist。
    注意: 200 但 0 条只是弱信号(drpy 站点可能只认 POST), 不能判死。"""
    notes, n = [], 0
    for s in sites:
        if n >= limit:
            break
        if not isinstance(s, dict):
            continue
        api = str(s.get("api") or "")
        nm = str(s.get("name") or "?")[:6]
        if not api:
            continue
        if api.startswith("csp_"):
            notes.append("%s=csp(需jar)" % nm)
            continue
        n += 1
        probe = urllib.parse.urljoin(base, api) + ("&" if "?" in api else "?") + "ac=videolist"
        st, body = fetch(probe, timeout=8)
        if st != 200:
            notes.append("%s=接口HTTP %s" % (nm, st))
        else:
            rows = len(re.findall(r'"vod_id"|"vod_name"', body.decode("utf-8", "ignore")))
            notes.append("%s=列表%d条" % (nm, rows))
    return notes


def check_source(url, deep=False, timeout=TIMEOUT, doh=False):
    """-> dict(name=url, http, type, sites, lives, parses, jar, verdict, note, ms)

    doh=True 时用 DoH 解析 + 直连 IP（绕开本机 fake-IP/DNS 污染），给"vantage 争议"下结论。
    verdict: ✓ 有效 / ⚠ 需CN复测 / ？需App验证 / ✗ 明确失效(仅404等强信号)
    """
    t0 = time.time()
    get = fetch_doh if doh else fetch
    out = {"url": url, "http": "-", "type": "-", "sites": None, "lives": None,
           "parses": None, "jar": "-", "verdict": "", "note": "", "via": "doh" if doh else "system"}
    st, body = get(url, timeout=timeout)
    # 连接层失败 → 30s 复测一次, 把「慢」和「死」分开（App 的超时是 10s/30s 两档）
    if isinstance(st, str):
        st2, body2 = get(url, timeout=max(30, timeout))
        if not isinstance(st2, str):
            st, body = st2, body2
            out["note"] = "30s 复测通过(首轮 %s)" % timeout
    out["http"] = st
    text = body.decode("utf-8", "ignore").lstrip("\ufeff").strip()

    def adopt(alt):
        """采用 DoH 复验结果（规模/jar/type）"""
        for k in ("sites", "lives", "parses", "jar", "type", "http"):
            if alt.get(k) not in (None, "-"):
                out[k] = alt.get(k)

    if st != 200 or not text:
        if isinstance(st, str):
            out["verdict"] = "⚠ 本机不可达(%s)" % st.replace("ERR:", "")
            if not doh:                      # 双通道: DoH 直连再验一次
                alt = check_source(url, deep=False, timeout=timeout, doh=True)
                out["doh"] = alt["verdict"]
                if alt["verdict"].startswith("✓"):
                    out["verdict"] = "✓ 有效(DoH 直连复验)"
                    out["note"] = "本机 DNS/代理通道异常, DoH 直连正常"
                    adopt(alt)
        else:
            out["verdict"] = "✗ 失效(HTTP %s)" % st
        out["ms"] = int((time.time() - t0) * 1000)
        return out

    # ---- L1: 容器解码（严格 JSON → ** base64 → 2423 AES → fix 归一）----
    is_img = (body[:3] == b"\xff\xd8\xff" or body[:4] == b"\x89PNG"
              or body[:2] == b"BM")
    text, hows, trace = decode_container(text, url)
    cfg, how_parse = parse_config(text)

    # 图片/二进制: **先看能不能解出配置**。饭太硬就是把 12232 字符的配置
    # 接在一张 2401 字节的 JPEG 后面（`VjVsmoeD**` 是分隔标记），FongMi 的
    # Decoder 只看 `**` 不看魔数, 所以照常可用。这里以前直接 return,
    # 把一条活配置判成了墓碑页 —— 是整套检测里最贵的一次误判。
    if is_img and cfg is None:
        out["type"] = "图片/二进制"
        _, b2 = get(url, timeout=8, ua=BROWSER_UA)
        html = b"<!DOCTYPE" in b2[:64] or b"<html" in b2[:64]
        out["verdict"] = ("⚠ 站点可达但入口非订阅响应(需CN复测)" if html
                          else "⚠ 入口返回图片占位(需CN复测)")
        if not doh:
            alt = check_source(url, deep=False, timeout=timeout, doh=True)
            out["doh"] = alt["verdict"]
            if alt["verdict"].startswith("✓"):
                out["verdict"] = "✓ 有效(DoH 直连复验)"
                out["note"] = "本机通道被 geo-gate/劫持, DoH 直连正常"
                adopt(alt)
            elif alt.get("type") == "图片/二进制":
                out["verdict"] = "✗ 入口返回图片(无内嵌配置)"
                out["note"] = "DoH 直连仍是纯图片, 且无 ** 标记可解, 排除本机网络因素"
        out["ms"] = int((time.time() - t0) * 1000)
        return out

    if looks_hex_obfuscated(text):
        out["type"] = "加密/混淆"
        out["verdict"] = "？内容非明文(需App内验证)"
        out["ms"] = int((time.time() - t0) * 1000)
        return out

    if cfg is None:
        if "#EXTM3U" in text[:64]:
            out["type"] = "m3u"
            out["sites"] = text.count("#EXTINF")
            out["verdict"] = "✓ 有效" if out["sites"] else "✗ 空m3u"
            out["ms"] = int((time.time() - t0) * 1000)
            return out
        links = [l.strip() for l in text.splitlines() if l.strip().startswith("http")]
        if links:
            out["type"] = "直链列表"
            out["sites"] = len(links)
            out["verdict"] = "✓ 有效"
            out["ms"] = int((time.time() - t0) * 1000)
            return out
        out["type"] = "非订阅"
        out["verdict"] = "✗ 不可识别"
        out["note"] = re.sub(r"\s+", " ", text[:40])
        out["ms"] = int((time.time() - t0) * 1000)
        return out

    how = "+".join(hows) or how_parse
    if is_img:
        out["type"] = "tvbox配置(图片内嵌)"
        how = (how + "+" if how and how != "plain" else "") + "图片内嵌"

    if isinstance(cfg, dict) and isinstance(cfg.get("urls"), list):
        out["type"] = "嵌套聚合"
        out["verdict"] = "✓ 有效"
        out["note"] = "%d条子订阅, 需递归再验" % len(cfg["urls"])
        out["sources"] = [{"name": s.get("name"), "url": s.get("url")}
                          for s in cfg["urls"] if isinstance(s, dict) and s.get("url")]
        out["ms"] = int((time.time() - t0) * 1000)
        return out
    if not isinstance(cfg, dict):
        out["type"] = "JSON数组"
        out["verdict"] = "？非标准结构"
        out["ms"] = int((time.time() - t0) * 1000)
        return out

    sites = cfg.get("sites") or []
    lives = cfg.get("lives") or []
    parses = cfg.get("parses") or []
    jar = cfg.get("spider") or ""
    out["type"] = "tvbox配置"
    out["sites"], out["lives"], out["parses"] = len(sites), len(lives), len(parses)
    out["jar"] = ((jar[4:] if jar.startswith("img+") else jar).split(";")[0]
                  .rsplit("/", 1)[-1][:20]) if jar else "-"

    total = len(sites) + len(lives) + len(parses)
    if total == 0 and not jar:
        out["verdict"] = "✗ 空配置"
        out["ms"] = int((time.time() - t0) * 1000)
        return out

    notes = []
    csp = sum(1 for s in sites if isinstance(s, dict)
              and str(s.get("api") or "").startswith("csp_"))
    if csp and not jar:
        notes.append("%d个csp站点但无spider jar" % csp)
        out["verdict"] = "⚠ 大概率不可用"
    else:
        out["verdict"] = "✓ 有效"
    if str(cfg.get("hasn'tUpdate", "")).lower() == "true":
        notes.append("源标记停止更新")
    if how != "strict":
        notes.append("解析方式:%s" % how)

    if deep:
        d = []
        if jar:
            # jar 可能带 ;md5;HASH / ;pk;KEY / img+ 前缀, 且可以是相对路径
            jraw = jar[4:] if jar.startswith("img+") else jar
            parts = jraw.split(";")
            jurl, jmd5 = parts[0], None
            for i, p in enumerate(parts[1:], 1):
                if p == "md5" and i + 1 < len(parts):
                    jmd5 = parts[i + 1].strip().lower()
            st2, b2 = fetch(urllib.parse.urljoin(url, jurl), timeout=15)
            ok = st2 == 200 and len(b2) > 1024
            tag = "可下载" if ok else "不可用(%s)" % st2
            if ok and jmd5:
                tag += " md5✓" if hashlib.md5(b2).hexdigest() == jmd5 else " md5✗"
            d.append("jar %s(%dKB)" % (tag, len(b2) // 1024))
        if lives:
            lv = lives[0]
            u = lv.get("url") if isinstance(lv, dict) else str(lv)
            if u:
                st2, b2 = fetch(urllib.parse.urljoin(url, u), timeout=8)
                if st2 != 200:
                    d.append("直播源不可达(%s)" % st2)
                elif b"#EXTINF" in b2 or b"#EXTM3U" in b2:
                    d.append("直播m3u频道%d" % b2.count(b"#EXTINF"))
                else:
                    d.append("直播txt直链%d" % len(
                        [l for l in b2.decode("utf-8", "ignore").splitlines()
                         if ".m3u8" in l or ",http" in l]))
        d += probe_sites(sites, url)
        if d:
            notes = d + notes
    out["note"] = "; ".join(notes)
    out["ms"] = int((time.time() - t0) * 1000)
    return out


# 明显不是"影视点播"的站点名/接口特征: 抽到它们去试播只会得到噪音
_NONVOD_RE = re.compile(
    r"听书|有声|小说|评书|相声|戏曲|广播|电台|音乐|MV|漫画|图集|"
    r"直播|斗鱼|虎牙|哔哩|B站|快手|抖音|体育|球|比分|"
    r"豆瓣|热搜|榜单|工具|设置|帮助|公告|备用|测试|福利|导航", re.I)
# 明确的视频点播接口特征(苹果CMS V10 / 常见采集站)
_VODAPI_RE = re.compile(r"provide/vod|/api\.php|ac=|vodtype|vodshow", re.I)


def _site_rank(s):
    """给站点打分: 越像"可直接离机验证的影视点播源"分越高。"""
    api = str(s.get("api") or "")
    name = str(s.get("name") or "")
    sc = 0
    if _NONVOD_RE.search(name):
        sc -= 60
    if _NONVOD_RE.search(api):
        sc -= 40
    if "/live" in api.lower() or "live." in api.lower():
        sc -= 40
    t = s.get("type")
    sc += {1: 10, 0: 6, 4: 4}.get(t if isinstance(t, int) else -1, 0)
    if s.get("searchable") in (1, "1", True):
        sc += 6
    if _VODAPI_RE.search(api):
        sc += 8
    if s.get("quickSearch") in (0, "0", False):
        sc -= 2
    return sc


def _probe_one_site(s, timeout):
    """试播单个站点。返回 ("ok", info) / ("hard", msg) / ("soft", msg)。"""
    api = str(s["api"])
    nm = str(s.get("name") or "?")[:6]
    probe = api + ("&" if "?" in api else "?") + "ac=videolist"
    st, body = fetch(probe, timeout=timeout)
    if st != 200 or not body:
        return ("soft", "%s=接口%s" % (nm, st))
    try:
        lst = (json.loads(body.decode("utf-8", "ignore")).get("list") or [])
    except ValueError:
        return ("soft", "%s=非点播接口" % nm)      # 听书/直播/XML 常见
    if not lst:
        return ("soft", "%s=空列表" % nm)
    media = title = None
    for it in lst[:10]:
        m = re.search(r"(https?://[^\s#$]+?\.(?:m3u8|mp4)[^\s#$]*)",
                      str(it.get("vod_play_url") or ""))
        if m:
            media, title = m.group(1), it.get("vod_name")
            break
    if not media:
        return ("soft", "%s=无直链播放地址" % nm)
    st2, b2 = fetch(media, timeout=timeout)
    if st2 != 200 or not b2:
        return ("hard", "%s=媒体%s" % (nm, st2))
    if not (b"#EXTM3U" in b2[:600] or b"#EXTINF" in b2[:600] or media.endswith(".mp4")):
        return ("hard", "%s=非播放列表" % nm)
    note = "m3u8 可读"
    ff = shutil.which("ffprobe")
    if ff:
        try:
            r = subprocess.run(
                [ff, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                 "-of", "csv=p=0", "-timeout", "8000000", media],
                capture_output=True, timeout=25, text=True)
            streams = [x for x in r.stdout.strip().splitlines() if x]
            if not streams:
                return ("hard", "%s=ffprobe无流" % nm)
            note = "流: " + ",".join(streams[:3])
        except Exception:                              # noqa: BLE001
            note = "m3u8 可读(ffprobe 超时)"
    return ("ok", {"ok": True, "site": s.get("name"), "title": title,
                   "media": media[:140], "note": note})


def probe_play(url, timeout=10, limit=5, deep_limit=12):
    """L4 试播: 配置 → 站点接口 → 取真实 m3u8 → 校验可读(有 ffprobe 再验流)。

    返回 {"ok": True|False|None, "site","title","media","note"}。

    ok 三态, 这个区分很关键(否则会把"没抽到能验的站点"误报成源坏了):
      True  —— 抓到播放地址且媒体可读(有 ffprobe 时确认有音视频流)
      False —— 拿到了播放地址但 CDN 拉不动/不是播放列表(真实死链信号)
      None  —— 无法离机判定: 抽中的站点都不是点播接口(听书/直播/XML)、
               或连接层被拦。需 App 内人工确认。

    两波探测: 先试排序前 limit 个; 若全无结论(只有 soft), 再往后扩到 deep_limit 个。
    因为连接层错误多半是本机链路造成的假阴性, 不该据此就放弃。

    注意: 只代表抽中的那一条线路, 不代表全站可用。
    """
    cfg, how, _ = load_config(url)
    if not isinstance(cfg, dict):
        return {"ok": None, "note": "配置不可解析(%s)" % how}
    sites = [s for s in (cfg.get("sites") or []) if isinstance(s, dict)
             and str(s.get("api") or "").startswith("http")
             and not str(s.get("api") or "").startswith("csp_")]
    if not sites:
        return {"ok": None, "note": "无可离机探测站点(全部依赖 jar, 需 App 内验)"}
    sites.sort(key=_site_rank, reverse=True)

    soft, hard, n = [], [], 0
    for wave in (sites[:limit], sites[limit:deep_limit]):
        for s in wave:
            n += 1
            kind, info = _probe_one_site(s, timeout)
            if kind == "ok":
                return info
            (hard if kind == "hard" else soft).append(info)
            # 单站 404/拉不动 ≠ 整个源坏了: 其它线路可能能播, 继续往后试。
            # 但连吃 3 个硬失败就没必要再烧时间了。
            if len(hard) >= 3:
                break
        if len(hard) >= 3:
            break

    tried = hard + soft
    if hard:
        return {"ok": False, "note": "取到播放地址但拉不动: " + "; ".join(tried[:3])}
    return {"ok": None,
            "note": "未能离机验证(抽 %d 站均非可探点播接口): %s"
                    % (n, "; ".join(soft[:3]) or "无响应")}


def load_config(url):
    """拉取并解析订阅配置 -> (cfg, how, status)。cfg 为 None 表示拿不到订阅结构。
    与 check_source 共用同一套解码: 2423 AES-CBC / Base64 包装 / JSONC。"""
    st, body = fetch(url)
    if st != 200 or not body:
        return None, ("拉取失败(%s)" % (st or "无响应")), st
    text = body.decode("utf-8", "ignore").lstrip("\ufeff").strip()
    text, hows, _ = decode_container(text, url)
    if looks_hex_obfuscated(text):
        return None, "encrypted", st
    cfg, how = parse_config(text)
    return cfg, ("+".join(hows) or how), st


def check_many(items, deep=False, workers=8, timeout=TIMEOUT):
    """items: [url] 或 [(name,url)] -> [(name,url,result)]"""
    def norm(it):
        return it if isinstance(it, (tuple, list)) else (None, it)

    def run(it):
        name, url = norm(it)
        return name, url, check_source(url, deep=deep, timeout=timeout)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(run, items))
