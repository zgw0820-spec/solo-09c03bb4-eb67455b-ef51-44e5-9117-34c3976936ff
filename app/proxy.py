"""FastAPI 缓存代理（含 gzip 内容编码协商）。

缓存语义：
- 每个路径维护一份“已确认”的 Vary 规则；查找缓存时按当前 Vary 选取
  头部（Accept / Accept-Language）计算变体键。
- max-age 未过期直接命中；过期后带源站 ETag 回源复验：
  * 304 -> 沿用发起复验时选中的旧正文，仅刷新元数据；
  * 200 -> 用新正文替换（新 Entry，旧 Entry 及其在途压缩随之作废）。
- 回源响应里的 Vary 与已确认规则不一致时，旧规则下的所有条目整体失效
  （generation 递增），本次正文按新规则缓存。
- 并发控制：每个协商身份（Accept + Accept-Language）配一个条件变量，
  同身份请求严格 single-flight（后到者等待在途回源后直接复用结果），
  不同身份互不阻塞、可并行回源。
- 在途切换识别：源站用 X-Rule-Epoch 标识规则世代，代理回源前探测基准
  世代；回源期间规则被别的请求切换时，在途答复只返回给其原始请求
  （INFLIGHT_RULE_SWITCHED / STALE_INFLIGHT_304），不写入新缓存。
- 回源失败返回 502，不改动任何已确认的缓存状态。

gzip 语义：
- 按客户端 Accept-Encoding 在 gzip 与原文之间选择；代理不转发
  Accept-Encoding，回源与缓存均保存原文（identity）字节。
- 对客户端的 Vary = 源站协商项（Accept / Accept-Language）+ Accept-Encoding。
- 两种表示各有与各自字节对应的 ETag：原文沿用源站 ETag；gzip 用压缩字节
  算出的强 ETag。客户端 If-None-Match 只按本次所选表示的 ETag 判定 304。
- 同一份缓存正文至多一份 gzip：Entry 上配独立条件变量，首个 gzip 请求发起
  压缩，并发 gzip 请求共用结果；原文请求完全不参与、不等待压缩。
- 回源 304 沿用同一 Entry（同一正文），其已有 gzip 继续有效。
- Vary 切换、清缓存或回源 200 换掉正文后，旧 Entry 上的在途压缩只能服务
  引用该旧 Entry 的请求，结果写在旧 Entry 上，绝不会进入新正文的缓存。
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Request, Response

from app.encoding import (
    GZIP,
    NotAcceptableEncoding,
    etag_matches,
    gzip_body,
    gzip_etag,
    parse_accept_encoding,
)

ORIGIN_BASE = os.environ.get("ORIGIN_BASE", "http://origin:8080")
# 测试辅助：给 gzip 压缩人为加延迟，便于复现并发压缩/正文换代竞态。
# 初始值取自环境变量，运行期可用 POST /_test/compress_delay 调整。
COMPRESS_DELAY_MS = int(os.environ.get("COMPRESS_DELAY_MS", "0"))
REPLAY_HEADERS = ("content-type", "content-language", "cache-control")
# 对客户端声明 Vary 时使用的标准头名
_CANONICAL = {
    "accept": "Accept",
    "accept-language": "Accept-Language",
    "accept-encoding": "Accept-Encoding",
}

app = FastAPI(title="cache-proxy", version="1.1")


@dataclass
class Entry:
    body: bytes
    headers: dict[str, str]
    etag: str  # 原文（identity）表示的 ETag，沿用源站 ETag
    vary: tuple[str, ...]
    variant_key: tuple[str, ...]
    identity: tuple[str, ...]  # 产生该条目的请求协商身份（accept, accept-language）
    stored_at: float
    max_age: int
    fetch_generation: int
    # 同一份正文的 gzip 表示（惰性生成，全文至多一份）
    gzip_body: bytes | None = None
    gzip_etag_value: str | None = None
    gzip_inflight: bool = False
    gzip_cond: asyncio.Condition = field(default_factory=asyncio.Condition)


@dataclass
class IdentitySync:
    """单个协商身份的 single-flight 同步原语：同一时刻只允许一个回源，
    后到同身份请求等待在途回源完成后重新检查缓存。"""
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    in_flight: bool = False


@dataclass
class PathState:
    vary: tuple[str, ...] | None = None
    # 已确认的源站规则世代号（None=尚未确认），随源站 X-Rule-Epoch 更新
    confirmed_epoch: int | None = None
    entries: dict[tuple[str, ...], Entry] = field(default_factory=dict)
    generation: int = 0
    path_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 键为协商身份（与 generation 无关），保证同身份请求始终共享同一同步原语
    identity_sync: dict[tuple[str, ...], IdentitySync] = field(default_factory=dict)
    stats: dict = field(
        default_factory=lambda: {
            "hits": 0,
            "revalidated": 0,
            "fetches": 0,
            "vary_switches": 0,
            "origin_errors": 0,
            "gzip_compressions": 0,
            "not_modified": 0,
            "not_acceptable": 0,
        }
    )


# path -> PathState
_cache: dict[str, PathState] = {}
_cache_guard = asyncio.Lock()


async def _get_path_state(path: str) -> PathState:
    async with _cache_guard:
        st = _cache.get(path)
        if st is None:
            st = PathState()
            _cache[path] = st
        return st


def _parse_vary(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip().lower() for part in value.split(",") if part.strip())


def _parse_max_age(value: str | None, default: int = 0) -> int:
    if not value:
        return default
    for part in value.split(","):
        part = part.strip().lower()
        if part.startswith("max-age="):
            try:
                return max(0, int(part.split("=", 1)[1]))
            except ValueError:
                return default
    return default


def _variant_key(vary: tuple[str, ...], headers) -> tuple[str, ...]:
    return tuple(headers.get(h.lower(), "") for h in vary)


def _public_vary(origin_vary: tuple[str, ...]) -> str:
    """对客户端的 Vary：源站协商项 + Accept-Encoding，去重并使用标准头名。"""
    names: list[str] = []
    for h in (*origin_vary, "accept-encoding"):
        canon = _CANONICAL.get(h, h)
        if canon not in names:
            names.append(canon)
    return ", ".join(names)


def _base_headers(entry: Entry) -> dict[str, str]:
    headers = {k: v for k, v in entry.headers.items() if k in REPLAY_HEADERS}
    headers["vary"] = _public_vary(entry.vary)
    headers["x-cache-generation"] = str(entry.fetch_generation)
    return headers


def _build_response(entry: Entry, content: bytes, encoding: str,
                    etag_value: str, cache_status: str) -> Response:
    headers = _base_headers(entry)
    headers["etag"] = etag_value
    headers["x-cache"] = cache_status
    if encoding == GZIP:
        headers["content-encoding"] = GZIP
    return Response(content=content, status_code=200, headers=headers)


def _build_not_modified(entry: Entry, encoding: str, etag_value: str,
                        cache_status: str) -> Response:
    """304 无正文，但带上本次所选表示的元数据（ETag / 编码 / Vary）。"""
    headers = _base_headers(entry)
    headers["etag"] = etag_value
    headers["x-cache"] = f"{cache_status}_NOT_MODIFIED"
    if encoding == GZIP:
        headers["content-encoding"] = GZIP
    return Response(status_code=304, headers=headers)


async def _ensure_gzip(st: PathState, entry: Entry) -> None:
    """保证 entry 的 gzip 表示就绪。每个 Entry（= 每份正文）只压缩一次；
    并发 gzip 请求在该 Entry 自己的条件变量上共用结果，原文请求不经过这里。"""
    async with entry.gzip_cond:
        if entry.gzip_body is not None:
            return
        if entry.gzip_inflight:
            # 同一正文的压缩已在途：等待其完成后直接复用
            await entry.gzip_cond.wait_for(
                lambda: entry.gzip_body is not None or not entry.gzip_inflight
            )
            if entry.gzip_body is not None:
                return
            # 在途压缩异常退出：由当前请求补做一次
        entry.gzip_inflight = True

    try:
        if COMPRESS_DELAY_MS:
            await asyncio.sleep(COMPRESS_DELAY_MS / 1000.0)
        # 压缩在线程池执行，不阻塞事件循环；确定性压缩（mtime=0）
        data = await asyncio.to_thread(gzip_body, entry.body)
        gz_etag = gzip_etag(data)
    except BaseException:
        async with entry.gzip_cond:
            entry.gzip_inflight = False
            entry.gzip_cond.notify_all()
        raise

    # 写回的是“本 Entry”：旧正文上的在途压缩结果只会落在旧 Entry 上，
    # 不可能写进 Vary 切换/换正文后新 Entry 的缓存
    async with entry.gzip_cond:
        if entry.gzip_body is None:
            entry.gzip_body = data
            entry.gzip_etag_value = gz_etag
            st.stats["gzip_compressions"] += 1
        entry.gzip_inflight = False
        entry.gzip_cond.notify_all()


async def _serve(st: PathState, entry: Entry, request: Request,
                 cache_status: str) -> Response:
    """缓存解析完成后的表示选择、按需压缩、客户端条件请求判定与回包构造。
    在回源 single-flight 之外执行：原文命中不会被任何在途压缩阻塞。"""
    try:
        encoding = parse_accept_encoding(request.headers.get("accept-encoding"))
    except NotAcceptableEncoding as exc:
        st.stats["not_acceptable"] += 1
        # 冷缓存下尚无已确认 Vary：用源站已知协商项（Accept / Accept-Language）
        # 兜底，保证 406 也正确声明 Vary
        origin_vary = st.vary if st.vary is not None else ("accept", "accept-language")
        return Response(
            f"{exc}\n".encode(),
            status_code=406,
            headers={"vary": _public_vary(origin_vary), "x-cache": "NOT_ACCEPTABLE"},
        )
    if encoding == GZIP:
        await _ensure_gzip(st, entry)
        content = entry.gzip_body
        etag_value = entry.gzip_etag_value
    else:
        content = entry.body
        etag_value = entry.etag

    # 只按本次所选表示的 ETag 判定客户端条件请求
    if etag_matches(request.headers.get("if-none-match"), etag_value):
        st.stats["not_modified"] += 1
        return _build_not_modified(entry, encoding, etag_value, cache_status)
    return _build_response(entry, content, encoding, etag_value, cache_status)


async def _fetch_from_origin(request: Request, path: str, etag: str | None) -> httpx.Response:
    # 只转发协商与条件请求头部；不转发 Accept-Encoding：
    # 代理始终缓存源站原文，gzip 由代理按客户端协商自行生成
    fwd = {}
    for name in ("accept", "accept-language"):
        value = request.headers.get(name)
        if value is not None:
            fwd[name] = value
    if etag:
        fwd["if-none-match"] = etag
    url = f"{ORIGIN_BASE}{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.get(url, headers=fwd)


async def _probe_origin_epoch() -> int | None:
    """回源前探测源站当前规则世代，作为本次回源的基准世代。失败不阻断主流程。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{ORIGIN_BASE}/rule_epoch")
            return int(r.json()["rule_epoch"])
    except (httpx.HTTPError, KeyError, ValueError):
        return None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "proxy"}


