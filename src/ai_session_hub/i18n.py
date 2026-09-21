from __future__ import annotations

import json
import locale
import os
import tempfile
from pathlib import Path


_LANGUAGES = ("zh-CN", "en")
_PREFERENCES_FILE = "ui-preferences.json"

# Keys are English source templates; inserted source data is never translated.
_ZH_CN = {
    "Session Hub": "Session Hub",
    "Sessions": "会话",
    "Projects": "项目",
    "Dashboard": "概览",
    "Settings": "设置",
    "Language": "语言",
    "Filters": "筛选",
    "Search": "搜索",
    "Search messages or title (/ to focus)": "搜索消息或标题（/ 聚焦）",
    "Search projects": "搜索项目",
    "Reset filters": "重置筛选",
    "Apply": "应用",
    "Save": "保存",
    "Close": "关闭",
    "Back": "返回",
    "Back/Table": "返回列表",
    "Resume": "恢复",
    "Refresh": "刷新",
    "Quit": "退出",
    "Scroll Up": "向上滚动",
    "Scroll Down": "向下滚动",
    "Prev Page": "上一页",
    "Next Page": "下一页",
    "Load more": "加载更多",
    "Confirm": "确认",
    "Cancel": "取消",
    "Cancel (Esc)": "取消（Esc）",
    "Resume (Enter)": "恢复（Enter）",
    "Resume session": "恢复会话",
    "Confirm resume": "确认恢复",
    "The native tool will take over this terminal. Exit it to return here.": "原工具将接管当前终端，退出后返回此界面。",
    "All Platforms": "全部平台",
    "All Projects": "全部项目",
    "All Time": "全部时间",
    "All Kinds": "全部类别",
    "Last 24 hours": "最近 24 小时",
    "Last 7 days": "最近 7 天",
    "Last 30 days": "最近 30 天",
    "Platform": "平台",
    "Tool": "工具",
    "Title": "标题",
    "Project": "项目",
    "Time": "时间",
    "Kind": "类别",
    "Status": "状态",
    "Date": "日期",
    "Trend": "趋势",
    "Share": "占比",
    "Tokens": "Token",
    "Cost": "费用",
    "Models": "模型",
    "Model / Provider": "模型 / Provider",
    "Messages": "消息",
    "Total tokens": "总 Token",
    "Known cost": "已知费用",
    "Total sessions": "会话总数",
    "Total messages": "消息总数",
    "Usage coverage": "用量覆盖",
    "Pricing coverage": "定价覆盖",
    "Unknown usage": "未知用量",
    "Active days": "活跃天数",
    "Consecutive days": "连续天数",
    "Peak single day": "峰值单日",
    "Top model": "常用模型",
    "Consecutive active": "连续活跃",
    "Total active days": "总活动日",
    "Overview": "总览",
    "Token Activity": "Token 活动",
    "Tool breakdown": "按工具",
    "No usage data": "无用量数据",
    "Bar": "占比条",
    "Model breakdown": "模型用量",
    "Daily activity": "每日活动",
    "Usage trend": "用量趋势",
    "Conversation": "对话",
    "Details": "详情",
    "Metadata": "元数据",
    "Usage": "用量",
    "Source": "来源",
    "Source root": "来源根目录",
    "Native ID": "原生 ID",
    "Working directory": "工作目录",
    "Profile": "配置",
    "Default": "默认",
    "Started": "开始时间",
    "Last activity": "最后活动",
    "Input (including cache)": "输入（含缓存）",
    "Output (including reasoning)": "输出（含推理）",
    "Cache read": "缓存读取",
    "Cache write": "缓存写入",
    "Reasoning subset": "推理子集",
    "Warnings": "警告",
    "User": "用户",
    "Assistant": "助手",
    "Unknown": "未知",
    "Unknown project": "未知项目",
    "Estimated": "估算",
    "Recorded": "记录",
    "Partial": "部分",
    "Native": "原生",
    "Cached": "缓存",
    "Metadata only": "仅元数据",
    "Unavailable": "不可用",
    "Archived": "已归档",
    "Subagent": "子代理",
    "Advisor": "顾问",
    "Background review": "后台审查",
    "IDE": "IDE",
    "Body match": "正文匹配",
    "Title/project match": "标题/项目匹配",
    "Ready": "就绪",
    "Loading…": "加载中…",
    "Scanning…": "扫描中…",
    "Scan complete": "扫描完成",
    "Scan cancelled": "扫描已取消",
    "No sessions found": "未找到匹配的会话",
    "No projects found": "未找到匹配的项目",
    "No native usage yet": "尚无原生用量",
    "Session detail unavailable": "会话详情不可用",
    "No transcript messages; metadata only": "无转录消息，仅有元数据",
    "Select a session first": "请先选择会话",
    "Session data not found": "找不到会话数据",
    "Cannot resume session": "无法恢复会话",
    "Resume target changed; resume cancelled": "会话恢复目标发生变更，已取消恢复",
    "Query failed": "查询失败",
    "Analytics failed: {error}": "统计失败：{error}",
    "Indexing failed: {error}": "索引失败：{error}",
    "Terminal suspension failed: {error}": "终端挂起失败：{error}",
    "Launch failed: {error}": "执行失败：{error}",
    "Tool exited with status {code}": "工具退出状态码：{code}",
    "Preferences warning": "偏好设置警告",
    "Could not save preferences": "无法保存偏好设置",
    "Language saved": "语言已保存",
    "Missing executable": "缺少可执行程序",
    "Missing source": "来源不存在",
    "Missing working directory": "工作目录不存在",
    "Unknown working directory": "工作目录未知",
    "Unsupported profile": "不支持此配置",
    "Unsupported resume": "不支持恢复",
    "Unverified compatibility": "兼容性尚未验证",
    "Ambiguous identity": "会话身份不明确",
    "Secondary session": "附属会话",
    "Invalid identity": "会话身份无效",
    "Search acceleration unavailable": "搜索索引加速不可用",
    "Last {count} days · peak {peak:,}": "最近 {count} 天 · 峰值 {peak:,}",
    "Coverage: {known:,}/{total:,} records": "覆盖：{known:,}/{total:,} 条记录",
    "Priced: {known:,}/{total:,} records": "定价：{known:,}/{total:,} 条记录",
    "Unknown usage: {count:,} sessions": "未知用量：{count:,} 个会话",
    "Active: {count:,} days": "活跃：{count:,} 天",
    "{count:,} active days · Up/Down to browse": "共 {count:,} 个活跃日 · 上下键浏览",
    "{count:,} projects · Enter to expand · Up/Down to select": "共 {count:,} 个项目 · Enter 展开 · 上下键选择",
    "{count:,} sessions": "{count:,} 个会话",
    "Session activity filter · lifetime tokens/cost": "按会话活动时间筛选 · Token/费用为会话全部历史",
    "Usage-event time filter": "按用量事件时间筛选",
    "Token coverage: {tokens}/{records}; cost coverage: {priced}/{records} usage records": "Token 覆盖：{tokens}/{records}；费用覆盖：{priced}/{records} 条用量记录",
    "All tools": "全部工具",
    "All projects": "全部项目",
    "All time": "全部时间",
    "All kinds": "全部类别",
    "Reset": "重置",
    "Find a name or path…": "搜索名称或路径…",
    "No matches": "没有匹配项",
    "Interface language": "界面语言",
    "Applies immediately and is remembered on restart.": "应用后立即生效，下次启动仍然保留。",
    "Resume session?": "继续此会话？",
    "The native CLI takes over this terminal. Exit it to return here.": "原工具将接管当前终端；退出后返回此处。",
    "Settings warning": "设置警告",
    "Source warnings": "来源警告",
    "Search acceleration unavailable; literal search remains available.": "搜索索引加速不可用；仍可使用字面搜索。",
    "Could not save settings": "无法保存设置",
    "Usage time · totals include unknown coverage": "按用量时间统计 · 总数包含覆盖未知的会话",
    "Activity time · session usage is lifetime": "按活动时间筛选 · 会话用量为全部历史",
    "Esc back   i details   r resume   Ctrl+C copy selection": "Esc 返回   i 详情   r 继续会话   Ctrl+C 复制选中内容",
    "Enter open   f filters   r resume   , settings   ? help": "Enter 打开   f 筛选   r 继续会话   , 设置   ? 帮助",
    "Tab next panel   f filters   F2 sessions   , settings   ? help": "Tab 切换面板   f 筛选   F2 会话   , 设置   ? 帮助",
    "/ search   f filters   Enter read   r resume   , settings   ? help": "/ 搜索   f 筛选   Enter 阅读   r 继续会话   , 设置   ? 帮助",
    "Keyboard help": "键盘帮助",
    "F1 dashboard · F2 sessions · F3 projects\n/ search · f filters · , settings\nUp/Down select · Enter read or expand\nEsc close overlay or return to list\n[ previous page · ] next page\ni details · r confirm native resume\nCtrl+R refresh · q quit · Ctrl+Q always quit\nSelect text with mouse or Shift+arrows; Ctrl+C copies.\nConversation text, model names and paths are never translated.": "F1 概览 · F2 会话 · F3 项目\n/ 搜索 · f 筛选 · , 设置\n上下键选择 · Enter 阅读或展开\nEsc 关闭弹窗或返回列表\n[ 上一页 · ] 下一页\ni 详情 · r 确认后进入原工具\nCtrl+R 刷新 · q 退出 · Ctrl+Q 始终退出\n鼠标拖选或 Shift+方向键选择文字；Ctrl+C 复制。\n会话原文、模型名和路径不会被翻译。",
    "{start}–{end} / {total}  ·  [ ] pages  ·  Activity time; lifetime usage": "{start}–{end} / {total}  ·  [ ] 翻页  ·  活动时间筛选；完整会话用量",
    "No matching conversations. Adjust the search or filters.": "没有匹配的会话，请调整搜索或筛选条件。",
    "Select a conversation in the project tree.": "在项目树中选择一条会话。",
    "No transcript available. Metadata only.": "没有可用的对话原文，仅有元数据。",
    "Token coverage": "Token 覆盖",
    "Cost coverage": "费用覆盖",
    "Coverage {known}/{total}": "覆盖 {known}/{total}",
    "Priced {known}/{total}": "已定价 {known}/{total}",
    "Unknown usage: {count}": "未知用量：{count}",
    "{count} active days": "{count} 个活跃日",
    "Unknown model": "未知模型",
    "Unknown provider": "未知提供商",
    "Usage time · costs may be estimated/partial · undated tokens: {tokens}": "按用量时间统计 · 费用可能为估算或部分覆盖 · 无日期 Token：{tokens}",
    "{count} stored sessions": "{count} 个已收录会话",
    "{count} projects · session list filtered by activity time": "{count} 个项目 · 会话列表按活动时间筛选",
    "Load more… ({loaded}/{total})": "加载更多…（{loaded}/{total}）",
    "No matching conversations.": "没有匹配的会话。",
    "Retry loading": "重试加载",
    "Indexing {tool} · {count} sessions": "正在索引 {tool} · {count} 个会话",
    "No sources configured. Add local source roots in config.toml.": "尚未配置来源，请在 config.toml 中添加本地来源目录。",
    "Index ready · {count} sessions scanned": "索引就绪 · 已扫描 {count} 个会话",
    "{count} source errors": "{count} 个来源错误",
    "Native CLI executable not found. Check installation and PATH.": "未找到原生 CLI，请检查安装及 PATH。",
    "The native source is missing or has moved.": "原生来源不存在或已移动。",
    "The recorded working directory no longer exists.": "会话记录的工作目录已不存在。",
    "No verified working directory. Refusing to guess.": "没有经过验证的工作目录，拒绝推测。",
    "The profile does not match the selected native store.": "配置与选中的原生存储不匹配。",
    "This session cannot be resumed in a native CLI.": "此会话不能通过原生 CLI 恢复。",
    "The native environment is not initialized or verified.": "原生环境尚未初始化或验证。",
    "Multiple sources match this identity. Resume is disabled.": "多个来源匹配此身份，已禁用恢复。",
    "Unarchive this session in its original tool first.": "请先在原工具中取消归档。",
    "Secondary sessions cannot be resumed as top-level conversations.": "附属会话不能作为顶级对话恢复。",
    "Invalid native session identity.": "原生会话身份无效。",
    "Only metadata is available; no native transcript.": "仅有元数据，没有原生对话记录。",
    "Cannot resume this session.": "无法恢复此会话。",
    "Select a conversation first.": "请先选择一条会话。",
    "Conversation unavailable.": "会话不可用。",
    "Cannot resume": "无法恢复",
    "Resume target changed. Please review and confirm again.": "恢复目标发生变化，请重新检查并确认。",
    "Terminal suspension failed": "终端挂起失败",
    "Launch failed": "启动失败",
    "Native CLI exited with status {code}": "原工具退出状态码：{code}",
    "Resume target": "恢复目标",
}


