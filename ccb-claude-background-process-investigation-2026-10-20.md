# CCB 关闭窗口后 Claude 仍在后台运行：排查记录

- 记录日期：2026-10-20。
- 排查项目：`~/Desktop/person/dao-fu`。
- CCB 安装位置：`~/.local/share/codex-dual`。
- 安装版本：`8.7.12`，构建提交 `3a2b777`，stable release，macOS。
- 本文是本次排查的历史记录；进程号、连接和生命周期状态仅代表对应检查时刻，不代表以后一直保持该状态。

## 一、问题与最终结论

用户已经关闭 CCB 的可见窗口，没有主动打开 Claude，但代理日志仍出现 `claude.exe` 访问 `api.anthropic.com:443` 的记录。

排查确认，本地 CCB 将“关闭前台窗口”和“停止项目后台运行时”视为两种不同操作：

1. CCB 在后台创建并保留项目专属 tmux 会话。
2. 关闭窗口只断开前台连接，不自动停止 tmux 内的代理，也不记录项目停止意图。
3. ccbd 持续监督配置中的代理；单独终止 Claude 进程，会被当作运行故障并触发恢复。
4. 用户随后执行项目级 `ccb kill`。复查确认 Claude、项目控制进程及 tmux 会话均已停止，生命周期已切换为停止状态。

这是当前安装版本明确实现的后台常驻与自动恢复行为；本次未证明存在退出清理代码执行失败。如果产品预期是“关闭最后一个窗口即停止所有代理”，当前行为与该预期不一致。

## 二、最初日志能证明什么

截图中有三条记录：18:34:09、18:39:09、18:44:09，恰好间隔五分钟。

可确认：

- 代理将这些连接归属为 `claude.exe`。
- 目标标记为 `api.anthropic.com:443`。
- `match RuleSet(Claude AI)` 表示命中代理分流规则，不代表已经执行模型推理。

不能仅凭截图确认：

- 请求是心跳、额度检查、遥测还是模型调用。
- 请求是否成功、发送了什么内容、是否产生费用。
- 每五分钟连接的具体实现来源。

本次没有解密 TLS 流量或获取请求路径，因此这些用途保持未确定，不能将周期性连接直接定性为遥测或扣费。

## 三、本地进程与网络证据

初次检查发现：

| 项目 | 观察结果 |
| --- | --- |
| Claude PID | `7228` |
| 父进程 | PID `6534`，项目专属 tmux 服务 |
| 启动时间 | 进程列表显示 `2026-10-20 02:34:48`，当时已运行约 16 小时 |
| tmux 会话 | `ccb-dao-fu-0541fef5` |
| 代理身份 | `CCB_CALLER_ACTOR=lead` |
| 前台连接数 | `attached_clients=0` |
| 工作目录 | `~/Desktop/person/dao-fu` |
| 活跃连接 | `198.18.0.1:54857 → 198.18.2.13:443`，`ESTABLISHED` |

本地活跃连接的目标为 IP 地址；仅凭这条 socket 记录，不能独立确认它与截图中的域名连接是同一条请求。

可执行文件：

```text
~/.local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe
```

`file` 命令确认其格式为：

```text
Mach-O 64-bit executable arm64
```

因此，文件名以 `.exe` 结尾并不意味着它一定是 Windows 程序。本次初步回复中由后缀推断 Windows 的判断，已通过本地文件检查纠正。

## 四、源码逻辑

以下路径均相对于本地安装目录 `~/.local/share/codex-dual/`。行号对应本次检查的安装版本。

### 4.1 主动创建和保留后台 tmux

`lib/ccbd/services/project_namespace_runtime/backend.py`：

- `:152–163`：准备 tmux 服务时，在同一个命令队列中启动服务并设置 `exit-empty off`。
- `:174–184`：显式设置 `destroy-unattached off`、`exit-empty off`。
- `:308–316`：通过 `new-session -d` 创建后台会话。

运行时还查到 `exit-unattached off`。其中 `destroy-unattached off` 和 `exit-empty off` 已在上述源码中找到显式设置；本次没有把 `exit-unattached off` 归因于该源码片段。

结论：窗口全部断开后继续保留后台，不只是偶然继承用户 tmux 配置。

### 4.2 前台断开后直接返回，不调用项目停止

`lib/cli/services/start_foreground.py:65–87`：

- 启动 `tmux attach-session` 连接已有项目会话。
- 等待 attach 子进程结束。
- 如果此前成功连接，直接返回摘要；该成功连接后的返回路径没有调用项目停止逻辑。

`lib/cli/phase2_runtime/handlers_start.py:110–112`：调用前台 attach 后直接返回。

因此，关闭终端窗口并不等价于调用 `ccb kill`。

### 4.3 后台监督会恢复死亡的代理

`lib/ccbd/app_runtime/lifecycle.py:1349–1359`：心跳包含健康检查与 `runtime_supervision.reconcile_once`，在生命周期进入停止状态时中断后续步骤。

