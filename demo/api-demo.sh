#!/usr/bin/env bash
# 通过 API 逐步演示缓存代理的全部关键行为（含 gzip 内容编码协商）。
# 用法：
#   Docker:  docker compose up -d --build && bash demo/api-demo.sh
#   本地:    bash scripts/start-local.sh && bash demo/api-demo.sh
set -u
PROXY="${PROXY_BASE:-http://127.0.0.1:18000}"
ORIGIN="${ORIGIN_BASE:-http://127.0.0.1:18080}"
H_JSON='Accept: application/json'
H_MD='Accept: text/markdown'
H_GZIP='Accept-Encoding: gzip'
H_IDENTITY='Accept-Encoding: identity'

line() { printf '\n========== %s ==========\n' "$1"; }
code() { printf '$ %s\n' "$*"; }

line "0. 复位：源站规则=Vary: Accept, Accept-Language，清空代理缓存"
code curl -s -X POST "$ORIGIN/admin/reset"
curl -s -X POST "$ORIGIN/admin/reset"; echo
code curl -s -X POST "$PROXY/_cache/clear"
curl -s -X POST "$PROXY/_cache/clear"; echo

line "1. 按 Accept / Accept-Language 协商，同一变体第二次命中缓存"
code "curl -s $PROXY/releases -H '$H_JSON' -H 'Accept-Language: zh-CN' | head -4"
curl -s "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | head -4
code "curl -si ... （查看 X-Cache: HIT、Vary 含 Accept-Encoding）"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | grep -iE 'HTTP|x-cache|vary|etag'
code "curl -s $PROXY/releases -H '$H_MD' -H 'Accept-Language: zh-CN' | head -3"
curl -s "$PROXY/releases" -H "$H_MD" -H 'Accept-Language: zh-CN' | head -3

line "2. gzip 与原文两种表示：各自 ETag 与字节对应，Vary 含编码与源站协商项"
code "curl -si $PROXY/releases -H '$H_JSON' -H '$H_GZIP' （gzip 表示，只看响应头）"
curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" -H "$H_GZIP" | grep -iE 'HTTP|content-encoding|etag|vary|content-length'
code "curl -si $PROXY/releases -H '$H_JSON' -H '$H_IDENTITY' （原文表示）"
curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" -H "$H_IDENTITY" | grep -iE 'HTTP|content-encoding|etag|vary|content-length'
code "压缩收益对比（字节数）"
GZIP_BYTES=$(curl -s "$PROXY/releases" -H "$H_JSON" -H "$H_GZIP" | wc -c)
PLAIN_BYTES=$(curl -s "$PROXY/releases" -H "$H_JSON" -H "$H_IDENTITY" | wc -c)
echo "gzip=${GZIP_BYTES}B  identity=${PLAIN_BYTES}B"
code "gzip 字节可还原，且与原文一致"
curl -s "$PROXY/releases" -H "$H_JSON" -H "$H_GZIP" | gzip -d | head -2
code "不带 Accept-Encoding 的朴素客户端拿到原文"
curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" | grep -iE 'HTTP|content-encoding|etag' || true

line "3. 客户端条件复验：If-None-Match 只按本次所选表示的 ETag 判定"
ETAG_ID=$(curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" -H "$H_IDENTITY" | grep -i '^etag:' | tr -d '\r' | awk '{print $2}')
ETAG_GZ=$(curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" -H "$H_GZIP" | grep -i '^etag:' | tr -d '\r' | awk '{print $2}')
echo "identity ETag = $ETAG_ID ; gzip ETag = $ETAG_GZ"
code "原文 ETag 复验原文 -> 304"
curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" -H "$H_IDENTITY" -H "If-None-Match: $ETAG_ID" | grep -iE 'HTTP|etag|content-encoding'
code "gzip ETag 复验 gzip -> 304（带 Content-Encoding）"
curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" -H "$H_GZIP" -H "If-None-Match: $ETAG_GZ" | grep -iE 'HTTP|etag|content-encoding'
code "交叉使用：gzip 请求带原文 ETag -> 不匹配，回 200 完整压缩正文"
curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" -H "$H_GZIP" -H "If-None-Match: $ETAG_ID" | grep -iE 'HTTP|content-encoding|etag'

line "4. 查看代理缓存内部状态（双 ETag、gzip 就绪状态、压缩计数）"
code curl -s "$PROXY/_cache/inspect"
curl -s "$PROXY/_cache/inspect" | python3 -m json.tool 2>/dev/null || curl -s "$PROXY/_cache/inspect"

line "5. max-age 到期后带源站 ETag 复验，304 沿用旧正文与已有 gzip"
code "curl -s -X POST $PROXY/_cache/age -d '{\"seconds\":31}'  # 演示用：让条目立即过期"
curl -s -X POST "$PROXY/_cache/age" -H 'Content-Type: application/json' -d '{"seconds":31}'; echo
code "gzip 请求触发复验（代理回源转发的是原文 ETag）"
curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" -H "$H_GZIP" | grep -iE 'HTTP|x-cache|etag'
code "源站统计（可见 status304 计数；代理不转发 Accept-Encoding）"
curl -s "$ORIGIN/admin/state"; echo

line "6. 正文换代：源站 200 换新正文后，gzip 按新字节重新生成"
code curl -s -X POST "$ORIGIN/admin/rev"
curl -s -X POST "$ORIGIN/admin/rev"; echo
curl -s -X POST "$PROXY/_cache/age" -H 'Content-Type: application/json' -d '{"seconds":31}' >/dev/null
code "复验拿到 REVALIDATED_200，原文与 gzip ETag 同时换代"
curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" -H "$H_IDENTITY" | grep -iE 'HTTP|x-cache|etag'
curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" -H "$H_GZIP" | grep -iE 'HTTP|x-cache|etag|content-encoding'

line "7. 切换源站 Vary 规则 -> 只按 Accept，复验发现后旧规则条目整体失效"
code curl -s -X POST "$ORIGIN/admin/vary/accept"
curl -s -X POST "$ORIGIN/admin/vary/accept"; echo
curl -s -X POST "$PROXY/_cache/age" -H 'Content-Type: application/json' -d '{"seconds":31}' >/dev/null
code curl -si "$PROXY/releases" -H "'$H_JSON'" -H 'Accept-Language: zh-CN'
curl -sD - -o /dev/null "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | grep -iE 'HTTP|x-cache|vary'
code "新规则下再用中文头请求：语言不参与键，直接 HIT 同一份英文正文"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: zh-CN' | grep -iE 'x-cache|content-language'
curl -s "$PROXY/_cache/inspect" | python3 -m json.tool 2>/dev/null | grep -E 'confirmed_vary|generation|entry_count|gzip_'

line "8. 回源失败：代理报 502，已确认缓存保持不变"
code curl -s -X POST "$ORIGIN/admin/fail/1"
curl -s -X POST "$ORIGIN/admin/fail/1"; echo
code "curl -si $PROXY/releases -H 'Accept: text/markdown' （冷变体，回源失败）"
curl -si "$PROXY/releases" -H "$H_MD" | grep -iE 'HTTP|x-cache'
code "已缓存的英文 JSON 仍可命中"
curl -si "$PROXY/releases" -H "$H_JSON" -H 'Accept-Language: en' | grep -iE 'x-cache'

line "9. 并发演示（可选）：并行回源、并发压缩共用、切换瞬间在途隔离"
code "PROXY_BASE=$PROXY ORIGIN_BASE=$ORIGIN python -m app.tests.selftest"
echo "完整并发自测请运行：python -m app.tests.selftest（123 项断言，含并发压缩与正文换代）"
echo
line "演示结束"
