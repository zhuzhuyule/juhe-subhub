"""订阅合并逻辑（纯函数，无 DB / 无网络）。

server.py（本地常驻模式）和 run_once.py（worker / GitHub Action 模式）共用，
避免两套合并规则各写一遍产生漂移。
"""
import urllib.parse as up


def rank_key(s):
    """订阅与面板统一的排序权重 —— 一处定义, 三处共用(多仓/主源/面板)。

    0 人工置顶且验证有效   >  1 验证有效  >  2 人工兜底但检测未过  >  3 其他
    同档内按 sort_order(手调) 再按 id, 保证每轮结果稳定可复现。

    为什么不用纯 sort_order: 源会自然腐坏, 手调一次之后失效的源仍会占着前排。
    按"当前结论"分档, 再用手调的 sort_order 做档内微调, 两者各管一段。
    """
    ok = s.get("last_ok")
    uok = bool(s.get("user_ok"))
    if uok and ok == 1:
        tier = 0
    elif ok == 1:
        tier = 1
    elif uok:
        tier = 2
    else:
        tier = 3
    return (tier, s.get("sort_order") or 0, s.get("id") or 0)


def merge_multi(entries, cap=50, kind=None, include_degraded=False):
    """多仓聚合（juhe 格式）: {"urls":[{name,url}]}

    entries: [{id,name,url,kind,sort_order,user_ok,degraded,last_ok,enabled}]
    规则: 用户确认(user_ok) 或 上次检测有效(last_ok==1) 才进; 降级源踢出。
    """
    out = []
    for s in sorted(entries, key=rank_key):
        if not s.get("enabled", True):
            continue
        if kind in ("feed", "manual") and s.get("kind") != kind:
            continue
        trusted = bool(s.get("user_ok"))
        if s.get("degraded") and not (trusted or include_degraded):
            continue
        if not trusted and s.get("last_ok") != 1 and not include_degraded:
            continue
        out.append({"name": "%s #%s" % (s.get("name") or "源", s.get("id", "?")),
                    "url": s["url"]})
    return {"urls": out[:cap]}


def merge_single(master, others, want_lives=True, want_parses=True):
    """单仓 tvbox 配置: 主源 jar 唯一可加载, 跨源只并入非 csp 且绝对 URL 的站点。

    master: {name,id,url,cfg};  others: [{url,cfg}]
    返回 (config, stats)；master.cfg 非 dict 时返回 (None, {"error":...})
    """
    cfg = master.get("cfg")
    if not isinstance(cfg, dict):
        return None, {"error": "主源无法解析: %s" % master.get("name")}
    jar = str(cfg.get("spider") or "")
    out = {"spider": up.urljoin(master["url"], jar.split(";")[0]) if jar else "",
           "sites": [], "lives": [], "parses": [], "flags": []}
    seen_keys, dropped_csp, skipped, dup = set(), 0, 0, 0

    def absorb_sites(c, is_master):
        nonlocal dropped_csp, dup
        for site in c.get("sites") or []:
            if not isinstance(site, dict):
                continue
            key = str(site.get("key") or "")
            api = str(site.get("api") or "")
            if key and key in seen_keys:
                dup += 1
                continue
            if api.startswith("csp_"):
                if not is_master:
                    dropped_csp += 1  # 别的 jar 解析不了
                    continue
            elif not is_master and not api.startswith("http"):
                continue  # 相对路径站点只在主源内有效
            if key:
                seen_keys.add(key)
            out["sites"].append(site)

    absorb_sites(cfg, True)
    if want_lives:
        out["lives"] += cfg.get("lives") or []
    if want_parses:
        out["parses"] += cfg.get("parses") or []
    for f in cfg.get("flags") or []:
        if f not in out["flags"]:
            out["flags"].append(f)

    for o in others:
        c2 = o.get("cfg")
        if not isinstance(c2, dict):
            skipped += 1
            continue
        absorb_sites(c2, False)
        if want_lives:
            out["lives"] += c2.get("lives") or []
        if want_parses:
            for p in c2.get("parses") or []:
                if p not in out["parses"]:
                    out["parses"].append(p)
        for f in c2.get("flags") or []:
            if f not in out["flags"]:
                out["flags"].append(f)

    out["wallpaper"] = " "
    stats = {"master": "%s #%s" % (master.get("name"), master.get("id", "?")),
             "jar": out["spider"].rsplit("/", 1)[-1] or "(无)",
             "sites": len(out["sites"]), "lives": len(out["lives"]),
             "parses": len(out["parses"]), "dropped_csp": dropped_csp,
             "dup_keys": dup, "skipped_sources": skipped}
    return out, stats