@app.get("/_cache/inspect")
async def cache_inspect() -> dict:
    out = {}
    for path, st in _cache.items():
        out[path] = {
            "confirmed_vary": list(st.vary) if st.vary else [],
            "generation": st.generation,
            "entry_count": len(st.entries),
            "stats": dict(st.stats),
            "entries": [
                {
                    "variant_key": list(e.variant_key),
                    "etag_identity": e.etag,
                    "etag_gzip": e.gzip_etag_value,
                    "gzip_ready": e.gzip_body is not None,
                    "gzip_inflight": e.gzip_inflight,
                    "body_bytes": len(e.body),
                    "gzip_bytes": len(e.gzip_body) if e.gzip_body is not None else 0,
                    "vary": list(e.vary),
                    "age_seconds": round(time.time() - e.stored_at, 2),
                    "max_age": e.max_age,
                    "fresh": time.time() - e.stored_at < e.max_age,
                    "generation": e.fetch_generation,
                }
                for e in st.entries.values()
            ],
        }
    return {"paths": out}


@app.post("/_cache/clear")
async def cache_clear() -> dict:
    _cache.clear()
    return {"ok": True}


@app.post("/_cache/age")
async def cache_age(request: Request) -> dict:
    """测试辅助：把所有条目的 stored_at 向前拨，模拟 max-age 到期。"""
    payload = await request.json()
    seconds = float(payload.get("seconds", 0))
    touched = 0
    for st in _cache.values():
        for entry in st.entries.values():
            entry.stored_at -= seconds
            touched += 1
    return {"ok": True, "entries_aged": touched}


