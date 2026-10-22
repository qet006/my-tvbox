#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动发现新源 —— 每天跑一次，让源池"会长大"。

三条发现渠道：
  1. GitHub Search API 扫最近更新的 TVBox 配置仓库
  2. 已收录源的备用域名（同后端不同入口）
  3. sources/seed_api.txt 里人工补充的候补地址

准入四道闸：
  ① 结构：标准苹果 CMS，返回 JSON 且带 list
  ② 四关：L1 列表 / L2 搜索 / L3 详情 / L4 m3u8 首行 #EXTM3U
  ③ 规模：库容 >= MIN_TOTAL 条
  ④ 连续两日存活：今天过、明天复测还过，才真正写进 allowed.json

新增源会写进 candidates.json 的 pending，第二天由本脚本或 check_health 转正。
"""
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_core as pc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "sources")
TODAY = time.strftime("%Y-%m-%d")
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
MIN_TOTAL = 3000          # 库容门槛：太小的私人库死得快
MAX_NEW_PROBE = 40        # 单次最多测多少个新接口，防止跑爆
QUERIES = [
    "topic:tvbox pushed:>7d",
    "tvbox in:name,description pushed:>7d",
    "tvbox 接口 in:readme pushed:>14d",
]
CONFIG_HINT = re.compile(r"(tvbox|box|api|config|m\.json|单仓|多仓)", re.I)


def gh(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "my-tvbox-bot",
        "Accept": "application/vnd.github+json",
        **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
    })
    with urllib.request.urlopen(req, timeout=20, context=pc._CTX) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def load(name, default):
    p = os.path.join(SRC, name)
    if not os.path.exists(p):
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save(name, obj):
    with open(os.path.join(SRC, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def classify(api):
    a = (api or "").strip()
    if a.startswith(("csp_", "py_", "./")) or a.endswith(".jar"):
        return "spider"
    if a.endswith(".js") or ".js?" in a:
        return "js"
    if "api.php" in a or "provide/vod" in a or "/vod" in a:
        return "cms"
    return "other"


def load_tvbox_json(text):
    """TVBox 配置常带 // 注释，标准 json 会直接崩。"""
    t = text.strip()
    if t.startswith("**"):  # base64 编码配置
        try:
            t = base64.b64decode(t.strip("*").strip()).decode("utf-8", "ignore")
        except Exception:
            return None
    t = re.sub(r"^\s*//.*$", "", t, flags=re.M)
    t = re.sub(r'(?<!:)//[^"\n]*$', "", t, flags=re.M)
    try:
        return json.loads(t)
    except Exception:
        return None


def discover_repos(limit=25):
    """从 GitHub 搜索最近更新的配置仓库，返回 [(repo, [候选配置文件 url])]"""
    found, seen = [], set()
    for q in QUERIES:
        try:
            url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({
                "q": q, "sort": "updated", "order": "desc", "per_page": 20})
            data = gh(url)
        except Exception as e:
            print(f"  [discover] 查询失败 {q}: {type(e).__name__}")
            continue
        for repo in data.get("items", []):
            full = repo["full_name"]
            if full in seen:
                continue
            seen.add(full)
            if repo.get("archived") or repo.get("stargazers_count", 0) < 1:
                continue
            found.append((full, repo.get("pushed_at", "")))
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break
    return found


def config_urls(full, branch="main"):
    """列出仓库根目录里像配置的文件，返回 raw 地址"""
    out = []
    for br in (branch, "master"):
        try:
            items = gh(f"https://api.github.com/repos/{full}/contents/?ref={br}")
        except Exception:
            continue
        for it in items:
            name = it.get("name", "")
            if not name.lower().endswith((".json", ".txt")):
                continue
            if not CONFIG_HINT.search(name):
                continue
            if it.get("size", 0) < 400:
                continue
            out.append(it.get("download_url") or
                       f"https://raw.githubusercontent.com/{full}/{br}/{urllib.parse.quote(name)}")
            if len(out) >= 3:
                return out
        if out:
            return out
    return out


def extract_apis(cfg):
    apis = []
    for s in cfg.get("sites", []) or []:
        api = (s.get("api") or "").strip()
        if classify(api) == "cms" and api.startswith("http"):
            apis.append((s.get("name") or "", api))
    return apis


