# -*- coding: utf-8 -*-
"""
TVBox 源全量测活引擎（纯标准库，可在 GitHub Actions 里直接跑）

设计要点：
1. 每个 site 条目都测，不抽样；相同 api 只发一次请求、结果复用（报告里注明映射关系）。
2. L1 列表 / L2 搜索 / L3 详情取播放地址 / L4 m3u8 首行校验，四级逐级淘汰。
3. 双通道：先按系统代理请求，失败再强制直连重试一次，避免把"本机代理坏了"误判成"源死了"。
4. L4 只流式读前 2KB 就断开，不整份下载 m3u8。
"""
import json, re, ssl, time, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_CTX))


def http_get(url, timeout=8, ua=UA, stream_bytes=0):
    """返回 (status, body_bytes, elapsed, err)；stream_bytes>0 时只读前 N 字节。"""
    t = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
        r = urllib.request.urlopen(req, timeout=timeout, context=_CTX)
        body = r.read(stream_bytes) if stream_bytes else r.read()
        if stream_bytes:
            r.close()
        return r.status, body, round(time.time() - t, 2), None
    except Exception as e:
        return 0, b"", round(time.time() - t, 2), f"{type(e).__name__}: {str(e)[:120]}"


def http_get_dual(url, timeout=8, ua=UA, stream_bytes=0):
    """先走系统代理，失败后强制直连再试一次。返回 (ok, status, body, elapsed, channel, err)。"""
    st, body, ms, err = http_get(url, timeout, ua, stream_bytes)
    if st and 200 <= st < 400:
        return True, st, body, ms, "proxy", None
    st2, body2, ms2, err2 = (0, b"", 0, "skip")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
        t = time.time()
        r = _DIRECT.open(req, timeout=timeout)
        body2 = r.read(stream_bytes) if stream_bytes else r.read()
        if stream_bytes:
            r.close()
        st2, ms2, err2 = r.status, round(time.time() - t, 2), None
    except Exception as e:
        err2 = f"{type(e).__name__}: {str(e)[:120]}"
    if st2 and 200 <= st2 < 400:
        return True, st2, body2, ms2, "direct", None
    return False, st2 or st, b"", ms2, "none", err2 or err


def _json(body):
    return json.loads(body.decode("utf-8", "ignore"))


def probe_cms(api, keyword="庆余年", timeout=8, deep=True):
    """标准苹果CMS(v10) 站点四级测活。"""
    rec = {"api": api, "kind": "cms"}
    base = api.split("?")[0]

    ok, st, body, ms, ch, err = http_get_dual(base, timeout)
    rec.update(l1_ok=ok, l1_status=st, l1_ms=ms, l1_channel=ch, l1_err=err)
    if not ok:
        return rec
    try:
        d = _json(body)
        lst = d.get("list") or []
        rec.update(l1_list_n=len(lst), l1_total=d.get("total"),
                   l1_first=(lst[0].get("vod_name") if lst else None),
                   l1_first_time=(lst[0].get("vod_time") if lst else None))
        if not lst:
            rec["verdict"] = "无数据"
            return rec
    except Exception as e:
        rec.update(verdict="非JSON/非CMS", note=f"{type(e).__name__}")
        return rec

    if not deep:
        rec["verdict"] = "L1通过"
        return rec

    # L2 搜索
    ok2, st2, b2, ms2, ch2, err2 = http_get_dual(
        f"{base}?ac=videolist&wd={urllib.parse.quote(keyword)}", timeout)
    rec.update(l2_ok=ok2, l2_n=0, l2_channel=ch2)
    if ok2:
        try:
            d2 = _json(b2)
            rec["l2_n"] = len(d2.get("list") or [])
        except Exception:
            rec["l2_ok"] = False

    # L3 详情 + 播放地址
    vid = None
    try:
        d2 = _json(b2)
        for it in (d2.get("list") or []):
            if it.get("vod_id"):
                vid = it["vod_id"]
                break
    except Exception:
        pass
    if vid is None:
        try:
            vid = (_json(body).get("list") or [{}])[0].get("vod_id")
        except Exception:
            pass
    if vid is None:
        rec["verdict"] = "取不到详情ID"
        return rec

    ok3, st3, b3, ms3, ch3, err3 = http_get_dual(f"{base}?ac=videolist&ids={vid}", timeout)
    if not ok3:
        rec.update(l3_ok=False, verdict="详情失败")
        return rec
    try:
        it = (_json(b3).get("list") or [{}])[0]
    except Exception:
        rec.update(l3_ok=False, verdict="详情非JSON")
        return rec
    pu = it.get("vod_play_url") or ""
    urls = re.findall(r"(https?://[^\s#$]+?\.m3u8)", pu)
    rec.update(l3_ok=bool(pu), play_lines=len(re.findall(r"\$", pu)) + (1 if pu else 0),
               m3u8_n=len(urls), detail_name=it.get("vod_name"))
    if not urls:
        rec["verdict"] = "有详情无m3u8"
        return rec

    # L4 m3u8 首行（只读 2KB）
    ok4, st4, b4, ms4, ch4, err4 = http_get_dual(urls[0], timeout, stream_bytes=2048)
    head = b4.decode("utf-8", "ignore")
    rec.update(l4_ok=bool(ok4 and "#EXTM3U" in head), l4_status=st4, l4_ms=ms4,
               l4_sample=urls[0][:100], l4_err=err4)
    rec["verdict"] = "四级通过" if rec["l4_ok"] else "播放地址不可播"
    return rec


