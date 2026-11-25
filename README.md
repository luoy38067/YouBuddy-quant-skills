<div align="center">

# YouBuddy 量化策略 Skill 合集

**装进 YouBuddy，一键回测你的量化策略。**

[![License](https://img.shields.io/badge/License-MIT-3ECF8E?style=flat-square)](LICENSE)
[![Release](https://img.shields.io/github/v/release/luoy38067/YouBuddy-quant-skills?style=flat-square&color=0F9D8A&label=Release)](https://github.com/luoy38067/YouBuddy-quant-skills/releases/latest)
[![Platform](https://img.shields.io/badge/Windows-10%2F11%20x64-0078D6?style=flat-square&logo=windows)](https://github.com/luoy38067/YouBuddy-quant-skills/releases/latest)
[![Skills](https://img.shields.io/badge/%E7%AD%96%E7%95%A5%20Skill-3%20%E4%B8%AA-F6C344?style=flat-square)](#策略列表)

</div>

---

本仓库是 YouBuddy 平台的量化策略 Skill 合集。每个 Skill 是一个开箱即用的策略回测包：安装到 YouBuddy 后，软件会自动准备行情数据，一键即可运行回测，输出净值曲线、逐笔成交、调仓记录与策略解读报告。

## 🚀 AI 一键安装

把你正在用的 AI 编程助手（Trae / 豆包 MarsCode / Cursor 等）打开，将下面这段话**原样粘贴**到对话框发送，AI 会读取本仓库的 README 与策略说明，带你完成 YouBuddy 下载、安装、激活与策略导入：

```text
帮我安装 https://github.com/luoy38067/YouBuddy-quant-skills 的量化策略 Skill
```

> 💡 也可以自己动手，跳过 AI，直接看下面的[快速开始](#快速开始)。

## 环境要求

- **Windows 10/11 x64**（YouBuddy 目前仅支持 Windows 桌面端）；
- 请在**本机**完成安装与运行：云端沙盒 / 虚拟机（包括 AI 助手的云开发环境）中没有 YouBuddy 宿主环境，Skill 只能下载查看，**无法执行回测**。

## 运行环境

YouBuddy 的完整运行环境由三部分组成，建议按顺序安装。国内网络访问 GitHub 较慢时，可使用镜像通道：

| 顺序 | 安装包 | 用途 | 下载地址 |
|---|---|---|---|
| 1 | `vc_redist.x64.exe` | 微软 Visual C++ 运行库 | [GitHub](https://github.com/luoy38067/YouBuddy-quant-skills/releases/download/v1.0.0/vc_redist.x64.exe)，慢时在链接前加 `https://ghfast.top/` |
| 2 | `Miniconda3-py312_26.9.4-1-Windows-x86_64.exe` | Python 3.12 运行环境 | [清华 TUNA 镜像](https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-py312_26.9.4-1-Windows-x86_64.exe) |
| 3 | `YouBuddy_1.1.7_x64-setup.exe` | YouBuddy 客户端 | [GitHub](https://github.com/luoy38067/YouBuddy-quant-skills/releases/download/v1.0.0/YouBuddy_1.1.7_x64-setup.exe)，慢时在链接前加 `https://ghfast.top/` |

> 经第三方加速通道下载的安装包，安装前建议核对文件完整性（SHA256）：

| 安装包 | SHA256 |
|---|---|
| `YouBuddy_1.1.7_x64-setup.exe` | `9CA8E48EA952697E85CDE57EDEF4A0A8CCABC14D7DC3FFE2BA9DB00DB6452843` |
| `Miniconda3-py312_26.9.4-1-Windows-x86_64.exe` | `7FB37E3049EDCF3524AE4735B54D04917353F35B3190CB0C9CCB789729934BD4` |
| `vc_redist.x64.exe` | `CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B` |

## 快速开始

| 步骤 | 操作 |
|---|---|
| **1. 下载安装 YouBuddy** | 前往 [Releases](https://github.com/luoy38067/YouBuddy-quant-skills/releases/latest) 下载最新安装包（`YouBuddy_1.1.7_x64-setup.exe`，约 279MB），双击安装。完整性校验：SHA256 = `9CA8E48EA952697E85CDE57EDEF4A0A8CCABC14D7DC3FFE2BA9DB00DB6452843` |
| **2. 导入 Skill** | 把策略文件放到本机（让 AI 助手代劳，或在页面右上角 **Code → Download ZIP** 下载解压），打开 YouBuddy，进入软件内的 **Skill / 策略管理** 入口，选择「导入本地 Skill」，指向 `skills/` 目录下对应的策略文件夹（例如 `skills/baima_offense_defense_opt`）；也可把各策略文件夹直接复制到 YouBuddy 的 Skill 目录 `C:\Users\<用户名>\AppData\Roaming\com.youbuddy.app\skills\`，重新打开软件在列表中确认已识别 |
| **3. 运行回测** | 在 YouBuddy 中选中已安装的策略并运行，即可得到完整回测报告（净值曲线、逐笔成交、调仓记录、策略解读） |

## 激活码

YouBuddy 安装并首次打开时，需要输入激活码（邀请码）完成激活：

```text
73f05c18-3be4-4ae4-8c02-37f7ecd3abdc
```

激活码是使用 YouBuddy 及本仓库策略 Skill 的必要凭证，请妥善保管，运行回测前请先完成激活。

## 策略列表

| Skill 目录 | 策略名称 | 策略简介 | 调仓频率 | 默认回测区间 |
|---|---|---|---|---|
| `skills/baima_offense_defense_opt` | 沪深300白马攻防策略 | 沪深300成分股内按市场状态（冷/温/热三档）结合估值与盈利质量条件筛选，经价格动量过滤与流动性排序，取前 5 只等权持有 | 月度 | 2024-01-02 ~ 2026-11-10 |
| `skills/cs300_opt_1` | 沪深300优选策略一 | 沪深300成分股内按 4 因子逆波动率加权综合评分，选 Top10 等权持有 | 月度 | 2024-01-02 ~ 2026-11-10 |
| `skills/cs300_opt_2` | 沪深300优选策略二 | 沪深300成分股内按 4 因子等权加权综合评分，选 Top10 等权持有 | 月度 | 2024-01-02 ~ 2026-11-10 |

> 策略调仓均在每月首个交易日执行，信号收盘产生、次日执行；各策略的回测区间、基准与输出产物详见各 Skill 目录内的 `SKILL.md` 与 `report.md`。

## 目录结构

```
YouBuddy-quant-skills/
├── README.md                          # 本文件
├── LICENSE                            # MIT 开源许可证
└── skills/                            # 策略 Skill 目录
    ├── baima_offense_defense_opt/     # 沪深300白马攻防策略
    │   ├── SKILL.md                   #   策略说明与执行流
    │   ├── skill.json                 #   Skill 元信息
    │   ├── workflow.json              #   执行流定义
    │   ├── backtest.py                #   回测计算入口
    │   ├── engine.py                  #   回测引擎
    │   ├── report.md                  #   报告模板
    │   ├── data/                      #   策略内嵌数据（调仓目标表等）
    │   └── schemas/                   #   输出产物契约
    ├── cs300_opt_1/                   # 沪深300优选策略一
    └── cs300_opt_2/                   # 沪深300优选策略二
```

## 📰 最新动态

| 日期 | 版本 | 重点 |
|---|---|---|
| 2026-11-20 | [v1.0.0](https://github.com/luoy38067/YouBuddy-quant-skills/releases/tag/v1.0.0) | 仓库开张：首批上架 3 个沪深300 系策略 Skill（白马攻防 / 优选策略一 / 优选策略二），YouBuddy 安装包同步发布至 Releases |

## 反馈与更新

欢迎通过 [Issues](https://github.com/luoy38067/YouBuddy-quant-skills/issues) 反馈使用问题与策略需求；更多类型的策略（ETF 轮动、全天候配置、红利杠铃等）将持续上架，欢迎 Star 关注。

## License

本项目基于 [MIT License](LICENSE) 开源。
