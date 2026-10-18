"""固定资源（父订阅页 / 聚合页）解析，纯函数无状态。

server.py 与 run_once.py 共用：从 HTML 文章页或 JSON 聚合页里挖出配置 URL。
"""
import json
import re

# 导航/工具/图床/静态资源链接一律忽略
FEED_URL_BLACKLIST = re.compile(
    r"(baidu\.com|quark\.cn|tjkapp|xiaomark|ghproxy\.net|qiqiv\.cn|"
    r"cctv\.com|foodieguide|henghost|ksust|adzhp|serv00|gitlink|imagehub|"
    r"github\.com/FongMi|\.css|\.js|\.png|\.jpe?g|\.webp|\.svg|\.ico|\.html?$)", re.I)

# 只留像"配置入口"的 URL
CONFIG_HINT = re.compile(
    r"(\.(json|txt|php|tv|webp)(\?|$)|/tv\b|tvbox|/box\b|/dc\b|provide/tvbox|/c/\d)",
    re.I)


def extract_feed_urls(raw_bytes, base_url="", content_type=""):
    """从固定资源提取 [(name, url)]。
    支持: JSON 聚合(urls[]) / HTML 文章页(data-clipboard-text + 裸 URL)。"""
    text = raw_bytes.decode("utf-8", "ignore")
    # 1) JSON 聚合页
    try:
        cfg = json.loads(text)
        if isinstance(cfg, dict) and isinstance(cfg.get("urls"), list):
            return [(str(s.get("name") or "").strip()[:30], s["url"])
                    for s in cfg["urls"]
                    if isinstance(s, dict) and str(s.get("url", "")).startswith("http")]
    except ValueError:
        pass
    # 2) HTML 页
    urls = []
    for m in re.findall(r'data-clipboard-text=["\']([^"\']+)["\']', text):
        if m.startswith("http"):
            urls.append(m)
    urls += re.findall(r'https?://[^\s"\'<>)\]）]+', text)
    seen, out = set(), []
    for u in urls:
        u = u.rstrip(".,;，。")
        if u in seen or FEED_URL_BLACKLIST.search(u) or not CONFIG_HINT.search(u):
            continue
        seen.add(u)
        out.append(("", u))
    return out