@app.post("/_test/compress_delay")
async def set_compress_delay(request: Request) -> dict:
    """测试辅助：运行期设置 gzip 压缩前的人为延迟（毫秒）。"""
    global COMPRESS_DELAY_MS
    payload = await request.json()
    COMPRESS_DELAY_MS = max(0, int(payload.get("ms", 0)))
    return {"ok": True, "compress_delay_ms": COMPRESS_DELAY_MS}


def _select_entry(st: PathState, request: Request, identity_key: tuple[str, ...]) -> "Entry | None":
    """按当前 Vary 规则为请求选取可用缓存条目。"""
    key = _variant_key(st.vary or (), request.headers)

    def _usable(candidate: "Entry | None") -> "Entry | None":
        if candidate is None or candidate.fetch_generation != st.generation:
            return None
        # 冷启动期暂存在空键下的条目只能被同协商身份复用；
        # 正式条目按 Vary 规则入键，同键即同表示，允许跨身份共享
        # （例如 Vary 切到只按 Accept 后，不同语言共享一份正文）
        if candidate.variant_key == () and candidate.identity != identity_key:
            return None
        return candidate

    entry = _usable(st.entries.get(key))
    if entry is None and st.vary is None:
        entry = _usable(st.entries.get(()))
    return entry


def _commit_upstream(st: PathState, request: Request, identity_key: tuple[str, ...],
                     upstream: httpx.Response, entry: "Entry | None",
                     epoch_before: int | None, snapshot_generation: int
                     ) -> "tuple[Entry | None, str | None, Response | None]":
    """回源成功后，在路径锁内原子完成规则确认/切换/写入。

    返回 (Entry, 缓存阶段, None)；无法处理时返回 (None, None, 错误响应)。
    注意返回的 Entry 可能是不入缓存的临时条目（在途规则切换透传）。
    """
    # 注意：upstream 缺少 Vary 头时按空规则处理，不能回退到旧规则
    if "vary" in upstream.headers:
        origin_vary = _parse_vary(upstream.headers.get("vary"))
    else:
        origin_vary = ()
    origin_epoch = int(upstream.headers.get("x-rule-epoch", "0"))
    current_vary = st.vary
    vary_changed = current_vary is not None and set(origin_vary) != set(current_vary)
    first_confirmation = st.confirmed_epoch is None
    # 在途切换：回源发起前的源站世代与响应世代不同，且新世代已被别的请求提交，
    # 或代理 generation 在回源期间已被递增。顺序切换的发现者不在此列。
    epoch_mismatch = epoch_before is not None and epoch_before != origin_epoch
    inflight_switch = epoch_mismatch and (
        st.generation != snapshot_generation or (st.confirmed_epoch == origin_epoch)
    )

    if inflight_switch:
        # 规则已在本请求回源期间被别的请求切换：结果只给原请求，不入缓存
        if upstream.status_code == 304 and entry is not None:
            return entry, "STALE_INFLIGHT_304", None
        passthrough = Entry(
            body=upstream.content,
            headers={k.lower(): v for k, v in upstream.headers.items()},
            etag=upstream.headers.get("etag", ""),
            vary=origin_vary,
            variant_key=(),
            identity=identity_key,
            stored_at=time.time(),
            max_age=_parse_max_age(upstream.headers.get("cache-control")),
            fetch_generation=snapshot_generation,
        )
        return passthrough, "INFLIGHT_RULE_SWITCHED", None

    if vary_changed:
        # 旧规则条目整体失效（连同它们的 gzip 与在途压缩），确认新规则
        st.entries.clear()
        st.generation += 1
        st.vary = origin_vary
        st.confirmed_epoch = origin_epoch
        st.stats["vary_switches"] += 1
        new_key = _variant_key(origin_vary, request.headers)
    elif first_confirmation:
        # 首次确认 Vary：之前的冷条目用空键暂存，按真实规则重新入键
        st.vary = origin_vary
        st.confirmed_epoch = origin_epoch
        new_key = _variant_key(origin_vary, request.headers)
        if epoch_before is not None and epoch_before != origin_epoch:
            # 冷启动在途期间源站已切换过规则：同样记一次规则世代切换
            st.generation += 1
            st.stats["vary_switches"] += 1
        rekeyed = {}
        for old_entry in st.entries.values():
            real_key = _variant_key(origin_vary, old_entry.headers)
            old_entry.variant_key = real_key
            rekeyed[real_key] = old_entry
        st.entries = rekeyed
    else:
        # 同规则下，世代号可能因“切走又切回”而推进，保持同步
        st.confirmed_epoch = origin_epoch
        new_key = _variant_key(origin_vary, request.headers)

    if upstream.status_code == 304:
        if entry is None:
            st.stats["origin_errors"] += 1
            return None, None, Response(
                b"origin 304 without cached representation\n", status_code=502
            )
        # 沿用发起复验时选中的正文（同一个 Entry），仅刷新元数据；
        # 正文字节未变 -> 已生成的 gzip 与 gzip ETag 继续有效
        entry.stored_at = time.time()
        entry.max_age = _parse_max_age(upstream.headers.get("cache-control"), entry.max_age)
        if etag_new := upstream.headers.get("etag"):
            entry.etag = etag_new
            entry.headers["etag"] = etag_new
        entry.vary = origin_vary
        entry.fetch_generation = st.generation
        if vary_changed:
            # 规则切换由本次 304 发现并提交：旧正文按新规则重新入键
            entry.variant_key = new_key
            st.entries[new_key] = entry
            st.stats["fetches"] += 1
            return entry, "MISS_NEW_RULE", None
        st.stats["revalidated"] += 1
        return entry, "REVALIDATED_304", None

    # 200：写入（或替换）缓存。新正文 = 新 Entry，旧 Entry 上的在途压缩
    # 仍只写在旧 Entry 上，不会污染这份新缓存
    revalidation = current_vary is not None and entry is not None
    new_entry = Entry(
        body=upstream.content,
        headers={k.lower(): v for k, v in upstream.headers.items()},
        etag=upstream.headers.get("etag", ""),
        vary=origin_vary,
        variant_key=new_key,
        identity=identity_key,
        stored_at=time.time(),
        max_age=_parse_max_age(upstream.headers.get("cache-control")),
        fetch_generation=st.generation,
    )
    st.entries[new_key] = new_entry
    if revalidation and not vary_changed:
        st.stats["revalidated"] += 1
        phase = "REVALIDATED_200"
    else:
        st.stats["fetches"] += 1
        phase = "MISS_NEW_RULE" if vary_changed or first_confirmation else "MISS"
    return new_entry, phase, None


