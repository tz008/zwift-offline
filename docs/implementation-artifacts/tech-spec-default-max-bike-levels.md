---
title: 默认解锁全部装备并将车辆设为满级
type: feature
created: '2026-11-13'
status: done
---

# 默认解锁全部装备并将车辆设为满级

## 行为与范围

沿用启动时自动创建的 `storage/unlock_all_equipment.txt`，默认解锁全部装备，包括车辆、轮组及特殊装备；所有车辆默认满级 5/5。人物骑行与跑步等级、经验、汗水（Drops）余额及已有骑行统计不变。

车辆升级进度通过 `/api/player-profile/user-game-storage/attributes?n=24` 下发。读取时补全车型并设为满级；保存时在原有合并逻辑之后重新应用满级，防止客户端旧进度覆盖。其他类别的属性继续按请求筛选。已有车辆里程、爬升、骑行时间、升级历史及其他设置保留。

`data/game_dictionary.txt` 新增完整的 `bike_upgrade_frames`，从当前公开 GameDictionary.xml 获取全部 175 个车架，包括 Brompton P Line（3017804836）。机器人随机装备使用原来的 163 个安全车架；完整升级清单不受机器人特殊车型排除规则影响。`scripts/get_game_dictionary.py` 在后续更新数据时同步生成完整升级清单。客户端保存的新增车型也会设为满级。

装备权限下发同时会恢复已有的过期或未激活 USE 权限为 ACTIVE，继续保留人物的非权限字段。

## 验证与交付

- 全量 200 项测试通过，其中 9 项新增测试覆盖车辆完整清单、新玩家、按类别读取、旧进度保存、玩家隔离、数据保留和装备权限。
- `pip check` 与相关变更的 `git diff --check` 通过。
- 使用正式 EXE 复制品在独立中文空格目录和替代 loopback 端口启动真实服务，HTTPS 玩家资料及权限接口均返回全部 2256 项 ACTIVE 装备权限；全部 175 个车架为 5/5，Brompton、保存旧进度及服务重启均通过。
- 真实服务验证中，人物资料文件逐字节保持不变；车库已有统计、升级历史和设置保留；已有解锁标记的内容与修改时间在重启后保留。验证 worker 已退出，独立运行目录已清理。未启动实际 Zwift 客户端验证车库显示。
- 正式产物为 `dist/zoffline.exe`，34,669,431 字节，SHA-256 `ca043edba184a9ea9b7d7fb097daccc17852898121aff32291e0b12823434bab`。已核对归档包含完整车型及满级逻辑，不包含玩家 storage/logs。
- 本机源码与 EXE 数据目录的 1 号玩家车库均已补齐 175 个满级车辆。原始 `user_storage.bin` 已各自备份为同目录的 `user_storage.before-max-bike-levels.20261113-103435.bak`；两处 `profile.bin` 的完整 SHA-256 均与修改前一致。

运行新版 EXE 并重新登录游戏即可刷新装备权限和车辆等级。源码修改不会自动更新其他位置的旧 EXE。
