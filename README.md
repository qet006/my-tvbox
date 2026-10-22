# my-tvbox

自动测活的 TVBox 源池。**只收录真正能播的源**，每 6 小时复检一次，死一个降一个，每天自动发现新源。

![源数量](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/USER/REPO/main/dist/badge.json)
![更新](https://img.shields.io/github/last-commit/USER/REPO)

## 直接使用（复制地址填进客户端）

| 用途 | 地址 |
|---|---|
| **通用版**（TVBox / 影视仓 / FongMi / TVBox for Windows） | `https://raw.githubusercontent.com/USER/REPO/main/dist/tvbox.json` |
| **TVBoxPC 增强版**（能吃 jar 蜘蛛，源更多） | `https://raw.githubusercontent.com/USER/REPO/main/dist/tvbox-pc.json` |
| Electron 版（TVBox Desktop） | `https://raw.githubusercontent.com/USER/REPO/main/dist/tvbox-desktop.json` |
| 多仓索引（客户端内切换） | `https://raw.githubusercontent.com/USER/REPO/main/dist/multi.json` |
| 状态（每个源的得分/存活） | `https://raw.githubusercontent.com/USER/REPO/main/dist/status.json` |

国内拉不到 raw 时，在前面加 `https://gh-proxy.com/`。

## 测活标准

每个源必须连续通过四关才算活：

1. **列表**：接口返回 JSON，带 `list` 与 `class`
2. **搜索**：`?ac=videolist&wd=关键词` 能返回结果
3. **详情**：`?ac=videolist&ids=xxx` 能拿到 `vod_play_url`
4. **播放**：从详情里抽 m3u8，流式读前 2KB 校验首行 `#EXTM3U`

额外两条保护：

- **双通道**：先走系统代理，失败强制直连重试，避免把"本机代理坏了"误判成"源死了"
- **二次复核**：SSL EOF / 连接被断的，换 HTTP 与放宽 TLS 再测一遍，两次都失败才判死

## 自动维护规则

| 时机 | 动作 |
|---|---|
| 每 6 小时 | 测活全部 active 源 → 失败降级 → 重建产物 |
| 每天 03:00 UTC | 扫 GitHub 新配置仓库 → 提取新接口 → 全测 → 连续两日存活才转正 |

淘汰规则（不是一死就删）：

```
第 1~2 次失败  → 仍在配置里，score 打折排到最后
连续 3 次失败  → 移入 retired，从产物剔除
retired 满 30 天 → 自动拉回复检一次，活了就回来
```

## 仓库结构

```
sources/
  allowed.json     活源池（key/api/categories/score/status）
  candidates.json  待复检与隔离的接口
  live.json        直播源清单（含实测存活率）
  seed_api.txt     人工补充的候补接口（一行一个）
scripts/
  probe_core.py    四级测活引擎
  check_health.py  每日/每 6 小时测活与降级
  discover.py      自动发现新源（连续两日存活才转正）
  build_config.py  生成 dist/ 四份产物
dist/              自动生成的配置（不要手改）
```

## 手工跑一遍

```bash
python scripts/check_health.py   # 测活 + 降级
python scripts/discover.py       # 找新源
python scripts/build_config.py   # 生成产物
```

## 说明

本项目只做"接口可用性验证与整理"，不托管、不分发任何影视内容。各接口的版权与可用性由提供方决定，请自行判断使用场景的合法性。