def _validate_language(language: str) -> None:
    if language not in _LANGUAGES:
        raise ValueError(f"Unsupported interface language {language!r}; choose 'zh-CN' or 'en'.")


def tr(language: str, key: str, **values: object) -> str:
    """Translate an English UI template without changing interpolated source data."""
    _validate_language(language)
    template = _ZH_CN.get(key, key) if language == "zh-CN" else key
    return template.format(**values) if values else template


def _detected_language() -> str:
    # Respect message-locale precedence without mutating the process locale.
    detected = next((os.environ[name] for name in ("LC_ALL", "LC_MESSAGES", "LANG") if os.environ.get(name)), None)
    if detected is None:
        try:
            detected = locale.getlocale()[0]
        except (ValueError, locale.Error):
            detected = None
    base = (detected or "").replace("-", "_").split(".", 1)[0].split("_", 1)[0].casefold()
    return "zh-CN" if base in ("zh", "chinese") else "en"


def load_language(data_dir: Path) -> tuple[str, str | None]:
    """Load an explicit choice, or return the detected locale and a raw warning.

    The caller must first validate and create the app-owned data directory.
    A missing preference file is normal; malformed/unreadable preferences are not.
    """
    path = data_dir / _PREFERENCES_FILE
    fallback = _detected_language()
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        if not path.is_symlink():
            return fallback, None
        return fallback, f"Cannot read UI preferences at {path}: {error}"
    except (OSError, UnicodeError) as error:
        return fallback, f"Cannot read UI preferences at {path}: {error}"
    try:
        preferences = json.loads(content)
        if not isinstance(preferences, dict):
            raise ValueError("expected a JSON object with a 'language' field")
        language = preferences.get("language")
        _validate_language(language)
    except (ValueError, TypeError) as error:
        return fallback, f"Invalid UI preferences at {path}: {error}"
    return language, None


def save_language(data_dir: Path, language: str) -> None:
    """Atomically replace preferences in the already-isolated app directory.

    Only the newly created preference file is chmodded. Neither the directory nor
    any existing file/symlink target is modified in place.
    """
    _validate_language(language)
    path = data_dir / _PREFERENCES_FILE
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=data_dir,
                                         prefix=".ui-preferences-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"language": language}, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        raise OSError(f"Cannot save UI preferences at {path}; check the app data directory's permissions and free space: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
