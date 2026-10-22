#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
my-tvbox 配置生成器

输入：sources/allowed.json（活源池）、sources/live.json（直播源）、sources/pc_extra.json（可选的 jar 蜘蛛源）
输出：dist/ 下四份配置 + 状态文件

设计原则
1. 只输出 status=active 的源；quarantine / retired 一律不进产物
2. key 全局唯一（重复会让 TVBox 静默覆盖站点），生成前强制断言
3. api 必须是无 query 的纯路径，避免客户端拼接 ?ac=videolist&wd= 时参数重复
4. 分类优先用 sources 里缓存的 categories，缺失的客户端自己请求
5. 分三份方言：标准 TVBox / TVBoxPC（含 jar）/ Electron（type:"cms"）
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "sources")
DIST = os.path.join(ROOT, "dist")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load(name, default=None):
    path = os.path.join(SRC, name)
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def clean_api(api):
    """剥掉 query 与多余尾斜杠，保证客户端拼接参数干净。"""
    api = (api or "").strip()
    if "?" in api:
        api = api.split("?")[0]
    return api.rstrip("/") + "/" if "/provide/vod" in api and not api.endswith("/") else api


def make_site(s, extra=None):
    item = {
        "key": s["key"],
        "name": s["name"],
        "type": int(s.get("type") or 3),
        "api": clean_api(s["api"]),
        "searchable": int(s.get("searchable") or 0),
        "quickSearch": int(s.get("searchable") or 0),
        "filterable": 1,
    }
    if s.get("categories"):
        item["categories"] = s["categories"][:40]
    if s.get("ext"):
        item["ext"] = s["ext"]
    if s.get("playerType"):
        item["playerType"] = s["playerType"]
    if extra:
        item.update(extra)
    return item


def build_lives(live_cfg):
    lists = sorted((live_cfg or {}).get("lists", []), key=lambda x: x.get("priority", 99))
    return [
        {"name": L["name"], "type": int(L.get("type", 0)), "url": L["url"]}
        for L in lists
        if L.get("url")
    ]


SPIDER_PREFIX = ("csp_", "py_", "./")


def verify(sites, name, allow_spider=False):
    keys = [s["key"] for s in sites]
    assert len(keys) == len(set(keys)), f"{name}: key 重复 -> {[k for k in keys if keys.count(k) > 1][:5]}"
    for s in sites:
        api = s["api"]
        if allow_spider and api.startswith(SPIDER_PREFIX):
            continue  # csp_/py_ 是客户端内置蜘蛛名，不是 URL
        assert api.startswith("http"), f"{name}: 非法 api {api}"
        assert "?" not in api, f"{name}: api 含 query {api}"
    return True


