# tvbox-subhub

TVBox / FongMi / 影视仓 订阅的**自动检测 + 自动拼装**。每天跑一轮，产出一页可读的体检面板和三个可直接用的订阅地址。

零第三方依赖（Python 3.10+ 标准库），不需要 pip install，不需要数据库。

## 三个订阅地址

部署后（GitHub Pages，从 `/docs` 发布）：

| 产物 | 地址 | 给谁用 |
|---|---|---|
| **多仓聚合** | `https://<user>.github.io/tvbox-subhub/merged.json` | 影视仓 / 支持多仓的 TVBox。`{"urls":[...]}`，含自动发现的源 |
| **多仓精选** | `https://<user>.github.io/tvbox-subhub/merged-manual.json` | 同上，但**只含人工种子**，不受自动发现影响 |
| **单仓配置** | `https://<user>.github.io/tvbox-subhub/tvbox.json` | FongMi / 原生 TVBox。合并成一份完整配置，主源 jar 已选定 |

面板：`https://<user>.github.io/tvbox-subhub/`

**为什么是两个多仓**：`feed` 类源是每轮从父订阅里自动 diff 出来的，作者改一次你的列表就变一次。
想要稳定集合的人用精选版，想尽量全的人用聚合版。数据都来自同一个 `state/sources.json`。

**为什么单仓只有一个**：一份 tvbox 配置只能加载**一个** spider jar，而配置里大部分 `csp_*` 站点
依赖这个 jar。主源一挂，这些站点全废。所以主源的选择比多一个文件更重要 ——
选法见下面「排序」。

## 排序规则

订阅和面板用同一套权重（`merge.rank_key`），不只看手调的 `sort_order`：

```
0  人工置顶 + 当前检测有效      ← 最高
1  当前检测有效                ← 按 sort_order 排
2  人工确认但检测没过（兜底）    ← 仍进订阅，排最后
3  未验证 / 失效               ← 不进订阅
```

**为什么不用纯 `sort_order`**：源会自然腐坏。手调一次之后，失效的源还占着前排，
而新冒出来的好源永远在后面。所以按「当前结论」分档，再用 `sort_order` 做**档内**微调，
两者各管一段。同一档内按 `sort_order` 再按 `id`，每轮结果稳定可复现。

主源（`tvbox.json` 的 spider jar）取同一序列里的第一个 —— 所以把某个源顶上去，
它就可能成为主源，连带改变 200 多个站点的可用性。想固定主源就显式设置 `single_master`。

## 每天一次，不是持续监控

Action 模式是**快照**。有三个东西让「一天一次」不至于太脆：

- **last-good-wins**：连接层失败不计失败、不降级，沿用上次真实结论
- **轮内重试**：连接失败 30s 复测一次，再不行走 DoH 直连复验
- **cron 可调密**：`0 */6 * * *` 就是每 6 小时（代价是提交噪音）

要真正的分钟级监控，跑本机 `server.py` 或一台 VPS —— 见下面「三种跑法」。
另外 GitHub cron 本身会延迟 5~20 分钟（高峰期更久），别当准点任务依赖。

## 检测怎么判

五层，L0→L4：

1. **L0 连通**：HTTP 200 且非空。连接类错误只代表本机 vantage，不判死。
2. **L1 容器**：严格按 FongMi `Decoder.java:31` 的分派顺序 ——
   `isObj`（严格 JSON）→ 含 `**` 走 base64 → `2423` 走 AES-CBC → `fix()` 相对路径归一。
   **顺序本身就是逻辑**，见下面「踩过的坑」。
3. **L2 结构**：sites / lives / parses / jar 非空；有 `csp_*` 但无 spider jar → 大概率不可用。
4. **L3 内容**（`--deep`）：jar 可下载性 + `;md5;` 校验、直播频道数、点播接口条数。
5. **L4 试播**（`--play`）：站点 `ac=videolist` → 抽一条真实 `m3u8/mp4` → 校验播放列表，
   有 `ffprobe` 再验音视频流。**这是「真能播」的唯一离机证据**，但只代表抽中的那条线路。

L4 是**三态**，这个区分是刻意的：

| 判定 | 含义 | 面板 |
|---|---|---|
| `True` | 抓到播放地址且媒体可读（有 ffprobe 时确认 h264/aac） | `▶ 可播` |
| `False` | 拿到播放地址但 CDN 拉不动（404 / 非播放列表 / 解不出流） | `▶ 失败` |
| `null` | 无法离机判定：全是 `csp_*`+jar、或抽中听书/直播/XML 接口 | `试播 n/a` |

