# 发布说明页 FastAPI 缓存代理（含 gzip 内容编码协商）

为文档站的发布说明页（`/releases`）实现的带内容协商感知的缓存代理，支持
JSON / Markdown 两种正文，并在代理侧按 `Accept-Encoding` 提供 gzip 压缩。

## 架构

```
客户端 ──HTTP──> cache-proxy (FastAPI, :8000) ──回源（恒为原文）──> docs-origin (FastAPI, :8080)
                  内存缓存 + 并发控制 + 代理侧 gzip        可切换 Vary 规则 / 可延迟 / 可注入故障 / 可换代
```

- `app/origin.py`：本地源站。按 `Accept` 选 JSON / Markdown，可切换是否再按
  `Accept-Language` 选语言；管理 API 可注入回包延迟、故障、切换 Vary 规则、
  推进正文版本（revision）。
- `app/proxy.py`：缓存代理，实现全部缓存、并发与 gzip 语义。
- `app/encoding.py`：`Accept-Encoding` 解析、确定性 gzip 压缩、双 ETag 生成
  与 `If-None-Match` 条件判定。
- `app/content.py`：中英文发布说明示例内容。
- `app/tests/selftest.py`：端到端自测（123 项断言，含并发压缩与正文换代）。

## 缓存语义（与需求逐条对应）

1. **按 Vary 查缓存**：每个路径确认一份源站 Vary 规则，按规则中的请求头
   （`Accept`、`Accept-Language`）计算变体键，不同变体分别缓存。
2. **max-age + ETag 复验**：新鲜期内直接命中；过期后带**源站原文 ETag** 回源。
   - 源站回 **304**：沿用发起复验时选中的旧正文（同一个缓存 Entry），只刷新元数据
     （`X-Cache: REVALIDATED_304`）；正文字节未变，已生成的 gzip 继续有效。
   - 源站回 **200**：用新正文替换为新 Entry（`X-Cache: REVALIDATED_200` / `MISS`），
     旧 Entry 及其在途压缩随之作废。
3. **Vary 规则改变整体失效**：回源响应的 Vary 与已确认规则不同时，清空该路径
   旧规则全部条目、generation 递增，本次正文按新规则缓存（`X-Cache: MISS_NEW_RULE`）。
4. **并行回源 + single-flight**：不同协商身份（不同 Accept / 语言）用不同的锁，
   可并行回源；同一身份的并发请求合并为一次回源。
5. **切换时在途请求隔离**：源站通过 `X-Rule-Epoch` 标识规则世代；代理回源前探测
   基准世代，响应世代不一致且规则已被其他请求提交时，该在途答复只返回给它的
   原始请求（`X-Cache: INFLIGHT_RULE_SWITCHED` / `STALE_INFLIGHT_304`），不写入新缓存。
6. **回源失败保状态**：连接错误或源站 5xx 返回 502（`X-Cache: ORIGIN_ERROR`），
   不修改任何已确认的缓存条目与规则；已缓存的新鲜内容仍可正常命中。

## gzip 语义（与需求逐条对应）

1. **按 Accept-Encoding 选表示**：可接受 gzip 时返回压缩字节，否则返回原文；
   不带 `Accept-Encoding` 的朴素客户端恒收原文；两种编码都不可接受时返回 406。
   代理**不向源站转发 `Accept-Encoding`**，回源与缓存恒为原文，压缩只在代理侧发生。
2. **下游 Vary**：对客户端的 `Vary` 头 = 源站协商项（`Accept` /
   `Accept-Language`）+ `Accept-Encoding`，去重后输出。
3. **双 ETag**：原文表示沿用源站 ETag；gzip 表示使用对实际压缩字节取摘要的
   强 ETag（`"gzip-<sha1前16位>"`），两种表示各用对应字节的 ETag。
   压缩采用固定 mtime 的确定性 gzip，同一份原文永远产出同一份压缩字节与 ETag。
4. **客户端条件请求**：`If-None-Match` 只与**本次所选表示**的 ETag 比较
   （支持弱比较与 `*`）。命中返回 304 并带上该表示的 `ETag` /
   `Content-Encoding` / `Vary`；交叉使用（gzip 请求带原文 ETag）不命中、回 200。
5. **一份正文只压一次**：每个缓存 Entry 配独立条件变量，首个 gzip 请求发起压缩
   （在线程池执行，不阻塞事件循环），并发 gzip 请求等待并共用同一份结果；
   原文请求不经过压缩流程，命中时绝不等待任何在途压缩。
6. **回源 304 沿用 gzip**：304 复用同一 Entry，已有 gzip 字节与 gzip ETag 原样保留。
7. **换代隔离**：Vary 切换、清缓存或回源 200 换掉正文后，旧 Entry 上的在途压缩
   只能写回旧 Entry、只服务引用旧正文的那个请求；新正文的 Entry 初始无 gzip，
   首次 gzip 请求重新压缩，不存在把旧压缩写入新缓存的路径。

## 用 Docker Compose 启动

```bash
docker compose up -d --build
# 代理: http://localhost:18000   源站: http://localhost:18080

# API 演示（两种编码、双 ETag 条件复验、304 沿用 gzip、正文换代、规则切换、故障保缓存）
bash demo/api-demo.sh

# 并发与换代自测（123 项断言）
docker compose run --rm self-test
```

## 无 Docker 时本地裸跑

```bash
python3 -m pip install --user virtualenv
python3 -m virtualenv .venv
.venv/bin/pip install -r requirements.txt
bash scripts/start-local.sh
PROXY_BASE=http://127.0.0.1:18000 ORIGIN_BASE=http://127.0.0.1:18080 \
  .venv/bin/python -m app.tests.selftest
bash demo/api-demo.sh
bash scripts/stop-local.sh
```

## 主要 API

| 服务 | 方法/路径 | 说明 |
| --- | --- | --- |
| 代理 | `GET /releases` | 代理入口，响应头 `X-Cache` 标注缓存状态 |
| 代理 | `GET /_cache/inspect` | 查看各路径的 Vary、generation、双 ETag、gzip 状态与统计 |
| 代理 | `POST /_cache/clear` | 清空缓存 |
| 代理 | `POST /_cache/age` | 测试辅助：把条目录龄（模拟 max-age 到期） |
| 代理 | `POST /_test/compress_delay` | 测试辅助：设置 gzip 压缩前人为延迟（毫秒） |
| 源站 | `POST /admin/vary/{accept,both}` | 切换 Vary 规则 |
| 源站 | `POST /admin/rev` | 正文换代：渲染内容与 ETag 同时变化 |
| 源站 | `POST /admin/delay/{ms}` | 设置回包延迟 |
| 源站 | `POST /admin/fail/{n}` | 接下来 n 个请求返回 500 |
| 源站 | `GET /admin/wait_inflight/{n}` | 测试同步：等待 n 个请求在途 |
| 源站 | `GET /admin/state` | 源站状态与请求计数 |

## 已知边界

- 缓存为进程内内存存储，重启即失效、不支持多实例共享。
- 仅代理 GET；`/_cache/*`、`/_test/*` 管理接口无鉴权，仅适用于本地演示。
- 内容编码只实现 gzip 与 identity（原文）；不支持 br / zstd 等其他编码。
- gzip 为惰性生成并常驻内存，没有压缩块的单独淘汰策略（条目淘汰时一并释放）。
- 为识别“回源在途期间的规则切换”，代理每次回源前会额外请求一次源站
  `/rule_epoch` 探测基准世代；该端点很轻量，但若源站不支持等价机制，
  在途切换的精确识别只能退化为按 Vary 头比对。
