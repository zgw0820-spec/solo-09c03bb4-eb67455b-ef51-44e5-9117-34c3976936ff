"""端到端自测：Vary 缓存、304 复验、规则切换失效、并发切换、回源失败，
以及 gzip 内容编码协商、双 ETag 条件请求与并发压缩隔离。

运行方式：
  Docker:   docker compose run --rm self-test
  本地裸跑: PROXY_BASE=http://127.0.0.1:18000 ORIGIN_BASE=http://127.0.0.1:18080 \
            python -m app.tests.selftest
"""
from __future__ import annotations

import asyncio
import gzip
import http.client
import os
import sys
import time
from urllib.parse import urlparse

import httpx

PROXY = os.environ.get("PROXY_BASE", "http://127.0.0.1:18000").rstrip("/")
ORIGIN = os.environ.get("ORIGIN_BASE", "http://127.0.0.1:18080").rstrip("/")

JSON = "application/json"
MD = "text/markdown"
ZH = "zh-CN"
EN = "en"
GZIP_HDR = {"Accept-Encoding": "gzip"}
IDENTITY_HDR = {"Accept-Encoding": "identity"}

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class RawResponse:
    """原始 HTTP 回包：不自动解压、不注入默认 Accept-Encoding。"""

    def __init__(self, status: int, headers: list[tuple[str, str]], content: bytes):
        self.status_code = status
        self.headers = {k.lower(): v for k, v in headers}
        self.content = content


def _raw_get_sync(url: str, headers: dict | None) -> RawResponse:
    u = urlparse(url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30)
    path = u.path + (f"?{u.query}" if u.query else "")
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    headers = resp.getheaders()
    conn.close()
    return RawResponse(resp.status, headers, data)


async def raw_get(path: str, headers: dict | None = None) -> RawResponse:
    return await asyncio.to_thread(_raw_get_sync, f"{PROXY}{path}", headers)


async def reset(c: httpx.AsyncClient) -> None:
    await c.post(f"{ORIGIN}/admin/reset")
    await c.post(f"{PROXY}/_cache/clear")
    await c.post(f"{PROXY}/_test/compress_delay", json={"ms": 0})
    # 确认两侧均已归零，避免跨用例污染统计
    origin = await origin_state(c)
    assert origin["counters"]["requests"] == 0 and origin["inflight"] == 0, origin
    cache = (await c.get(f"{PROXY}/_cache/inspect")).json()
    assert cache["paths"] == {}, cache


async def origin_state(c: httpx.AsyncClient) -> dict:
    return (await c.get(f"{ORIGIN}/admin/state")).json()


async def proxy_stats(c: httpx.AsyncClient) -> dict:
    return (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]["stats"]


def merged(base: dict | None = None, **kw) -> dict:
    out = dict(base or {})
    out.update(kw)
    return out


async def test_content_negotiation_and_hit(c: httpx.AsyncClient) -> None:
    print("[1] 按 Accept / Accept-Language 协商，第二次命中缓存")
    await reset(c)
    r1 = await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": ZH})
    r2 = await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": EN})
    r3 = await c.get(f"{PROXY}/releases", headers={"Accept": MD, "Accept-Language": ZH})
    check("zh/json 返回中文", "发布说明" in r1.text and r1.headers["content-type"].startswith(JSON))
    check("en/json 返回英文", "Release Notes" in r2.text)
    check("zh/md 返回 markdown", r3.headers["content-type"].startswith("text/markdown") and "# 发布说明" in r3.text)

    r1b = await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": ZH})
    check("相同变体二次请求 HIT", r1b.headers.get("x-cache") == "HIT", r1b.headers.get("x-cache"))
    check("HIT 正文与首次一致", r1b.content == r1.content)

    st = await origin_state(c)
    check("命中不回源（源站计数=3）", st["counters"]["requests"] == 3, str(st["counters"]))

    inspect = (await c.get(f"{PROXY}/_cache/inspect")).json()
    entries = inspect["paths"]["/releases"]["entries"]
    check("按 Vary 缓存 3 个变体", len(entries) == 3, str(entries))
    check("已确认源站 Vary 为两个头", inspect["paths"]["/releases"]["confirmed_vary"] == ["accept", "accept-language"])
    check("下游 Vary 含编码与源站协商项",
          r1b.headers["vary"] == "Accept, Accept-Language, Accept-Encoding", r1b.headers["vary"])


