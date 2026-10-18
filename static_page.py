"""把 status.json 渲染成一页静态 HTML（无 JS 依赖，GitHub Pages 直接托管）。"""
import html
import re
import time


def _esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _pill(s):
    if s.get("user_ok"):
        # 人工确认过的源要区分两种情况, 否则面板会把"检测通过"和"人工兜底"混成一个徽章:
        #   last_ok==1 → 两边结论一致, 显示 有效 + 👤(来源可追溯)
        #   last_ok==0 → 只有人工结论, 且要提示可能是缓存假活
        if s.get("last_ok") == 1:
            return ('<span class="pill p-ok">有效</span> '
                    '<span class="pill p-user" title="人工确认过(永不降级), '
                    '当前检测同样通过">👤</span>')
        if s.get("last_ok") is None:
            return '<span class="pill p-user">👤 确认(未检测)</span>'
        return ('<span class="pill p-user">👤 确认</span> '
                '<span class="pill p-warn" title="App 里能用但线上检测失败——'
                '很可能是影视仓的本地缓存，清缓存后可能就打不开了">⚠ 疑似缓存</span>')
    if s.get("last_unreachable"):
        return '<span class="pill p-unk">本机不可达</span>'
    v = s.get("last_verdict") or ""
    if s.get("degraded"):
        return '<span class="pill p-bad">降级</span>'
    if v.startswith("✓"):
        return '<span class="pill p-ok">有效</span>'
    if v.startswith("✗"):
        return '<span class="pill p-bad">失效</span>'
    if v.startswith("⚠"):
        return '<span class="pill p-warn">需复测</span>'
    if not v:
        return '<span class="pill p-unk">未检</span>'
    return '<span class="pill p-unk">待 App 验</span>'


