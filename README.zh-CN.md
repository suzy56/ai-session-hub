<p align="right">
   <a href="./README.md">English</a> | <strong>简体中文</strong>
</p>

<div align="center">
  <h1>AI Session Hub</h1>
  <p><strong>一站式本地终端工作台：聚合、检索、分析并恢复您所有的 AI 编程对话。</strong></p>
  <p><em>跨越各大 AI 编程助手，浏览历史会话、掌握 Token 用量趋势，一键无缝恢复至原工具环境。</em></p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.13%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.13+" />
    <img src="https://img.shields.io/badge/UI-Textual_8.2-10b981?style=flat-square" alt="Textual 8.2" />
    <img src="https://img.shields.io/badge/Search-SQLite_FTS5_Trigram-003B57?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite FTS5 Trigram" />
    <img src="https://img.shields.io/badge/Privacy-100%25_本地离线-22c55e?style=flat-square" alt="100% 本地离线" />
    <img src="https://img.shields.io/badge/Platform-macOS_%7C_Linux-0A84FF?style=flat-square" alt="支持平台" />
    <img src="https://img.shields.io/badge/License-MIT-purple?style=flat-square" alt="MIT 许可证" />
  </p>
</div>

```text
┌─ Session Hub ────────────────────────────────────────────────────────────────────────────────────────┐
│ 会话 [F2]       项目 [F3]       概览 [F1]                                筛选 [f]      设置 [,]      │
│ 范围: 全部工具 · 全部时间                                                                            │
├──────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 时间         工具    标题                                   │ 对话                                   │
│ 09-21 14:32  codex   重构数据库连接池与事务管理             │ 助手 -                                 │
│ 09-21 11:15  omp     实现后台增量索引扫描 Worker            │ 为在不阻塞 UI 的前提下优化查询延迟，   │
│ 09-20 18:40  claude  修复流式响应断流与重试逻辑             │ 我们通过独立的后台工作线程与只读事务   │
│ 09-19 09:20  hermes  添加上下文压缩分支续接处理             │ 快照进行增量解析……                     │
│ 09-18 16:05  dsh     解析压缩流式用量事件日志               │                                        │
├──────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 1–5 / 1,388 个会话 · 5.56B Token · $1,602.70 估算           │ 详情 [i]    继续会话 [r]    帮助 [?]   │
└──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## AI Session Hub 是什么？

**AI Session Hub** 是一款为多 AI 编程工具开发者打造的开源键盘优先终端界面（TUI）。

如今开发者往往同时使用多种 AI 编程助手（如 Codex、Claude Code、Hermes、OMP、DSH 等），会话记录分散在各自的配置目录、SQLite 数据库或自定义压缩日志中，历史难以统一翻阅。AI Session Hub 能够在本地实时读取各工具的会话转录与用量事实，建立独立的 SQLite FTS5 高性能检索索引，提供媲美 **Token Monitor** 的多维度用量看板，并支持快捷键一键安全恢复至原生工具终端。

**100% 本地离线与隐私安全**：所有的会话解析、Token 统计与全文索引均完全在您的本地机器上执行，使用只读数据库连接，绝不读取源码密钥、绝不上报遥测数据、绝不向任何外部模型接口发送提示词。

---

## 支持的工具与能力矩阵

AI Session Hub 深度适配各大主流 AI 编程工具的原生转录格式与命令行接口：

| 工具 | 本地数据路径 | 全文检索 | 用量与费用 | 原生恢复 | 适配特性 |
|:-----|:-------------|:--------:|:----------:|:--------:|:---------|
| **Codex** | `~/.codex/sessions/`、`state_*.sqlite` | ✅ | ✅ | ✅ | 支持全量历史与分页数据库投影去重 |
| **Claude Code** | `~/.claude/projects/`、`transcripts/` | ✅ | ✅ | ✅ | 支持原生 UUID 恢复与子代理会话识别 |
| **Hermes Agent** | `~/.hermes/state.db` | ✅ | ✅ | ✅ | 智能追踪上下文压缩末梢与 Profile 环境 |
| **Oh My Pi (OMP)** | `~/.omp/agent/sessions/` | ✅ | ✅ | ✅ | 支持原生 JSONL 会话与多 Profile 隔离 |
| **DeepSeek Harness (DSH)** | `~/.dsh/sessions/` | ✅ | ✅ | ✅ | 流式解压 `.jsonl.zstd`，支持长会话恢复 |
| **Cursor IDE** | `~/Library/Application Support/Cursor/` | 仅元数据 | — | — | 自动发现本地工作区与会话元数据 |

---

## 核心特性

### 📊 Token Monitor 风格的用量看板
* **8 大核心指标卡片**：总 Token（附带覆盖率）、已知费用（区分原生记录与本地估算）、总活跃天数、连续活跃天数（Streak）、单日峰值用量及日期、最常用模型及占比、消息总量、会话总量。
* **贡献日历热力图（Calendar Heatmap）**：还原 GitHub / Token Monitor 风格的 7 行周历热力图，通过深浅色块直观展现过去一整年的每日活跃度。
* **双列模型与工具占比拆解**：
  * **按模型**：模型名称、字符进度条（`████████░░`）、Token 数、百分比份额、费用。
  * **按工具**：客户端工具名称、字符进度条、Token 数、份额、会话数。
* **高分辨率竖向用量趋势图**：8 行高度多行柱状图，带有 Token 纵刻度（`316.6M ┤`）与日期横坐标，取代传统单行扁平 sparkline。
* **每日活动明细表**：按天倒序展示日期、Token、趋势条、费用及全期占比。

### 🔍 毫秒级字面全文搜索
* **Unicode NFKC 字面精确匹配**：无论是在用户提问还是助手回复中，均可进行大小写不敏感的高速子串检索。
* **SQLite FTS5 Trigram 索引加速**：万级对话秒级召回；缺少 FTS5 支持时自动降级为平滑的字面扫描，确保搜索永不失效。
* **零分词损失**：完美支持中文、日文、韩文、英文长短句、代码符号、标点及 SQL 片段，无需分词切词，输入即所得。

### 🚀 安全、防误触的原生工具恢复
* **严格前置校验**：启动前校验原可执行文件在 `PATH` 中的有效性、绝对工作目录是否存在、源文件完整性及 Profile 隔离配置。
* **二次确认弹窗**：清晰展示即将执行的完整命令行参数、运行目录及 Profile，默认聚焦“取消”按钮，防止误回车启动。
* **优雅终端挂起**：TUI 干净交出终端控制权（恢复 Cooked 模式），等待子进程退出后自动重载界面并刷新索引，无缝衔接。
* **杜绝错误启动**：对于子任务归档、压缩中间态或目录丢失的会话，给出明确原因并拒绝启动，绝不盲目新建无关会话。

### 📁 规整的项目浏览器
* **固定列宽制表对齐**：项目名称、Token 数、费用、会话数、占比在等宽终端下严格垂直对齐，完美支持中英文混排。
* **原地树状展开**：直接在项目节点下展开查看该项目所含的具体会话，无需在视图间跳跃。
* **无限分页加载**：单项目超过 200 个会话时支持平滑的 `加载更多…`，无截断风险。
* **波浪号路径缩写**：自动将用户主目录缩写为 `~/`，杜绝路径溢出换行。

### 🌐 极简终端美学与双语支持
* **OMP 启发设计**：低饱和中性深色背景、极细边框、单一强调色，杜绝高饱和光效污染。
* **中英无缝切换**：按 `,` 呼出设置，即时切换简体中文与 English，原子化写入 `ui-preferences.json`，重启永久保留。
* **自适应响应式布局**：在标准（140x45）下呈左右分栏，在紧凑（80x24）终端下自动转为列表/正文全屏阅读切换。

---

## 安装与快速开始

### 环境依赖
* Python **3.13** 或更高版本
* macOS 或 Linux 终端环境（推荐 Ghostty、iTerm2、Alacritty、Kitty、WezTerm 等现代终端）

### 快速安装

```bash
# 1. 克隆代码仓库
git clone https://github.com/suzy56/ai-session-hub.git
cd ai-session-hub