async def test_revalidation_304(c: httpx.AsyncClient) -> None:
    print("[2] max-age 到期带 ETag 复验，304 沿用旧正文")
    await reset(c)
    headers = {"Accept": JSON, "Accept-Language": ZH}
    first = await c.get(f"{PROXY}/releases", headers=headers)
    etag = first.headers["etag"]
    check("首请求带 ETag", bool(etag))

    aged = await c.post(f"{PROXY}/_cache/age", json={"seconds": 31})
    check("缓存快进成功", aged.status_code == 200, aged.text)

    second = await c.get(f"{PROXY}/releases", headers=headers)
    check("复验走 304", second.headers.get("x-cache") == "REVALIDATED_304", second.headers.get("x-cache"))
    check("304 沿用原正文", second.content == first.content)
    check("复验后恢复新鲜", (await c.get(f"{PROXY}/releases", headers=headers)).headers.get("x-cache") == "HIT")
    st = await origin_state(c)
    check("源站收到条件请求并回 304", st["counters"]["status304"] >= 1, str(st["counters"]))


async def test_vary_switch_invalidation(c: httpx.AsyncClient) -> None:
    print("[3] Vary 规则切换：旧规则条目整体失效，按新规则缓存")
    await reset(c)
    h_zh = {"Accept": JSON, "Accept-Language": ZH}
    h_en = {"Accept": JSON, "Accept-Language": EN}
    await c.get(f"{PROXY}/releases", headers=h_zh)
    await c.get(f"{PROXY}/releases", headers=h_en)
    before = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("切换前 2 个语言变体", before["entry_count"] == 2 and before["generation"] == 0)

    # 规则只能在回源时确认：先让缓存过期，复验才会带着新 Vary 回来
    await c.post(f"{PROXY}/_cache/age", json={"seconds": 31})
    await c.post(f"{ORIGIN}/admin/vary/accept")
    r = await c.get(f"{PROXY}/releases", headers=h_zh)
    check("复验发现 Vary 变化，按新规则回源", r.headers.get("x-cache") == "MISS_NEW_RULE", r.headers.get("x-cache"))
    check("accept 模式语言被忽略，回英文", "Release Notes" in r.text)

    after = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("旧条目已清空，只剩 1 条", after["entry_count"] == 1, str(after["entries"]))
    check("generation 递增", after["generation"] == 1, str(after["generation"]))
    check("新 Vary 只剩 accept", after["confirmed_vary"] == ["accept"], str(after["confirmed_vary"]))

    # 新规则下，不同 Accept-Language 映射到同一个变体键 -> HIT 同一份英文正文
    r2 = await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": ZH})
    check("新规则下语言不参与键，直接 HIT", r2.headers.get("x-cache") == "HIT", r2.headers.get("x-cache"))
    check("HIT 正文为英文", r2.content == r.content)


async def test_parallel_languages(c: httpx.AsyncClient) -> None:
    print("[4] 不同语言并行回源（single-flight 只合并同变体）")
    await reset(c)
    await c.post(f"{ORIGIN}/admin/delay/500")

    async def get(lang: str) -> httpx.Response:
        return await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": lang})

    t0 = time.monotonic()
    zh1, zh2, en1 = await asyncio.gather(get(ZH), get(ZH), get(EN))
    elapsed = time.monotonic() - t0
    check("两个语言都成功", zh1.status_code == 200 and en1.status_code == 200)
    check("同语言并发只回源一次（正文一致）", zh1.content == zh2.content)
    check("两种语言正文不同", zh1.content != en1.content)
    check("并行而非串行（耗时 < 1.3s）", elapsed < 1.3, f"{elapsed:.2f}s")
    # 等待在途回源全部结束再核对计数，避免统计窗口竞态
    for _ in range(100):
        st = await origin_state(c)
        if st["inflight"] == 0:
            break
        await asyncio.sleep(0.05)
    check("源站只收到 2 个请求（同变体合并）", st["counters"]["requests"] == 2, str(st["counters"]))
    await c.post(f"{ORIGIN}/admin/delay/0")