def _spark(seq):
    if not seq:
        return '<span class="dim">—</span>'
    w, h, gap = 4, 14, 1
    bars = []
    for i, v in enumerate(seq[-24:]):
        x = i * (w + gap)
        color = "#16a34a" if v else "#dc2626"
        bars.append('<rect x="%d" y="%d" width="%d" height="%d" rx="1" fill="%s" opacity=".85"/>'
                    % (x, 0 if v else h // 2, w, h if v else h // 2, color))
    total = len(seq[-24:]) * (w + gap)
    return '<svg width="%d" height="%d" viewBox="0 0 %d %d">%s</svg>' % (
        total, h, total, h, "".join(bars))


def render(status, base_url=None):
    """base_url: 部署后的绝对前缀(如 https://u.github.io/repo/subhub)。
    给了就直接写死绝对地址；没给则输出相对地址, 由页面里的 JS 按 location 补齐。"""
    st = status.get("stats") or {}
    gen = status.get("generated_at_str") or time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(status.get("generated_at") or 0))
    srcs = status.get("sources") or []
    # 主源: merge_multi 会拼成 "名字 #id", 这里只要名字, 用来在表里标出来
    mname = re.sub(r" #\d+$", "", str(
        (status.get("tvbox_stats") or {}).get("master") or ""))
    rows = []
    for pos, s in enumerate(srcs, 1):
        up = s.get("uptime")
        # 可用率/历史照实显示 —— 人工确认只影响"要不要降级", 不该把检测历史藏起来
        spark_html = _spark(s.get("spark") or [])
        up_html = "" if up is None else "%d%%" % up
        if s.get("user_ok"):
            up_html = (up_html + " 👤").strip()
        pl = s.get("play")
        if not pl:
            play_html = "<span class='dim'>—</span>"
        elif pl.get("ok") is None:
            play_html = ("<span class='pill p-unk' title='%s'>试播 n/a</span>"
                         % _esc(pl.get("note") or ""))
        elif pl.get("ok"):
            play_html = ("<span class='pill p-ok' title='%s | %s'>▶ 可播</span>"
                         % (_esc(pl.get("title") or ""), _esc(pl.get("note") or "")))
        else:
            play_html = ("<span class='pill p-bad' title='%s'>▶ 失败</span>"
                         % _esc(pl.get("note") or ""))
        rows.append(
            "<tr>"
            "<td class='dim mono'>#%s</td>"
            "<td><b>%s</b></td>"
            "<td>%s</td>"
            "<td class='dim'>%s</td>"
            "<td class='dim mono ell' title='%s'>%s</td>"
            "<td>%s <span class='dim'>%s</span></td>"
            "<td>%s</td>"
            "<td class='dim'>%s</td>"
            "<td class='dim'>%s</td>"
            "</tr>" % (
                # 显示行号而不是源 id: 顺序是按权重算出来的, id 跳号只会让人看不懂
                _esc(pos), _esc(s.get("name")), _pill(s)
                + (' <span class="pill p-user" title="单仓配置的主源, '
                   '它的 spider jar 是 App 唯一能加载的">主源</span>'
                   if s.get("name") == mname else ""),
                ("站%s" % s["sites"] if s.get("sites") is not None else "—")
                + (" · 直播%s" % s["lives"] if s.get("lives") else ""),
                _esc(s.get("url")), _esc(s.get("jar") or "—"),
                spark_html, up_html, play_html,
                _esc((s.get("last_ts") and time.strftime(
                    "%m-%d %H:%M", time.localtime(s["last_ts"]))) or "—"),
                _esc(s.get("origin") or ("feed" if s.get("kind") == "feed" else "手动")),
            ))

    hist = status.get("history") or []
    hist_max = max([h.get("ok") or 0 for h in hist] + [1])
    hbars = []
    for i, h in enumerate(hist[-30:]):
        hh = max(2, int((h.get("ok") or 0) / hist_max * 26))
        hbars.append('<div class="hb" style="height:%dpx" title="%s: %s/%s"></div>'
                     % (hh, time.strftime("%m-%d", time.localtime(h.get("ts") or 0)),
                        h.get("ok"), h.get("total")))
    tv = status.get("tvbox_stats") or {}

    page = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>subhub · TVBox 订阅快照</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--bd:#e5e7eb;--tx:#111827;--dim:#6b7280;
      --ok:#16a34a;--bad:#dc2626;--warn:#b45309;--user:#5b21b6;--acc:#2563eb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
     font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:22px 20px 60px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:20px;margin:0}