`False` 只在「已经拿到真实播放地址、但 CDN 拒绝」时给出。其余一律 `n/a` 交给 👤 确认 ——
不假装检测能证明播放能力。实测 26 个源里 8 个是纯 jar 配置，**结构上就不可能在设备外验证**。

## 踩过的坑（都是会误判的那种）

**1. 图片魔数短路在 base64 之前** —— 最贵的一次误判。

饭太硬的入口是 `ffd8ffe0` 开头的一张真 JPEG，配置接在 2401 字节之后：

```
ffd8ffe0 ... 2401 字节 JPEG ... "VjVsmoeD**" + base64(12232 字符 JSONC)
```

连它的 spider 都是 `.../posters/xxx.jpg;md5;aa8d3752...` —— 一个 `.jpg` URL 返回 1.25MB 的 zip。
`Decoder` 从来不看魔数，只看 `**`。旧代码先判 `body[:3] == ffd8ff` 就 return「图片占位」，
把一条 49 站点的活配置判成了作者弃坑。**现在只在解不出配置时才把图片作为判定依据。**

**2. 两个解析器不是同一个**。`isObj` 用 `org.json.JSONObject`（严格），L3 用 Gson（lenient）。
带 `//` 注释的配置在 ① 判 false、在 L3 判 true，所以它会掉进 ②/③ 再被捞回来。
用严格解析器判「格式坏了」会误杀大量正常配置。

**3. `**` 标记要在字符串上找，不能拿字节偏移去减**。标记前的二进制被折叠成 U+FFFD，
字节 2401 对应字符 2286 左右。先 UTF-8 解码，再在字符串上 `indexOf` + 10 切片。

**4. OkHttp 透明解 gzip，urllib 不解**。漏了这一步，26 条里 19 条会显示「解不开」。
同理 `Content-Length` 要校验 —— 截断的响应会伪装成「一张图片」。

**5. `fix()` 不能省**。配置里写 `./js/xx.js` 时 App 会按订阅 URL 补全。
老刘备有 189 处、香雅情 51 处，不补全就是一堆坏路径。

**6. jar 可能是相对路径、可能伪装成图片、可能 md5 对不上**。老刘备是 `./fty.jar`；
饭太硬/王二小/小马是 `.jpg`；南风和健康家用声明了 `;md5;` 但对不上。
这些都要单独看，不能用「jar 能下载」一句糊过去。

**7. 缓存陷阱**。影视仓按 `md5(api_url)` 缓存配置，请求失败时**静默回退到缓存**。
所以「电视上还能看」≠「线上还活着」。验证前先清缓存（或给地址加个 `?t=1`）。
**但反过来也成立**：不要因为「入口返回图片」就断定作者弃坑 —— 先确认自己没漏掉解码分支。

## 三种跑法

| 模式 | 适合 | 状态存储 | 触发 | 代价 |
|---|---|---|---|---|
| **A. GitHub Action**（推荐） | 只要「每天自动更新订阅」 | 仓库里的 JSON | cron 每天 00:00 北京时间 | 免费；延迟 5~20 分钟 |
| **B. 本机常驻** `server.py` | 要可视化控制台、手动点按钮 | SQLite | 进程内调度（默认 10 分钟） | 要一台常开的机器 |
| **C. 一次性容器** | 已有 cron / 调度器 | 挂载卷里的 JSON | 外部触发 | 同 B，但要自己配调度 |

数据量很小（26 源 + 30 轮历史 ≈ 100KB），**仓库本身就是 KV** —— A 模式不需要任何外部存储。

### A. GitHub Action

1. 推仓库，`.github/workflows/daily.yml` 已就位（每天 00:00 北京时间 + 手动触发）
2. Settings → Pages → Source: *Deploy from a branch* → `main` / `/docs`
3. 把订阅地址填进 TVBox / 影视仓

三个必须知道的：

- **cron 只在默认分支生效**，且会延迟
- **Action 出口在 Azure（非 CN）**：被 geo-gate 的源会和本地一样报「本机不可达」，
  按 last-good-wins 保留上次结论
- 私有仓库的 Pages 需要 GitHub Pro，且 TVBox 拉 raw 链接带不了 token → 建议公开仓库

### B. 本机常驻

```bash
python3 server.py                 # http://127.0.0.1:8765
SUBHUB_PORT=9000 python3 server.py
nohup python3 server.py > subhub.log 2>&1 &    # 开机自启用 launchd, KeepAlive=true
```

### C. 单轮 worker