async def test_concurrent_switch_inflight(c: httpx.AsyncClient) -> None:
    print("[5] 并发切换（冷回源）：切换瞬间两个在途回源，负方答复只给原请求不进缓存")
    await reset(c)
    await c.post(f"{ORIGIN}/admin/delay/800")
    h_zh = {"Accept": JSON, "Accept-Language": ZH}
    h_en = {"Accept": JSON, "Accept-Language": EN}

    async def get(headers: dict) -> httpx.Response:
        return await c.get(f"{PROXY}/releases", headers=headers)

    # 两个不同语言的冷请求并行在途（不同身份键，代理并行放行）
    t_zh = asyncio.create_task(get(h_zh))
    t_en = asyncio.create_task(get(h_en))
    # 精确等待两个回源都进入源站延迟窗口，再切换规则
    w = await c.get(f"{ORIGIN}/admin/wait_inflight/2")
    check("两个回源确认在途", w.json().get("ok") is True, w.text)
    await c.post(f"{ORIGIN}/admin/vary/accept")  # 在途期间切换规则
    r_zh, r_en = await asyncio.gather(t_zh, t_en)

    statuses = {r_zh.headers.get("x-cache"), r_en.headers.get("x-cache")}
    check("两个在途请求都成功", r_zh.status_code == 200 and r_en.status_code == 200)
    check("恰好一个成为新规则条目，另一个只答复原请求",
          statuses == {"MISS_NEW_RULE", "INFLIGHT_RULE_SWITCHED"}, str(statuses))
    # 新规则 accept-only 下源站统一回英文；关键是两个请求都拿到了完整答复，
    # 且负方（INFLIGHT_RULE_SWITCHED）的结果没有写入共享缓存
    loser = r_zh if r_zh.headers.get("x-cache") == "INFLIGHT_RULE_SWITCHED" else r_en
    winner = r_en if loser is r_zh else r_zh
    check("负方答复只给原请求（完整英文正文）", "Release Notes" in loser.text and loser.status_code == 200)
    check("胜方按新规则缓存（英文正文）", "Release Notes" in winner.text)

    inspect = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("新缓存只有 1 条（负方在途结果未写入）", inspect["entry_count"] == 1, str(inspect["entry_count"]))
    check("缓存条目属于新 generation", inspect["entries"][0]["generation"] == 1,
          str(inspect["entries"][0]["generation"]))
    await c.post(f"{ORIGIN}/admin/delay/0")


