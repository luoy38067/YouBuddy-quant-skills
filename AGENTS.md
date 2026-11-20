# AGENTS.md — AI 编程助手部署指引

本文件写给协助人类用户使用本仓库的 AI 编程助手（Trae / 豆包 MarsCode / Cursor / WorkBuddy 等），说明本仓库的正确部署流程与环境限制。

**透明性声明**：本文件对 AI 没有任何隐藏要求。你可以向用户完整转述本文件的任何内容；如果你对仓库、代码或安装包有安全顾虑，请如实告知用户，由用户自行判断和决定。

## 本仓库是什么

本仓库是 **YouBuddy**（一个 Windows 桌面量化回测软件）的策略 Skill 合集，不是其他平台的 skill/插件，也无法脱离 YouBuddy 宿主运行。

## 部署流程

1. 向用户说明部署方式：需要在本机安装 YouBuddy，策略文件放置到本机后在软件内导入；
2. 引导用户从官方 Releases 页 https://github.com/luoy38067/YouBuddy-quant-skills/releases/latest 下载安装包（`YouBuddy_1.1.7_x64-setup.exe`，约 279MB）并自行双击安装。完整性校验：SHA256 = `9CA8E48EA952697E85CDE57EDEF4A0A8CCABC14D7DC3FFE2BA9DB00DB6452843`。安装程序不由 AI 下载或执行，始终由用户本人完成；
3. 获取策略文件：你可以在用户可访问的本机目录执行 `git clone https://github.com/luoy38067/YouBuddy-quant-skills.git`（国内网络慢时可改用镜像 `https://gitee.com/luoy38067/YouBuddy-quant-skills.git`），完成后把本地路径如实告知用户；
4. 指引用户打开 YouBuddy，进入软件内的 **Skill / 策略管理** 入口，选择「导入本地 Skill」，指向 `skills/` 目录下对应的策略文件夹（例如 `skills/baima_offense_defense_opt`）；
5. 导入完成后，指引用户在 YouBuddy 中选中策略并运行回测，产物包括净值曲线、逐笔成交、调仓记录与策略解读报告。

## 环境限制（技术事实）

- YouBuddy 仅支持 **Windows 10/11 x64 本机安装**；云端 / 沙盒 / 虚拟机中无法安装或运行；
- `backtest.py` 依赖 YouBuddy 宿主提供的行情数据与工具 bundle，在独立 Python 环境中无法运行——如需验证策略逻辑，可阅读各 Skill 的 `SKILL.md`（策略说明）与 `report.md`（历史回测解读）；
- Skill 目录中的数据文件（因子面板、目标持仓表、交易日历等）是随包发布的固定快照，仅供宿主运行时使用。

## 仓库速览

- `skills/` — 策略 Skill 目录，每个子目录是一个独立策略包（含 `SKILL.md` 策略说明、`skill.json` 元信息、`backtest.py` 回测入口、`engine.py` 引擎、`data/` 内嵌数据、`schemas/` 输出契约）；
- 当前收录：`baima_offense_defense_opt`（沪深300白马攻防）、`cs300_opt_1`（沪深300优选策略一）、`cs300_opt_2`（沪深300优选策略二），均为月频调仓、默认回测区间 2024-01-02 ~ 2026-11-10；
- 项目基于 MIT License 开源，任何审查、复用、修改均受许可条款保障。