.sub{color:var(--dim);font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px}
.stat b{display:block;font-size:22px;line-height:1.2}
.stat span{color:var(--dim);font-size:12px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;
      padding:16px 18px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 10px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:500;font-size:12px;
   border-bottom:1px solid var(--bd);padding:7px 8px;white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid #f1f2f4;vertical-align:middle}
tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.dim{color:var(--dim)}
.ell{max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;white-space:nowrap}
.p-ok{background:#dcfce7;color:#166534}.p-bad{background:#fee2e2;color:#991b1b}
.p-warn{background:#fef3c7;color:#92400e}.p-unk{background:#f3f4f6;color:#4b5563}
.p-user{background:#ede9fe;color:var(--user)}
.copybox{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input{flex:1;min-width:280px;padding:7px 10px;border:1px solid var(--bd);
      border-radius:8px;font:12px ui-monospace,Menlo,monospace;color:#374151;background:#fafafa}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
a.btn{display:inline-block;padding:6px 12px;border:1px solid var(--bd);border-radius:8px;
      background:#fff;font-size:12px;color:#374151}
a.btn:hover{background:#f9fafb;text-decoration:none}
.hist{display:flex;align-items:flex-end;justify-content:flex-start;gap:3px;height:30px;margin-top:6px}
.hb{flex:1;max-width:26px;background:#93c5fd;border-radius:2px 2px 0 0;min-width:3px}
.tip{color:var(--dim);font-size:12px;margin-top:8px}
.foot{color:var(--dim);font-size:12px;margin-top:20px;text-align:center}
@media(max-width:820px){.stats{grid-template-columns:repeat(3,1fr)}
  .ell{max-width:150px}}
</style></head><body><div class="wrap">
<header>
  <h1>📡 subhub</h1>
  <span class="sub">TVBox 订阅快照 · 无服务版</span>
  <span class="sub">生成于 {{gen}}（耗时 {{elapsed}} s）</span>
</header>

<div class="stats">
  <div class="stat"><b>{{total}}</b><span>订阅源</span></div>
  <div class="stat"><b>{{ok}}</b><span>检测有效</span></div>
  <div class="stat"><b>{{merged}}</b><span>进订阅</span></div>
  <div class="stat"><b>{{unreach}}</b><span>本机不可达</span></div>
  <div class="stat"><b>{{deg}}</b><span>已降级</span></div>
  <div class="stat"><b>{{uok}}</b><span>👤 用户确认</span></div>
  <div class="stat"><b>{{playable}}</b><span>▶ 试播通过</span></div>
</div>

<div class="card">
  <h2>订阅地址</h2>
  <div class="copybox">
    <input id="u1" readonly value="{{sub_url}}">
    <a class="btn" href="merged.json">多仓聚合</a>
    <a class="btn" href="merged-manual.json">多仓精选</a>
    <a class="btn" href="tvbox.json">单仓配置</a>
  </div>
  <div class="tip">把输入框里的地址填进 TVBox / FongMi / 影视仓。<br>
    <b>多仓聚合</b> {{merged}} 条（含自动发现的源）·
    <b>多仓精选</b> {{merged_manual}} 条（只含人工种子，不受自动发现影响）·
    <b>单仓配置</b> {{sites}} 站 / {{lives}} 直播（主源 {{master}}）。</div>
</div>

<div class="card">
  <h2>最近 30 轮有效数</h2>
  <div class="hist">{{hbars}}</div>
  <div class="tip">柱高 = 当轮检测有效数（不含本机不可达）。</div>
</div>

<div class="card">
  <h2>源列表（{{total}}）</h2>
  <table>
    <thead><tr><th>#</th><th>名称</th><th>状态</th><th>规模</th>
      <th>jar</th><th>可用率</th><th>试播</th><th>最近</th><th>来源</th></tr></thead>
    <tbody>{{rows}}</tbody>
  </table>
  <div class="tip">「本机不可达」= 执行检测的机器连不上（多为出口网络问题），
    不计失败、不影响订阅；「👤 确认」= 在 App 里实测可用，永久保留。</div>
</div>

<div class="foot">由 GitHub Action 定时生成 · 静态页面，无后端</div>
</div>
{{urljs}}
</body></html>"""
    repl = {
        "gen": _esc(gen), "elapsed": _esc(status.get("elapsed_sec")),
        "total": st.get("total", 0), "ok": st.get("ok", 0),
        "merged": st.get("merged", 0), "unreach": st.get("unreachable", 0),
        "merged_manual": st.get("merged_manual", "—"),
        "deg": st.get("degraded", 0), "uok": st.get("user_ok", 0),
        "playable": st.get("playable", "—"),
        "sites": tv.get("sites", "—"), "lives": tv.get("lives", "—"),
        "master": _esc(mname or "—"),
        "hbars": "".join(hbars), "rows": "".join(rows),
    }
    if base_url:
        repl["sub_url"] = _esc(base_url.rstrip("/") + "/merged.json")
        repl["urljs"] = ""
    else:
        repl["sub_url"] = "merged.json"
        # 相对地址 -> 绝对地址，方便直接复制
        repl["urljs"] = (
            "<script>\n(function(){\n"
            '  var base = location.href.replace(/index\\.html(\\?.*)?$/, "");\n'
            '  var el = document.getElementById("u1");\n'
            '  if (el && !/^https?:/.test(el.value)) el.value = base + el.value;\n'
            "})();\n</script>"
        )
    for k, v in repl.items():
        page = page.replace("{{%s}}" % k, str(v))
    return page