async def test_concurrent_switch_during_revalidation(c: httpx.AsyncClient) -> None:
    print("[6] 并发切换（304 复验）：同一英文表示在规则切换后复验命中 304，旧正文只给复验方")
    await reset(c)
    await c.post(f"{ORIGIN}/admin/delay/800")
    h_zh = {"Accept": JSON, "Accept-Language": ZH}
    h_en = {"Accept": JSON, "Accept-Language": EN}
    # 两种语言都预热（旧规则 both）
    await c.get(f"{PROXY}/releases", headers=h_zh)
    warm_en = await c.get(f"{PROXY}/releases", headers=h_en)
    await c.post(f"{PROXY}/_cache/age", json={"seconds": 31})

    async def get(headers: dict) -> httpx.Response:
        return await c.get(f"{PROXY}/releases", headers=headers)

    # 两个过期变体并行复验；en 在 accept 模式下表示不变 -> 304；zh 表示消失 -> 200
    t_zh = asyncio.create_task(get(h_zh))
    t_en = asyncio.create_task(get(h_en))
    w = await c.get(f"{ORIGIN}/admin/wait_inflight/2")
    check("两个复验回源确认在途", w.json().get("ok") is True, w.text)
    await c.post(f"{ORIGIN}/admin/vary/accept")
    r_zh, r_en = await asyncio.gather(t_zh, t_en)

    check("两个复验请求都拿到 200 答复", r_zh.status_code == 200 and r_en.status_code == 200)

    winner = r_zh if r_zh.headers.get("x-cache") == "MISS_NEW_RULE" else r_en
    loser = r_en if winner is r_zh else r_zh
    check("恰好一个请求提交新规则（MISS_NEW_RULE）",
          winner.headers.get("x-cache") == "MISS_NEW_RULE", winner.headers.get("x-cache"))
    check("胜方按新规则缓存英文正文", "Release Notes" in winner.text)
    check("负方标记为在途切换（304 或 200 透传，均不进缓存）",
          loser.headers.get("x-cache") in ("STALE_INFLIGHT_304", "INFLIGHT_RULE_SWITCHED"),
          loser.headers.get("x-cache"))
    if loser.headers.get("x-cache") == "STALE_INFLIGHT_304":
        # en 复验命中 304：沿用发起复验时选中的英文旧正文
        check("304 负方沿用自己的旧正文", loser.content == warm_en.content)
    else:
        # zh 的表示在新规则下不存在，源站回 200：负方仍拿到完整答复
        check("200 负方拿到完整英文正文", "Release Notes" in loser.text)

    inspect = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("缓存中只有新规则的 1 条", inspect["entry_count"] == 1, str(inspect["entry_count"]))
    check("缓存已切换为新 generation", inspect["generation"] == 1, str(inspect["generation"]))
    check("胜方条目属于新 generation", inspect["entries"][0]["generation"] == 1)
    # 新规则下再用中文头请求：语言不参与键，直接命中同一份英文缓存
    follow = await c.get(f"{PROXY}/releases", headers=h_zh)
    check("切换后中文头请求命中英文缓存", follow.headers.get("x-cache") == "HIT" and "Release Notes" in follow.text,
          follow.headers.get("x-cache"))
    await c.post(f"{ORIGIN}/admin/delay/0")


async def test_origin_failure_keeps_cache(c: httpx.AsyncClient) -> None:
    print("[7] 回源失败报错，已确认缓存状态保持不变")
    await reset(c)
    h = {"Accept": JSON, "Accept-Language": ZH}
    # 用原始请求固定原文表示，拿到的就是 identity ETag
    good = await raw_get("/releases", h | IDENTITY_HDR)
    before = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]

    await c.post(f"{ORIGIN}/admin/fail/1")
    # 冷变体（en）回源 -> 502
    bad = await c.get(f"{PROXY}/releases", headers={"Accept": JSON, "Accept-Language": EN})
    check("回源失败返回 502", bad.status_code == 502, str(bad.status_code))
    check("带错误标记头", bad.headers.get("x-cache") == "ORIGIN_ERROR")

    after = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("缓存条目数不变", after["entry_count"] == before["entry_count"] == 1)
    check("generation 不变", after["generation"] == before["generation"] == 0)

    # 已缓存且新鲜的变体仍可正常命中（httpx 自动解压，正文与原文一致）
    hit = await c.get(f"{PROXY}/releases", headers=h)
    check("已确认缓存仍可命中", hit.headers.get("x-cache") == "HIT" and hit.content == good.content)

    # 过期复验时回源失败：保留旧正文缓存不动
    await c.post(f"{PROXY}/_cache/age", json={"seconds": 31})
    await c.post(f"{ORIGIN}/admin/fail/1")
    bad2 = await c.get(f"{PROXY}/releases", headers=h)
    check("复验失败也返回 502", bad2.status_code == 502)
    after2 = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("失败后旧条目仍在",
          after2["entry_count"] == 1 and after2["entries"][0]["etag_identity"] == good.headers["etag"])