def write(name, obj):
    os.makedirs(DIST, exist_ok=True)
    path = os.path.join(DIST, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    # 回读校验：写坏了的 JSON 会让客户端整份配置加载失败
    with open(path, "r", encoding="utf-8") as f:
        json.load(f)
    return path, os.path.getsize(path)


def main():
    allowed = load("allowed.json", {"sources": []})
    live_cfg = load("live.json", {"lists": []})
    pc_extra = load("pc_extra.json", {"sources": []})

    sources = [s for s in allowed.get("sources", []) if s.get("status", "active") == "active"]
    sources.sort(key=lambda s: (-(s.get("searchable") or 0), -(s.get("score") or 0)))
    if not sources:
        print("没有可用源，中止", file=sys.stderr)
        return 1

    lives = build_lives(live_cfg)
    parses = allowed.get("parses") or []
    flags = allowed.get("flags") or []

    # 1) 标准版：所有 Windows / Android 端通用，纯 CMS + 直播
    sites = [make_site(s) for s in sources]
    verify(sites, "standard")
    payload = {
        "wallpaper": allowed.get("wallpaper", ""),
        "logo": allowed.get("logo", ""),
        "sites": sites,
        "lives": lives,
        "parses": parses,
        "flags": flags,
        "doh": [],
        "rules": [],
    }
    p1, n1 = write("tvbox.json", payload)

    # 2) TVBoxPC 增强版：额外并入 jar 蜘蛛源（只有 TVBoxPC 这类带 JVM 的客户端能跑）
    pc_sites = list(sites)
    used = {s["key"] for s in pc_sites}
    for s in pc_extra.get("sources", []):
        if s.get("status", "active") != "active":
            continue
        key = s.get("key") or ""
        if not key or key in used:
            continue
        pc_sites.append(make_site(s))
        used.add(key)
    verify(pc_sites, "pc", allow_spider=True)
    pc_payload = dict(payload)
    pc_payload["sites"] = pc_sites
    if pc_extra.get("spider"):
        pc_payload["spider"] = pc_extra["spider"]
    p2, n2 = write("tvbox-pc.json", pc_payload)

    # 3) Electron 版方言：type 用 "cms"，扩展字段放 tvboxDesktop，标准端不认这些
    desk_sites = []
    for s in sources:
        if int(s.get("type") or 3) not in (3, 4):
            continue
        desk_sites.append({
            "key": s["key"],
            "name": s["name"],
            "type": "cms",
            "api": clean_api(s["api"]),
            "searchable": int(s.get("searchable") or 0),
        })
    home = allowed.get("desktop_home") or [
        {"title": "电影 · 最近更新", "site": desk_sites[0]["key"]} if desk_sites else {}
    ]
    desk_payload = {
        "format": "tvbox-desktop/1",
        "name": "my-tvbox 活源",
        "note": "自动生成：全部源经过四级测活（列表/搜索/详情/m3u8 首行）",
        "sites": desk_sites,
        "lives": [{"name": L["name"], "type": 0, "url": L["url"]} for L in lives],
        "tvboxDesktop": {"home": {"sections": home}},
    }
    p3, n3 = write("tvbox-desktop.json", desk_payload)

    # 4) 多仓索引
    base = (allowed.get("public_base") or "").rstrip("/")
    if base:
        multi = {
            "urls": [
                {"url": f"{base}/dist/tvbox.json", "name": "通用版"},
                {"url": f"{base}/dist/tvbox-pc.json", "name": "TVBoxPC 增强版"},
            ]
        }
        p4, n4 = write("multi.json", multi)
    else:
        p4 = n4 = 0

    # 5) 状态文件（给徽章和给自己看）
    status = {
        "updated": NOW,
        "counts": {"active": len(sources), "searchable": sum(1 for s in sources if s.get("searchable")),
                   "sites_standard": len(sites), "sites_pc": len(pc_sites), "lives": len(lives)},
        "sources": [
            {"key": s["key"], "name": s["name"], "api": s["api"], "score": s.get("score"),
             "searchable": s.get("searchable"), "total": s.get("total"),
             "last_ok": s.get("last_ok"), "fail_streak": s.get("fail_streak", 0)}
            for s in sources
        ],
    }
    p5, n5 = write("status.json", status)

    # 6) 徽章数据（shields.io endpoint 格式，README 直接引用）
    badge = {
        "schemaVersion": 1,
        "label": "活源",
        "message": f"{len(sources)} 个",
        "color": "brightgreen" if len(sources) >= 15 else ("yellow" if len(sources) >= 8 else "red"),
    }
    write("badge.json", badge)

    print(f"标准版   {os.path.basename(p1):<22} {len(sites):>3} 站点  {n1:>6} 字节")
    print(f"PC 增强  {os.path.basename(p2):<22} {len(pc_sites):>3} 站点  {n2:>6} 字节")
    print(f"Electron {os.path.basename(p3):<22} {len(desk_sites):>3} 站点  {n3:>6} 字节")
    if p4:
        print(f"多仓索引 {os.path.basename(p4):<22}   2 项   {n4:>6} 字节")
    print(f"状态     {os.path.basename(p5):<22}         {n5:>6} 字节")
    print(f"直播源 {len(lives)} 个 / 可搜索站点 {status['counts']['searchable']} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