def _error_response(message: str) -> Response:
    return Response(message, status_code=502, headers={"X-Cache": "ORIGIN_ERROR"})


@app.api_route("/{full_path:path}", methods=["GET"])
async def proxy_get(full_path: str, request: Request) -> Response:
    path = request.url.path
    st = await _get_path_state(path)

    # 协商身份与 Vary 规则、generation 均无关：
    # - 不同身份（不同 Accept / 语言）使用不同条件变量，可并行回源；
    # - 同身份共享同一条件变量，严格 single-flight。
    identity_key = _variant_key(("accept", "accept-language"), request.headers)
    async with st.path_lock:
        sync = st.identity_sync.setdefault(identity_key, IdentitySync())

    # 基准规则世代在 single-flight 判定之外探测
    epoch_before = await _probe_origin_epoch()

    out_entry: Entry | None = None
    phase: str | None = None

    # 等待在途回源后命中的条目：压缩必须在 single-flight 锁外进行，
    # 否则同身份的原文请求会被别人的 gzip 压缩阻塞
    wait_hit: Entry | None = None
    async with sync.cond:
        # 已有同身份回源在途：等待其完成，然后重新检查缓存（可能已被填充）
        if sync.in_flight:
            await sync.cond.wait_for(lambda: not sync.in_flight)
            hit = _select_entry(st, request, identity_key)
            if hit is not None and time.time() - hit.stored_at < hit.max_age:
                wait_hit = hit
            # 前序回源失败或未产生可用条目：落到下方自行回源
        if wait_hit is None:
            sync.in_flight = True

    if wait_hit is not None:
        st.stats["hits"] += 1
        return await _serve(st, wait_hit, request, "HIT")

    snapshot_generation = st.generation
    try:
        entry = _select_entry(st, request, identity_key)
        if entry is not None and time.time() - entry.stored_at < entry.max_age:
            st.stats["hits"] += 1
            out_entry, phase = entry, "HIT"
        else:
            # 需要回源（冷请求或过期复验）
            etag = entry.etag if entry is not None else None
            try:
                upstream = await _fetch_from_origin(request, path, etag)
            except httpx.HTTPError as exc:
                st.stats["origin_errors"] += 1
                kind = "revalidation" if etag else "fetch"
                return _error_response(f"origin {kind} failed: {exc}\n".encode())

            if upstream.status_code not in (200, 304):
                st.stats["origin_errors"] += 1
                return _error_response(
                    f"origin returned status {upstream.status_code}\n".encode()
                )

            async with st.path_lock:
                out_entry, phase, err = _commit_upstream(
                    st, request, identity_key, upstream, entry,
                    epoch_before, snapshot_generation,
                )
            if err is not None:
                return err
    finally:
        # 回源 single-flight 到此释放：后续 gzip 压缩不在该锁内，
        # 同身份的原文请求命中时无需等待任何压缩
        async with sync.cond:
            sync.in_flight = False
            sync.cond.notify_all()

    return await _serve(st, out_entry, request, phase)