async def test_gzip_negotiation_and_dual_etags(c: httpx.AsyncClient) -> None:
    print("[8] Accept-Encoding 协商：gzip/原文两种表示，各自 ETag 与字节对应")
    await reset(c)
    base = {"Accept": JSON, "Accept-Language": ZH}

    plain = await raw_get("/releases", base | IDENTITY_HDR)
    check("原文请求无 Content-Encoding", "content-encoding" not in plain.headers)
    check("原文 ETag 与源站 ETag 形态一致", plain.headers["etag"].startswith('"'), plain.headers["etag"])
    etag_id = plain.headers["etag"]

    gz = await raw_get("/releases", base | GZIP_HDR)
    check("gzip 请求带 Content-Encoding: gzip", gz.headers.get("content-encoding") == "gzip")
    check("gzip ETag 与原文 ETag 不同", gz.headers["etag"] != etag_id, gz.headers["etag"])
    check("gzip ETag 带 gzip 前缀", gz.headers["etag"].startswith('"gzip-'), gz.headers["etag"])
    check("压缩字节可被 gzip 还原且与原文一致", gzip.decompress(gz.content) == plain.content)
    check("压缩后体积更小", len(gz.content) < len(plain.content),
          f"{len(gz.content)} vs {len(plain.content)}")

    # 完全不带 Accept-Encoding 的朴素客户端拿原文
    naive = await raw_get("/releases", base)
    check("无 Accept-Encoding 时回原文", "content-encoding" not in naive.headers)
    check("朴素客户端 ETag 为原文 ETag", naive.headers["etag"] == etag_id)

    # identity;q=0 且不接受 gzip -> 406
    bad = await raw_get("/releases", base | {"Accept-Encoding": "identity;q=0"})
    check("无可接受编码时 406", bad.status_code == 406, str(bad.status_code))
    check("406 也带 Vary", "accept-encoding" in bad.headers.get("vary", "").lower())

    # 只声明 identity 时不能返回 gzip
    only_id = await raw_get("/releases", base | {"Accept-Encoding": "identity"})
    check("仅 identity 协商回原文", "content-encoding" not in only_id.headers)
    # * 通配可以选中 gzip
    wildcard = await raw_get("/releases", base | {"Accept-Encoding": "*"})
    check("Accept-Encoding: * 可返回 gzip", wildcard.headers.get("content-encoding") == "gzip")

    inspect = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    entry = inspect["entries"][0]
    check("inspect 同时展示两个 ETag",
          entry["etag_identity"] == etag_id and entry["etag_gzip"] == gz.headers["etag"])
    check("gzip 已就绪且只压了一次", inspect["stats"]["gzip_compressions"] == 1,
          str(inspect["stats"]))
    check("Vary 头包含编码及源站协商项",
          gz.headers["vary"] == "Accept, Accept-Language, Accept-Encoding", gz.headers["vary"])

    # 代理不向源站转发 Accept-Encoding，以上所有变体共享一次回源
    os_st = await origin_state(c)
    check("源站只收到 1 次原文回源", os_st["counters"]["requests"] == 1, str(os_st["counters"]))


async def test_conditional_requests_by_selected_etag(c: httpx.AsyncClient) -> None:
    print("[9] If-None-Match 只按本次所选表示的 ETag 判定 304")
    await reset(c)
    base = {"Accept": JSON, "Accept-Language": EN}
    plain = await raw_get("/releases", base | IDENTITY_HDR)
    gz = await raw_get("/releases", base | GZIP_HDR)
    etag_id, etag_gz = plain.headers["etag"], gz.headers["etag"]

    # 原文表示带自己的 ETag -> 304
    r = await raw_get("/releases", base | IDENTITY_HDR | {"If-None-Match": etag_id})
    check("原文 ETag 复验原文 -> 304", r.status_code == 304, str(r.status_code))
    check("304 无正文", r.content == b"")
    check("304 回原文 ETag", r.headers["etag"] == etag_id)
    check("304 无 Content-Encoding", "content-encoding" not in r.headers)

    # gzip 表示带压缩字节 ETag -> 304，且带 Content-Encoding
    r = await raw_get("/releases", base | GZIP_HDR | {"If-None-Match": etag_gz})
    check("gzip ETag 复验 gzip -> 304", r.status_code == 304, str(r.status_code))
    check("gzip 304 带 Content-Encoding", r.headers.get("content-encoding") == "gzip")
    check("gzip 304 回 gzip ETag", r.headers["etag"] == etag_gz)

    # 交叉使用：gzip 请求带原文 ETag -> 不匹配，回完整 200 压缩正文
    r = await raw_get("/releases", base | GZIP_HDR | {"If-None-Match": etag_id})
    check("gzip 请求带原文 ETag -> 200", r.status_code == 200, str(r.status_code))
    check("200 为 gzip 正文", r.headers.get("content-encoding") == "gzip")
    check("200 正文可解压", gzip.decompress(r.content) == plain.content)

    # 原文请求带 gzip ETag -> 同样不匹配
    r = await raw_get("/releases", base | IDENTITY_HDR | {"If-None-Match": etag_gz})
    check("原文请求带 gzip ETag -> 200", r.status_code == 200, str(r.status_code))
    check("200 为原文", "content-encoding" not in r.headers and r.content == plain.content)

    # If-None-Match: * 对任一已选表示都成立
    r = await raw_get("/releases", base | GZIP_HDR | {"If-None-Match": "*"})
    check("星号条件请求 -> 304", r.status_code == 304, str(r.status_code))

    stats = await proxy_stats(c)
    check("统计到 3 次客户端 304", stats["not_modified"] == 3, str(stats))