# 2. 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖（可编辑模式）
pip install -e .

# 4. 运行 AI Session Hub
ai-session-hub
```

亦可无需安装直接启动：
```bash
.venv/bin/python -m ai_session_hub
```

---

## 配置说明

默认情况下，AI Session Hub 会自动扫描本机的默认工具目录。若需添加自定义目录或配置本地价格规则，可通过 `--config` 指定：

```bash
ai-session-hub --config /path/to/config.toml --data-dir ~/.local/share/ai-session-hub
```

### `config.toml` 配置示例

```toml
# 设为 false 可关闭默认扫描，仅探测下方明确列出的数据源
discover_defaults = true

# 自定义 OMP profile 目录
[[sources]]
tool = "omp"
root = "/Users/username/.omp/profiles/work/agent"

# 自定义 Codex 目录
[[sources]]
tool = "codex"
root = "/Users/username/.codex"
profile = "default"

# 可选：自定义模型单价（美元 / 百万 Token）
# 当某些原生会话缺少费用字段时，用于本地估算用量成本
[[pricing]]
model = "gpt-5.5"
input_per_million = 2.0
output_per_million = 8.0
cache_read_per_million = 0.5
cache_write_per_million = 2.5

[[pricing]]
model = "claude-3-7-sonnet"
provider = "anthropic"
input_per_million = 3.0
output_per_million = 15.0
cache_read_per_million = 0.3
cache_write_per_million = 3.75
```

---

## 快捷键指南

| 按键 | 功能说明 | 作用域 |
|:---:|:---------|:-------|
| `F1` | 切换到 **概览看板（Dashboard）** | 全局 |
| `F2` | 切换到 **会话列表（Sessions）** | 全局 |
| `F3` | 切换到 **项目浏览（Projects）** | 全局 |
| `/` | 聚焦搜索输入框 | 会话视图 |
| `f` | 呼出 **多维筛选（Filters）** 弹窗（工具/项目/时间/类型） | 全局 |
| `,` | 呼出 **系统设置（Settings）** 弹窗（中英切换） | 全局 |
| `r` | 请求 **恢复原生会话（Resume）** | 表格 / 树 |
| `i` | 弹出选中会话的 **完整详情与用量构成** | 表格 / 树 |
| `Enter` | 阅读所选对话 / 展开项目文件夹 | 表格 / 树 |
| `[` / `]` | 上一页 / 下一页（会话列表）或 切换总览 / 趋势（概览页） | 视图内 |
| `PageUp` / `PageDown` | 滚动长对话预览内容 | 预览区 |
| `Esc` | 关闭当前弹窗 / 从阅读返回表格 | 全局 |
| `Ctrl+R` | 手动触发增量索引刷新 | 全局 |
| `q` | 退出程序（非打字状态下） | 全局 |
| `Ctrl+Q` | 强制退出程序 | 全局 |
| `?` | 上下文操作帮助 | 全局 |

---

## 系统架构与安全原则

```text
┌────────────────────────────────────────────────────────┐
│                   本地 AI 编程工具                     │
│   Codex  ·  Claude  ·  Hermes  ·  OMP  ·  DSH  ·  ...  │
└──────────────────────────┬─────────────────────────────┘
                           │ 只读 WAL / JSONL 文件流
                           ▼