def main():
    allowed = load("allowed.json", {"sources": []})
    cands = load("candidates.json", {"items": []})
    items0 = cands.get("items", [])
    known = {s["api"].split("?")[0].rstrip("/").lower() for s in allowed.get("sources", [])}
    # pending 的必须每天复检（这就是"连续两日存活"的实现），不能算进已知
    known |= {c["api"].split("?")[0].rstrip("/").lower()
              for c in items0 if c.get("status") != "pending"}
    print(f"[discover] 已知接口 {len(known)} 个")

    # ---------- 渠道 1：GitHub 仓库 ----------
    new_apis = [(c.get("name") or "", c["api"]) for c in items0 if c.get("status") == "pending"]
    if new_apis:
        print(f"[discover] 待复检的 pending 候选 {len(new_apis)} 个")
    _seen_new = {a.split("?")[0].rstrip("/").lower() for _, a in new_apis}
    repos = discover_repos()
    print(f"[discover] 命中 {len(repos)} 个最近活跃仓库")
    for full, pushed in repos:
        for u in config_urls(full):
            ok, st, body, ms, ch, err = pc.http_get_dual(u, 20, stream_bytes=800000)
            if not ok or len(body) < 400:
                continue
            cfg = load_tvbox_json(body.decode("utf-8", "ignore"))
            if not isinstance(cfg, dict):
                continue
            apis = extract_apis(cfg)
            fresh = [(n, a) for n, a in apis
                     if a.split("?")[0].rstrip("/").lower() not in known]
            if fresh:
                print(f"  {full:<44} 配置 {len(apis)} 站 → 新接口 {len(fresh)}")
                for n, a in fresh:
                    key = a.split("?")[0].rstrip("/").lower()
                    if key in _seen_new:
                        continue
                    _seen_new.add(key)
                    new_apis.append((f"{full.split('/')[-1]}:{n}"[:40], a))
            if len(new_apis) >= MAX_NEW_PROBE:
                break
        if len(new_apis) >= MAX_NEW_PROBE:
            break

    # ---------- 渠道 2：人工 seed ----------
    seed = os.path.join(SRC, "seed_api.txt")
    if os.path.exists(seed):
        for line in open(seed, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or not line.startswith("http"):
                continue
            lk = line.split("?")[0].rstrip("/").lower()
            if lk in known or lk in _seen_new:
                continue
            _seen_new.add(lk)
            new_apis.append(("seed", line))

    print(f"[discover] 待测新接口 {len(new_apis)} 个")
    if not new_apis:
        print("[discover] 没有新候选")
        return 0

    # ---------- 准入实测 ----------
    items = cands.setdefault("items", [])
    by_api = {c["api"].split("?")[0].rstrip("/").lower(): c for c in items}
    passed = 0
    for name, api in new_apis[:MAX_NEW_PROBE]:
        rec = pc.probe_cms(api)
        key = api.split("?")[0].rstrip("/").lower()
        total = rec.get("l1_total") or 0
        ok = bool(rec.get("l4_ok")) and total >= MIN_TOTAL
        rec_name = rec.get("detail_name") or name
        entry = by_api.get(key)
        if entry is None:
            entry = {"api": api, "name": name, "status": "quarantine",
                     "first_seen": TODAY, "pass_streak": 0}
            items.append(entry)
            by_api[key] = entry
        entry["last_check"] = TODAY
        entry["verdict"] = rec.get("verdict")
        entry["total"] = total
        if ok:
            # 同一天多次运行不重复累加，保证真的是"连续两日"存活才转正
            last_pass = entry.get("last_pass")
            if last_pass != TODAY:
                entry["pass_streak"] = int(entry.get("pass_streak") or 0) + 1
                entry["last_pass"] = TODAY
            entry["score"] = rec.get("score")
            if entry["pass_streak"] >= 2:
                entry["status"] = "active"
                print(f"  [转正] {rec_name[:20]:<22} {api[:56]} 库容 {total}")
                passed += 1
            else:
                entry["status"] = "pending"
                print(f"  [待复检] {rec_name[:20]:<22} {api[:56]} 库容 {total}")
        else:
            entry["pass_streak"] = 0
            entry["status"] = "quarantine"
            print(f"  [淘汰] {api[:56]:<58} {str(rec.get('verdict'))[:12]}")

    # 转正的加进 allowed
    exist = {s["api"].split("?")[0].rstrip("/").lower() for s in allowed.get("sources", [])}
    added = 0
    for c in items:
        if c.get("status") != "active":
            continue
        k = c["api"].split("?")[0].rstrip("/").lower()
        if k in exist:
            continue
        base = re.sub(r"[^a-zA-Z0-9]", "", (c.get("name") or "src").split(":")[-1])[:10].lower()
        host = urllib.parse.urlparse(c["api"]).hostname or ""
        parts = host.split(".")
        if len(parts) >= 2:
            base = re.sub(r"[^a-z0-9]", "", parts[-2])[:12] or base
        base = base or f"new{added}"
        key = base
        n = 1
        while any(s["key"] == key for s in allowed["sources"]):
            n += 1
            key = f"{base}{n}"
        allowed["sources"].append({
            "key": key, "name": key.upper(),
            "api": c["api"], "type": 3, "searchable": 1, "categories": [],
            "total": c.get("total"), "score": c.get("score"),
            "status": "active", "added": TODAY, "last_ok": TODAY, "fail_streak": 0,
        })
        exist.add(k)
        added += 1

    allowed["updated"] = TODAY
    save("allowed.json", allowed)
    save("candidates.json", cands)
    print(f"[discover] 本轮转正 {added} 个，pending {sum(1 for c in items if c.get('status')=='pending')} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
