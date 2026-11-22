"""内容编码协商（gzip）与条件请求工具。

职责：
- 解析客户端 ``Accept-Encoding``，在 ``gzip`` 与原文（identity）之间选择；
- 生成与实际响应字节绑定的 ETag：原文沿用源站 ETag，gzip 使用压缩字节的
  强校验 ETag（同一份原文只压一次，压缩字节固定 -> ETag 固定）；
- 解析客户端 ``If-None-Match``，只按本次所选表示的 ETag 判定 304。
"""
from __future__ import annotations

import gzip
import hashlib

GZIP = "gzip"
IDENTITY = "identity"


def parse_accept_encoding(header: str | None) -> str:
    """返回本次响应使用的内容编码：``gzip`` 或 ``identity``。

    规则：
    - 不带 ``Accept-Encoding`` 的请求视为只接受原文（与 nginx 默认行为一致，
      避免向朴素客户端硬塞压缩字节）；
    - 按 q 值挑选，q=0 表示不可接受；``*`` 通配未列出的编码；
    - gzip 与原文同时可接受时优先 gzip（压缩 JSON/Markdown 收益明显）；
    - 原文被显式标记为不可接受（identity;q=0）且没有 gzip 时抛出
      ``NotAcceptableEncoding``，由调用方回 406。
    """
    if header is None or not header.strip():
        return IDENTITY

    q_gzip: float | None = None
    q_identity: float | None = None
    q_wildcard: float | None = None
    for raw in header.split(","):
        parts = [p.strip() for p in raw.split(";") if p.strip()]
        if not parts:
            continue
        coding = parts[0].lower()
        q = 1.0
        for p in parts[1:]:
            if p.lower().startswith("q="):
                try:
                    q = max(0.0, min(1.0, float(p[2:])))
                except ValueError:
                    q = 0.0
        if coding == "gzip":
            q_gzip = q
        elif coding == "identity":
            q_identity = q
        elif coding == "*":
            q_wildcard = q

    # 字段存在时，只有显式列出（或被 * 覆盖）的编码才可接受；
    # identity 例外：未被显式 q=0、也没有 *;q=0 时默认可接受
    gzip_ok = (q_gzip is not None and q_gzip > 0) or (
        q_wildcard is not None and q_wildcard > 0
    )
    if q_identity is not None:
        identity_ok = q_identity > 0
    elif q_wildcard is not None:
        identity_ok = q_wildcard > 0
    else:
        identity_ok = True
    if gzip_ok:
        return GZIP
    if identity_ok:
        return IDENTITY
    raise NotAcceptableEncoding(header)


class NotAcceptableEncoding(Exception):
    """客户端不接受任何可提供的内容编码。"""

    def __init__(self, header: str):
        super().__init__(f"no acceptable content-encoding in {header!r}")
        self.header = header


def gzip_body(body: bytes) -> bytes:
    """确定性 gzip 压缩：固定 mtime，保证同一份原文产出同一份压缩字节。"""
    return gzip.compress(body, compresslevel=6, mtime=0)


def gzip_etag(body: bytes) -> str:
    """gzip 表示的 ETag：对实际压缩字节取强校验值，与原文 ETag 区分。"""
    digest = hashlib.sha1(body).hexdigest()[:16]
    return f'"gzip-{digest}"'


def if_none_match_list(header: str | None) -> list[str]:
    """解析 If-None-Match 为 ETag 标记列表（保留 W/ 弱前缀，比较时再剥离）。"""
    if not header:
        return []
    return [part.strip() for part in header.split(",") if part.strip()]


def _weak_compare(token: str) -> str:
    # 弱比较：剥离 W/ 前缀后按 opaque-quoted-string 比较
    if token.startswith(("W/", "w/")):
        token = token[2:].strip()
    return token


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """只按调用方给定的（本次所选表示的）ETag 判定条件请求是否命中。"""
    tokens = if_none_match_list(if_none_match)
    if not tokens:
        return False
    if "*" in tokens:
        return True
    target = _weak_compare(etag)
    return any(_weak_compare(t) == target for t in tokens)
