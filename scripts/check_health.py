#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日健康检查：测活 allowed.json 里的所有源，死一个降一个。

降级规则（不是一死就删，避免抖动误杀）：
  fail_streak = 0        → active，正常排序
  fail_streak = 1~2      → active，但 score 打折，排到后面（等于降级）
  fail_streak >= 3       → retired，移出配置
  retired 满 30 天       → 自动拉回复检一次，活了就回来

同时复检 candidates.json 里隔离的接口（源站可能恢复），复活即回到 active。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_core as pc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "sources")
TODAY = time.strftime("%Y-%m-%d")
RETIRE_AFTER = 3          # 连续失败几次移出
REVIVE_AFTER_DAYS = 30    # retired 满多少天尝试复活


def load(name, default):
    p = os.path.join(SRC, name)
    if not os.path.exists(p):
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save(name, obj):
    p = os.path.join(SRC, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def apply_result(s, ok, rec):
    if ok:
        was = s.get("status")
        s["fail_streak"] = 0
        s["last_ok"] = TODAY
        s["status"] = "active"
        s["score"] = rec.get("score") or s.get("score")
        if rec.get("l1_total"):
            s["total"] = rec["l1_total"]
        if rec.get("l1_first_time"):
            s["last_update_at"] = rec["l1_first_time"]
        if was == "retired":
            s["revived_at"] = TODAY
        return "ok"
    s["fail_streak"] = int(s.get("fail_streak") or 0) + 1
    s["last_fail"] = TODAY
    if s["fail_streak"] >= RETIRE_AFTER:
        s["status"] = "retired"
        s["retired_at"] = TODAY
        return "retired"
    # 降级：仍在配置里，但 score 打折排后
    s["score"] = round(max(0, (s.get("score") or 0) - 25), 1)
    s["status"] = "active"
    return "degraded"


def main():
    allowed = load("allowed.json", {"sources": []})
    cands = load("candidates.json", {"items": []})
    sources = allowed.get("sources", [])
    changed = {"ok": 0, "degraded": 0, "retired": 0, "revived": 0}

    # ---------- 1. 测活全部 active ----------
    todo = [s for s in sources if s.get("status") in ("active", None)]
    print(f"[health] 待测 {len(todo)} 个 active 源")
    for s in todo:
        try:
            rec = pc.probe_cms(s["api"])
        except Exception as e:
            rec = {"l4_ok": False, "verdict": f"异常 {type(e).__name__}"}
        r = apply_result(s, bool(rec.get("l4_ok")), rec)
        changed[r] += 1
        print(f"  {s['key']:<10} {str(rec.get('verdict'))[:10]:<12} streak={s['fail_streak']} -> {r}")

    # ---------- 2. 复活退休满 30 天的 ----------
    for s in sources:
        if s.get("status") != "retired":
            continue
        retired_at = s.get("retired_at") or ""
        try:
            days = (time.mktime(time.strptime(TODAY, "%Y-%m-%d")) -
                    time.mktime(time.strptime(retired_at, "%Y-%m-%d"))) / 86400
        except Exception:
            days = 0
        if days < REVIVE_AFTER_DAYS:
            continue
        rec = pc.probe_cms(s["api"])
        if rec.get("l4_ok"):
            s["status"] = "active"
            s["fail_streak"] = 0
            s["last_ok"] = TODAY
            s["revived_at"] = TODAY
            changed["revived"] += 1
            print(f"  [复活] {s['key']}")

    # ---------- 3. 复检隔离候选（源站可能恢复） ----------
    items = cands.get("items", [])
    revive = []
    for it in items:
        if it.get("status") == "active":
            continue
        # 每 3 天复检一次，避免天天打无效请求
        lc = it.get("last_check") or ""
        try:
            gap = (time.mktime(time.strptime(TODAY, "%Y-%m-%d")) -
                   time.mktime(time.strptime(lc, "%Y-%m-%d"))) / 86400
        except Exception:
            gap = 99
        if gap < 3:
            continue
        rec = pc.probe_cms(it["api"])
        it["last_check"] = TODAY
        if rec.get("l4_ok"):
            it["status"] = "active"
            revive.append(it)
    if revive:
        exist = {s["api"].split("?")[0].rstrip("/").lower() for s in sources}
        for it in revive:
            k = it["api"].split("?")[0].rstrip("/").lower()
            if k in exist:
                continue
            key = pc.key_from_api(it["api"], f"rev{len(sources)}")
            base_key, n = key, 1
            while any(s["key"] == key for s in sources):
                n += 1
                key = f"{base_key}{n}"
            sources.append({
                "key": key, "name": key, "api": it["api"], "type": 3,
                "searchable": 1, "categories": [], "total": None,
                "score": rec.get("score") if revive else None,
                "status": "active", "added": TODAY, "last_ok": TODAY, "fail_streak": 0,
            })
        print(f"[health] 隔离区复活 {len(revive)} 个")

    # ---------- 4. 重排：能搜的优先，然后按分数 ----------
    sources.sort(key=lambda s: (0 if s.get("status") == "active" else 1,
                                -(s.get("searchable") or 0),
                                -(s.get("score") or 0)))
    allowed["sources"] = sources
    allowed["updated"] = TODAY
    allowed["stats"] = {
        "active": sum(1 for s in sources if s.get("status") == "active"),
        "retired": sum(1 for s in sources if s.get("status") == "retired"),
        "last_run": TODAY,
        "last_changed": changed,
    }
    save("allowed.json", allowed)
    save("candidates.json", cands)
    print(f"[health] 完成：正常 {changed['ok']} / 降级 {changed['degraded']} / "
          f"淘汰 {changed['retired']} / 复活 {changed['revived']}")
    print(f"[health] 池内 active {allowed['stats']['active']} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