async def test_single_gzip_per_body_concurrent(c: httpx.AsyncClient) -> None:
    print("[10] 同一份正文只压一次：并发 gzip 共用，原文命中不等压缩")
    await reset(c)
    await c.post(f"{PROXY}/_test/compress_delay", json={"ms": 800})
    base = {"Accept": MD, "Accept-Language": ZH}
    # 先以原文预热缓存（此时不应触发压缩）
    warm = await raw_get("/releases", base | IDENTITY_HDR)
    check("预热为原文", "content-encoding" not in warm.headers)

    async def gget() -> RawResponse:
        return await raw_get("/releases", base | GZIP_HDR)

    # 三个 gzip 请求并发：只有一个真正压缩，其余等待共用
    t0 = time.monotonic()
    g1, g2, g3 = await asyncio.gather(gget(), gget(), gget())
    elapsed = time.monotonic() - t0
    check("三个并发 gzip 都成功", all(r.status_code == 200 for r in (g1, g2, g3)))
    check("三份压缩字节完全一致", g1.content == g2.content == g3.content)
    check("三份 gzip ETag 完全一致",
          g1.headers["etag"] == g2.headers["etag"] == g3.headers["etag"])
    check("压缩只发生一次", (await proxy_stats(c))["gzip_compressions"] == 1)
    check("并发共用而非各压一次（耗时 < 1.6s）", elapsed < 1.6, f"{elapsed:.2f}s")

    # 压缩在途时，原文请求立即返回，不等待压缩
    await c.post(f"{PROXY}/_test/compress_delay", json={"ms": 1000})
    # 新变体（en）制造一个尚未压缩的 Entry
    base_en = {"Accept": MD, "Accept-Language": EN}
    await raw_get("/releases", base_en | IDENTITY_HDR)
    gz_task = asyncio.create_task(raw_get("/releases", base_en | GZIP_HDR))
    await asyncio.sleep(0.25)  # 确认压缩已在途
    t0 = time.monotonic()
    plain = await raw_get("/releases", base_en | IDENTITY_HDR)
    plain_elapsed = time.monotonic() - t0
    gz_en = await gz_task
    check("压缩在途时原文请求不等待（< 0.3s）", plain_elapsed < 0.3, f"{plain_elapsed:.2f}s")
    check("原文请求直接 HIT", plain.headers.get("x-cache") == "HIT")
    check("两份正文各自压缩一次（总计 2 次）", (await proxy_stats(c))["gzip_compressions"] == 2)
    check("两份正文的 gzip 字节不同", gz_en.content != g1.content)