```bash
python3 run_once.py --deep --play --workers 8   # 完整一轮（含试播，约 3~4 分钟）
python3 run_once.py --deep                      # 跳过试播，约 1 分钟
python3 run_once.py --strict                    # 未检测过的源不进订阅
python3 run_once.py --out /tmp/site --no-feeds
python3 run_once.py --base-url https://u.github.io/tvbox-subhub
```

`--base-url` 会把部署后的绝对地址写进面板的订阅地址框（也可用环境变量 `SUBHUB_BASE_URL`）。
不给则输出相对地址，由页面 JS 按 `location` 补齐。Action 里已自动从 `github.repository` 推导。

### D. Docker

```bash
docker compose up -d --build      # restart: unless-stopped
```

`Dockerfile` 是 `python:3.13-slim`，零依赖无 pip install。DB 通过 `SUBHUB_DB=/data/subhub.db`
放挂载卷。compose 与 env 已验证，**镜像构建未验证**（本机 daemon 当时未启动）。

## 手改 state

Action 模式没有界面，但所有开关都是 `state/` 下的普通 JSON，改完提交即生效：

```jsonc
// state/sources.json
{
  "name": "某源",
  "sort_order": 1,     // 档内排序权重（同档才比大小）
  "enabled": true,     // false = 停用，不进订阅
  "user_ok": true      // 人工确认：永不降级 + 置顶 + 强制进订阅
}
```

- `feeds.json` — 固定资源（父订阅页），每轮自动 diff 出新源
- `history.jsonl` — 每轮一行，保留最近 120 轮。被作废的记录保留 `invalidated` 字段说明原因

## 诊断工具

`fongmi_decode.py` 是 `Decoder.java` 的**忠实移植，只解析不判定** —— 用来回答
「按 App 的代码走，这条配置到底能解出什么」：

```bash
python3 fongmi_decode.py http://www.饭太硬.net/tv
python3 fongmi_decode.py --state state/sources.json          # 批量
python3 fongmi_decode.py <url> --follow                      # Depot 递归展开
python3 fongmi_decode.py <url> --json out.json --quiet
```

逐层打印 L0 取数 / L1 容器 / L3 结构 / L4 站点 / L5 jar / L6 ext。
和 `checker.py` 的区别：它不给结论，只给中间态。

## API（server.py 模式）

```
GET    /api/state                   全部状态
POST   /api/sources                 {name,url} 添加源
PUT    /api/sources/{id}            {enabled:0|1} 启停
DELETE /api/sources/{id}
POST   /api/sources/{id}/check      单条检测
POST   /api/sources/{id}/move       {dir:-1|1} 档内上下移
POST   /api/sources/{id}/override   {ok:1|0} 人工确认 / 取消
POST   /api/check-all               立即跑一轮
POST   /api/feeds                   添加固定资源并同步
POST   /api/settings                {interval_minutes,deep_check,single_master,...}
GET    /api/subscribe/merged        多仓聚合（?kind=feed|manual 过滤）
GET    /api/subscribe/tvbox         单仓合并配置
GET    /api/subscribe/tvbox/stats   单仓合并统计
```

## 已知边界

- **L6 拿不到**：`ext` 密文和 `.guard` 里的真 dex，App 层都不碰 ——
  `ext` 非 http 就原样丢给爬虫内部解。要往下只有三条路：读设备日志
  （`TV-search` / `TV-SpiderDebug`）、hook `Cipher.doFinal` + `DexClassLoader`、静态反汇编 `.so`。
  离机解析到 L5 就是顶。
- **stub 壳**：部分 jar 是 `classes.dex`(36KB) + `.guard` + `.so`，真 dex 由 native 解出。
  静态扫 `csp_` 类名必然是 0，**这不代表配置造假**。实测 4 个源是这样。
- 嵌套聚合（`urls` 套 `urls`）检测到即用，不递归拆解。
- 单仓合并只并入主源 jar 能解析的站点：跨源的 `csp_*` 会被丢弃并计数（jar 机制决定，绕不过）。
- `;pk;`（AES-ECB，影视仓支持、FongMi 不认）和 `img+` 前缀已实现但**未在设备上验证**；
  实测 26 个源里一次都没出现。
- TLS 不校验证书（与 App 行为一致）：自签/过期证书不会被判死，但也不保证链路安全。

## 免责

只做订阅地址的可用性检测与聚合，不存储、不分发任何音视频内容，不内置任何片源。
检测对象均为公开可访问的 URL，内容版权归原站所有。请自行确认所在地区的合规要求。
