"""app.encoding 的纯单元测试：Accept-Encoding 解析、确定性压缩、ETag、条件比较。

运行：python -m app.tests.test_encoding
"""
from __future__ import annotations

import gzip

from app.encoding import (
    GZIP,
    IDENTITY,
    NotAcceptableEncoding,
    etag_matches,
    gzip_body,
    gzip_etag,
    parse_accept_encoding,
)

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


def test_parse_accept_encoding() -> None:
    print("[U1] Accept-Encoding 解析")
    cases = [
        (None, IDENTITY),
        ("", IDENTITY),
        ("gzip", GZIP),
        ("gzip, identity", GZIP),
        ("identity", IDENTITY),
        ("identity, gzip;q=0", IDENTITY),
        ("gzip;q=0, identity", IDENTITY),
        ("gzip;q=0.5, identity;q=0.9", GZIP),
        ("*", GZIP),  # 通配覆盖 gzip
        ("*;q=0", None),  # 两种编码都不可接受 -> 406
        ("identity;q=0", None),
        ("gzip;q=0", IDENTITY),  # gzip 被禁，identity 默认可接受
        ("deflate, br", IDENTITY),  # 不支持的编码不影响 identity 默认
        ("GZIP", GZIP),  # 大小写不敏感
        (" gzip ; q=1.0 ", GZIP),
    ]
    for header, expected in cases:
        if expected is None:
            raised = False
            try:
                parse_accept_encoding(header)
            except NotAcceptableEncoding:
                raised = True
            check(f"{header!r} -> 406", raised)
        else:
            got = parse_accept_encoding(header)
            check(f"{header!r} -> {expected}", got == expected, got)


def test_deterministic_gzip_and_etag() -> None:
    print("[U2] 确定性 gzip 与双 ETag")
    body = "发布说明 release notes\n" * 50
    raw = body.encode("utf-8")
    g1, g2 = gzip_body(raw), gzip_body(raw)
    check("同一原文两次压缩字节一致", g1 == g2)
    check("与标准库默认压缩解压结果一致", gzip.decompress(g1) == raw)
    check("压缩后更小", len(g1) < len(raw))
    e1, e2 = gzip_etag(g1), gzip_etag(g2)
    check("gzip ETag 稳定", e1 == e2)
    check("gzip ETag 带前缀且为强标记", e1.startswith('"gzip-') and e1.endswith('"'))
    other = gzip_etag(gzip_body(raw + b"x"))
    check("不同正文 ETag 不同", other != e1)
    # mtime 固定为 0：gzip 头第 4-7 字节（MTIME）应为全 0
    check("gzip 头 MTIME 字段为 0", g1[4:8] == b"\x00\x00\x00\x00", g1[4:8].hex())


def test_if_none_match() -> None:
    print("[U3] If-None-Match 只按给定 ETag 判定")
    etag_id = '"abc123"'
    etag_gz = '"gzip-deadbeef"'
    check("精确匹配", etag_matches(etag_id, etag_id))
    check("不同表示 ETag 不匹配", not etag_matches(etag_id, etag_gz))
    check("无头不匹配", not etag_matches(None, etag_id))
    check("空头不匹配", not etag_matches("", etag_id))
    check("星号匹配任意表示", etag_matches("*", etag_gz))
    check("弱标记可弱比较命中", etag_matches(f'W/{etag_id}', etag_id))
    check("多值列表命中其一", etag_matches(f'"x", {etag_gz}', etag_gz))
    check("多值列表均不命中", not etag_matches('"x", "y"', etag_id))


def main() -> int:
    test_parse_accept_encoding()
    test_deterministic_gzip_and_etag()
    test_if_none_match()
    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
