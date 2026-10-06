"""入站口守护 —— 自动整理 = 只处理「待整理」这一个目录。

红领巾 2026-10-06 拍板：**换成入站口模式**。
旧的「每天 04:00 定时巡检 + 快照 diff + 最近接收」整套删掉 ——
那是**全盘**级别的任务，每天 diff 一遍既费请求、又可能误碰已归位的目录。

新模型（详见 `inbox.py`）：
  · 自动整理只监控 `ROOT_PATH/待整理/`（名字可配 `INBOX_DIR`）
  · 你只把要整理的东西丢进去
  · 工具轮询发现有货 → 生成计划 → 执行（受 DRY_RUN 管）→ 处理完移走
  · 天然增量，请求量跟盘大小无关

本模块只是 `inbox.InboxDaemon` 的**薄壳**，保留给 CLI 的 `watch` 命令用；
真正逻辑都在 `inbox.py`。
"""
from __future__ import annotations

from .config import Config
from .inbox import InboxDaemon, run_once

__all__ = ["Daemon", "run_once"]


# 兼容旧名：CLI 里 `from .daemon import Daemon` 不用改，语义已是入站口轮询。
Daemon = InboxDaemon