async def test_revalidation_304_keeps_gzip(c: httpx.AsyncClient) -> None:
    print("[11] 回源 304 沿用同一份正文与已有 gzip，ETag/压缩字节不变")
    await reset(c)
    base = {"Accept": JSON, "Accept-Language": ZH}
    gz1 = await raw_get("/releases", base | GZIP_HDR)
    etag_gz_before = gz1.headers["etag"]
    etag_id_before = (await raw_get("/releases", base | IDENTITY_HDR)).headers["etag"]

    await c.post(f"{PROXY}/_cache/age", json={"seconds": 31})
    # gzip 请求触发回源复验（转发的是原文 ETag），源站回 304
    gz2 = await raw_get("/releases", base | GZIP_HDR)
    check("复验状态为 304 沿用", gz2.headers.get("x-cache") == "REVALIDATED_304", gz2.headers.get("x-cache"))
    check("gzip 字节与复验前一致", gz2.content == gz1.content)
    check("gzip ETag 不变", gz2.headers["etag"] == etag_gz_before)
    check("原文 ETag 不变",
          (await raw_get("/releases", base | IDENTITY_HDR)).headers["etag"] == etag_id_before)
    check("304 不产生新压缩", (await proxy_stats(c))["gzip_compressions"] == 1)
    st = await origin_state(c)
    check("源站确实回了 304", st["counters"]["status304"] >= 1, str(st["counters"]))


async def test_body_replacement_inflight_compression(c: httpx.AsyncClient) -> None:
    print("[12] 回源 200 换正文：在途压缩只服务引用旧正文的请求，不写入新正文缓存")
    await reset(c)
    await c.post(f"{PROXY}/_test/compress_delay", json={"ms": 1000})
    base = {"Accept": JSON, "Accept-Language": EN}
    old_plain = await raw_get("/releases", base | IDENTITY_HDR)
    old_etag_id = old_plain.headers["etag"]

    # 启动旧正文的 gzip 压缩（在途）
    gz_old_task = asyncio.create_task(raw_get("/releases", base | GZIP_HDR))
    await asyncio.sleep(0.25)
    inspect = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("旧正文压缩确认在途", inspect["entries"][0]["gzip_inflight"] is True,
          str(inspect["entries"][0]))

    # 压缩在途期间让源站正文换代并令缓存过期，触发 200 换正文
    await c.post(f"{ORIGIN}/admin/rev")
    await c.post(f"{PROXY}/_cache/age", json={"seconds": 31})
    new_plain = await raw_get("/releases", base | IDENTITY_HDR)
    check("换代回源拿到 200 新正文", new_plain.headers.get("x-cache") == "REVALIDATED_200",
          new_plain.headers.get("x-cache"))
    check("新正文字节与旧正文不同", new_plain.content != old_plain.content)
    check("原文 ETag 随正文换代", new_plain.headers["etag"] != old_etag_id)

    # 等旧正文的在途压缩结束：它答复的是引用旧 Entry 的请求
    gz_old = await gz_old_task
    check("在途压缩仍正常答复其原请求", gz_old.status_code == 200)
    check("在途压缩的是旧正文字节", gzip.decompress(gz_old.content) == old_plain.content)

    # 新正文的 gzip 必须重新压缩，绝不能复用/写入旧压缩结果
    gz_new = await raw_get("/releases", base | GZIP_HDR)
    check("新正文 gzip 解压后为新正文", gzip.decompress(gz_new.content) == new_plain.content)
    check("新旧 gzip 字节不同", gz_new.content != gz_old.content)
    check("新 gzip ETag 与旧 gzip ETag 不同", gz_new.headers["etag"] != gz_old.headers["etag"])

    inspect = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("缓存只剩换代后的 1 条", inspect["entry_count"] == 1, str(inspect["entry_count"]))
    check("缓存条目持有新 ETag", inspect["entries"][0]["etag_identity"] == new_plain.headers["etag"])
    check("缓存中的 gzip 属于新正文", inspect["entries"][0]["etag_gzip"] == gz_new.headers["etag"])
    check("两份正文各压一次（总计 2 次）", inspect["stats"]["gzip_compressions"] == 2,
          str(inspect["stats"]))