def probe_live(url, timeout=6):
    """直播播放列表：取内容并抽验首条 HLS。"""
    ok, st, body, ms, ch, err = http_get_dual(url, timeout * 2, stream_bytes=200000)
    rec = {"url": url, "kind": "live_list", "ok": ok, "status": st, "ms": ms, "channel": ch, "err": err}
    if not ok:
        rec["verdict"] = "列表不可达"
        return rec
    txt = body.decode("utf-8", "ignore")
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    chans = []
    for l in lines:
        if l.startswith("#") or "#genre#" in l or "," not in l:
            continue
        n, u = l.split(",", 1)
        if u.startswith("http"):
            chans.append((n.strip(), u.strip()))
    rec.update(lines=len(lines), channels=len(chans), uniq_ch=len(set(n for n, _ in chans)),
               groups=len([l for l in lines if l.endswith("#genre#")]))
    if chans:
        ok2, st2, b2, ms2, ch2, e2 = http_get_dual(chans[0][1], timeout, stream_bytes=1024)
        rec.update(first_ch=chans[0][0], first_ok=bool(ok2 and "#EXTM3U" in b2.decode("utf-8", "ignore")))
    rec["verdict"] = "列表可用" if rec.get("channels") else "无有效频道行"
    return rec


def score(rec):
    """0-100 分。权重：可播 40 / 数据量 15 / 搜索 15 / 速度 15 / 新鲜度 15。"""
    s = 0.0
    if rec.get("l4_ok"):
        s += 40
    elif rec.get("l3_ok"):
        s += 20
    elif rec.get("l1_ok"):
        s += 8
    n = rec.get("l1_total") or 0
    s += 15 if n > 50000 else 10 if n > 10000 else 5 if n > 1000 else 0
    s += 15 if (rec.get("l2_n") or 0) > 0 else 0
    ms = rec.get("l1_ms") or 9
    s += 15 if ms < 1 else 10 if ms < 2.5 else 5 if ms < 5 else 0
    ft = rec.get("l1_first_time") or ""
    if ft[:10] == time.strftime("%Y-%m-%d"):
        s += 15
    elif ft[:7] == time.strftime("%Y-%m"):
        s += 10
    return round(s, 1)


def run_batch(items, kind="cms", workers=32, keyword="庆余年"):
    """items: [(name, api)] → list[dict]"""
    def one(it):
        name, api = it
        try:
            rec = probe_cms(api, keyword=keyword) if kind == "cms" else probe_live(api)
        except Exception as e:
            rec = {"api": api, "kind": kind, "verdict": f"异常 {type(e).__name__}"}
        rec["name"] = name
        rec["score"] = score(rec) if kind == "cms" else 0
        return rec
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, items))