`lib/ccbd/supervision/loop.py:49–72`：

- 未暂停监督时遍历配置中的代理。
- 按运行时状态执行挂载或恢复。
- 该入口不以前台窗口连接数为恢复前提。

`lib/ccbd/supervision/recovery_transitions.py:44–59`：恢复路径会调用 `refresh_provider_binding(agent_name, recover=True)`，必要时重新挂载项目。

`lib/ccbd/app_runtime/bootstrap.py:145–146`：监督暂停条件绑定到生命周期是否正在停止，而不是有没有窗口连接。

### 4.4 正式停止先写停止意图，再清理进程

`lib/cli/services/kill_runtime/remote.py:18–36`：

1. 连接现有后台，不允许为停止操作重启过期后台。
2. 记录 `shutdown_intent`，原因是 `kill`。
3. 发出 `stop_all` 请求。

`lib/cli/services/kill.py:70–95`：停止维护心跳、请求远端停止、准备本地清理、销毁项目 namespace、等待/清理控制进程并完成收尾。

停止意图是关键区别：不能只终止 Claude，而让后台继续认为项目应该运行。

## 五、实际处置过程与自动恢复证据

### 5.1 首次仅终止 Claude

用户要求终止后，助手重新核对 PID `7228` 的身份，执行：

```bash
kill -TERM 7228
```

短暂等待后，当次检查显示：

- PID `7228` 已退出。
- 未发现其他 Claude 进程。
- 对应 tmux 窗格为 `dead=1`。
- 其他 CCB 代理及项目后台未停止。

这只证明当时进程已退出，不证明它不会被监督程序重新拉起。

### 5.2 后续检查发现恢复

继续排查时，先后观察到同一项目 lead 窗格再次运行 Claude，PID 为 `86501`、`90920`。这些是分时观察值，不代表两个 Claude 同时运行。

当时 `.ccb/ccbd/lifecycle.json` 仍为：

```json
{
  "desired_state": "running",
  "phase": "mounted",
  "shutdown_intent": null
}
```

`.ccb/ccbd/supervision.jsonl` 记录了 lead 的恢复链：

```text
recover_started    prior_health=pane-dead
recover_probing
recover_succeeded  result_health=healthy
```

后续还观察到 `pane-missing` 对应的恢复尝试记录。源码与事件日志共同证明：单杀 PID 没有改变项目运行意图，后台确实发生了代理恢复。

因此，首次“已终止”的报告只覆盖当次进程退出，不是持久停止；后续已经向用户说明仅杀 PID 不够彻底。

## 六、正确停止方式与最终复查

已实际运行 `ccb kill --help`，帮助输出确认：

```text
ccb kill     Stop the current project's ccbd, agents, and tmux namespace.
ccb kill -f  Force cleanup project-owned runtime residue before `ccb -n`.
```

用户在 `~/Desktop/person/dao-fu` 项目目录执行：

```bash
ccb kill
```

助手随后只读复查，结果为：

| 检查项 | 停止后的结果 |
| --- | --- |
| 本机 Claude 进程 | 未发现 `claude` / `claude.exe` |
| 该项目控制进程 | 未发现匹配该项目的 ccbd、keeper 或 tmux 进程 |
| 项目 tmux | 连接失败，项目 socket 已不存在 |
| 生命周期 `desired_state` | `stopped` |
| 生命周期 `phase` | `unmounted` |
| 生命周期 `shutdown_intent` | `stop_all` |
| 生命周期 `owner_pid` | `null` |
| 租约 `mount_state` | `unmounted` |

生命周期和租约文件里仍可能保留旧 keeper/ccbd PID；记录中出现历史 PID，不等于操作系统里对应进程仍然存活。

结论：最后一次复查时，项目级停止已经生效，不只是 Claude 单个进程退出。该结论不保证以后用户重新启动 CCB 后仍保持停止。

## 七、可复用的排查要点

- 区分“关闭界面 / detach”“结束代理进程”“停止项目运行意图”三个层级。
- 对有 supervisor 的程序，只杀子进程可能触发自动恢复；需要通过所属管理程序停止。
- 判断是否真正停止，应同时检查实际进程、tmux 会话以及生命周期停止意图，不能只看窗口或记录中的 PID。
- 代理连接日志证明的是连接归属和目标信息，不能单独证明模型调用、提示词传输或费用。
- 若以后在会话管理界面提供关闭操作，应明确区分“断开窗口”和“停止项目”。这是由本次问题归纳出的交互要求，不表示本仓库已经实现相关功能。

## 八、本次操作范围

- 在用户授权后仅直接终止过 Claude PID `7228`。
- 项目级 `ccb kill` 由用户执行，助手负责随后验证。
- 未修改 CCB 的实现、tmux 策略、自动恢复配置或遥测设置。
- 未清理 Claude 账号、凭据、会话历史或指纹信息。
- 本文只记录调查证据与操作结果，不包含 API Key、Token 或请求正文。