async def test_vary_switch_inflight_compression(c: httpx.AsyncClient) -> None:
    print("[13] Vary 切换时在途压缩只给原请求，新规则缓存不带旧压缩")
    await reset(c)
    await c.post(f"{PROXY}/_test/compress_delay", json={"ms": 1000})
    h = {"Accept": JSON, "Accept-Language": ZH}
    old_plain = await raw_get("/releases", h | IDENTITY_HDR)

    gz_task = asyncio.create_task(raw_get("/releases", h | GZIP_HDR))
    await asyncio.sleep(0.25)
    # 在途压缩期间切换源站规则并令缓存过期
    await c.post(f"{ORIGIN}/admin/vary/accept")
    await c.post(f"{PROXY}/_cache/age", json={"seconds": 31})
    switched = await raw_get("/releases", h | IDENTITY_HDR)
    check("规则切换回源标记 MISS_NEW_RULE", switched.headers.get("x-cache") == "MISS_NEW_RULE",
          switched.headers.get("x-cache"))

    gz_old = await gz_task
    check("旧正文的在途压缩仍答复原请求", gz_old.status_code == 200)
    check("在途压缩对应旧正文", gzip.decompress(gz_old.content) == old_plain.content)

    inspect = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("新规则缓存只有 1 条且无旧压缩",
          inspect["entry_count"] == 1 and inspect["entries"][0]["gzip_ready"] is False,
          str(inspect["entries"]))
    gz_new = await raw_get("/releases", h | GZIP_HDR)
    check("新规则 gzip 重新压缩得到新字节", gzip.decompress(gz_new.content) == switched.content)
    # 统计挂在 PathState 上（条目清空不重置），重新取快照确认总压缩次数
    inspect2 = (await c.get(f"{PROXY}/_cache/inspect")).json()["paths"]["/releases"]
    check("压缩总次数为 2（旧、新各一）", inspect2["stats"]["gzip_compressions"] == 2,
          str(inspect2["stats"]))


async def test_cache_clear_inflight_compression(c: httpx.AsyncClient) -> None:
    print("[14] 清缓存后在途压缩不写回新缓存，重新请求按冷缓存处理")
    await reset(c)
    await c.post(f"{PROXY}/_test/compress_delay", json={"ms": 1000})
    h = {"Accept": MD, "Accept-Language": ZH}
    old_plain = await raw_get("/releases", h | IDENTITY_HDR)

    gz_task = asyncio.create_task(raw_get("/releases", h | GZIP_HDR))
    await asyncio.sleep(0.25)
    await c.post(f"{PROXY}/_cache/clear")
    gz_old = await gz_task
    check("清缓存不影响在途请求拿到完整答复", gz_old.status_code == 200)
    check("在途答复仍是旧正文的压缩", gzip.decompress(gz_old.content) == old_plain.content)

    inspect = (await c.get(f"{PROXY}/_cache/inspect")).json()
    check("缓存确为空", inspect["paths"] == {}, str(inspect["paths"]))
    new_plain = await raw_get("/releases", h | IDENTITY_HDR)
    # 清缓存后 Vary 也被重置，首个回源会重新确认规则，因此标记为 MISS 或
    # MISS_NEW_RULE 都符合预期；关键是确实重新回源而非命中
    check("清缓存后重新回源（非 HIT）",
          new_plain.headers.get("x-cache") in ("MISS", "MISS_NEW_RULE"),
          new_plain.headers.get("x-cache"))
    gz_new = await raw_get("/releases", h | GZIP_HDR)
    check("新缓存的 gzip 与旧在途字节同源但属于新条目",
          gzip.decompress(gz_new.content) == new_plain.content)


async def main() -> int:
    async with httpx.AsyncClient(timeout=30) as c:
        for base in (PROXY, ORIGIN):
            try:
                r = await c.get(f"{base}/health")
                r.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                print(f"服务不可达 {base}: {exc}")
                return 2
        await test_content_negotiation_and_hit(c)
        await test_revalidation_304(c)
        await test_vary_switch_invalidation(c)
        await test_parallel_languages(c)
        await test_concurrent_switch_inflight(c)
        await test_concurrent_switch_during_revalidation(c)
        await test_origin_failure_keeps_cache(c)
        await test_gzip_negotiation_and_dual_etags(c)
        await test_conditional_requests_by_selected_etag(c)
        await test_single_gzip_per_body_concurrent(c)
        await test_revalidation_304_keeps_gzip(c)
        await test_body_replacement_inflight_compression(c)
        await test_vary_switch_inflight_compression(c)
        await test_cache_clear_inflight_compression(c)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