┌────────────────────────────────────────────────────────┐
│                   ai-session-hub                       │
│  ┌────────────────────┐      ┌──────────────────────┐  │
│  │ 增量索引收集器     │ ───▶ │ SQLite FTS5 本地存储 │  │
│  └────────────────────┘      └──────────────────────┘  │
│            │                             │             │
│            ▼                             ▼             │
│  ┌────────────────────┐      ┌──────────────────────┐  │
│  │ 原生会话分发器     │      │ Textual TUI 交互引擎 │  │
│  └────────────────────┘      └──────────────────────┘  │
└────────────┬─────────────────────────────┬─────────────┘
             │ PTY 终端接管                │ 交互渲染
             ▼                             ▼
   [原生 CLI 会话终端]              [终端物理显示屏]
```

* **只读原则**：以 SQLite URI `?mode=ro` 与 `PRAGMA query_only=ON` 方式打开源数据库；JSONL 与 `.zstd` 流均采用只读流式缓冲区。
* **数据隔离**：应用自用索引存储于 `~/Library/Application Support/ai-session-hub/`（macOS）或 `~/.local/share/ai-session-hub/`（Linux），目录权限严格设为当前用户私有（`0700`），绝不向任何代码仓库内写入私有数据。
* **安全的子进程生命周期**：原生工具唤起使用标准 `subprocess.Popen`，绝不引入 `shell=True`，彻底杜绝命令注入风险。

---

## 运行自动化测试

项目内置 86 项自动化单元与集成测试，覆盖了转录数据提取、FTS 搜索、Unicode 规范化、项目用量聚合、PTY 终端接管生命周期以及响应式 UI 交互：

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -q
```

---

## 开源许可证

本项目基于 [MIT 许可证](LICENSE) 开源。
