"""
MoviePilot V2 插件: 日志监控转存整理 (LogMonitorTransfer)

功能:
1. 界面选择要监控的已安装插件
2. 定时读取 MP 主日志文件增量
3. 按插件名过滤日志行，匹配关键词
4. 命中后延迟指定分钟，将文件夹加入 MP 整理队列
5. 支持本地存储与 CloudDrive2/115 等网盘存储路径

存储类型说明:
- local: 路径是 MP 容器内的本地路径（也适用于 CloudDrive2 FUSE 挂载到本地目录的情况）
- CloudDrive储存: 路径是 CloudDrive2 的内部路径（如 /115/电影），需安装 clouddrivedisk 插件
- 自定义: 填写任意 storage 名称，用于其他存储插件
"""

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Set, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

# 日志行解析正则
LOG_LINE_RE = re.compile(
    r"^\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}:\d{2}[,.]\d*\s*"
    r"\|\s*\w+\s*\|\s*(\S+)\s*\|\s*(.*)$"
)


class RuleGroup:
    """一组规则: 目标文件夹 + 关键词列表"""
    def __init__(self, target_folder: str, keywords: List[str]):
        self.target_folder = target_folder
        self.keywords = [kw.strip().lower() for kw in keywords if kw.strip()]

    def matches(self, message: str):
        msg_lower = message.lower()
        for kw in self.keywords:
            if kw in msg_lower:
                return kw
        return None


class PluginInfo:
    """已安装插件的信息"""
    def __init__(self, plugin_id: str, plugin_name: str, logger_name: str = ""):
        self.plugin_id = plugin_id
        self.plugin_name = plugin_name
        self.logger_name = logger_name or f"plugins.v2.{plugin_id.lower()}"


# 存储类型选项
STORAGE_ITEMS = [
    {"title": "本地存储 (local)", "value": "local"},
    {"title": "CloudDrive2 (CloudDrive储存)", "value": "CloudDrive储存"},
    {"title": "自定义存储名称", "value": "__custom__"},
]


class LogMonitorTransfer(_PluginBase):
    # 插件名称
    plugin_name = "日志监控转存整理"
    # 插件描述
    plugin_desc = "监控指定插件的日志输出，命中关键词后延迟将文件夹加入 MP 整理队列，支持本地与 CloudDrive2 等网盘路径"
    # 插件图标
    plugin_icon = ""
    # 插件版本
    plugin_version = "1.5"
    # 插件作者
    plugin_author = "WorkBuddy"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "logmonitortransfer_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _notify = True
    _delay_minutes = 5
    _check_interval = 30
    _rule_groups_text = ""
    _monitored_plugins = []
    _storage_type = "local"
    _custom_storage_name = ""

    # 运行时状态
    _log_position = 0
    _log_file_path = ""
    _log_file_inode = 0
    _pending_transfers: Dict[str, datetime] = {}
    _rule_groups: List[RuleGroup] = []
    _installed_plugins: List[PluginInfo] = []
    _selected_logger_names: Set[str] = set()
    _scheduler: Optional[BackgroundScheduler] = None
    _scheduler_lock = Lock()
    _config: Dict[str, Any] = {}

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._config = dict(config)
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._delay_minutes = int(config.get("delay_minutes", 5))
            self._check_interval = int(config.get("check_interval", 30))
            self._rule_groups_text = config.get("rule_groups", "")
            self._monitored_plugins = config.get("monitored_plugins", [])
            if isinstance(self._monitored_plugins, str):
                self._monitored_plugins = [s.strip() for s in self._monitored_plugins.split(",") if s.strip()]
            self._storage_type = config.get("storage_type", "local") or "local"
            self._custom_storage_name = (config.get("custom_storage_name") or "").strip()

            # 规则组: 优先旧版 rule_groups 文本配置, 兼容历史数据; 否则解析结构化字段
            if self._rule_groups_text and self._rule_groups_text.strip():
                self._load_rule_groups(self._rule_groups_text)
                logger.info(f"检测到旧版 rule_groups 文本配置，已解析 {len(self._rule_groups)} 条规则")
            else:
                self._rule_groups = self._parse_structured_rules(config)

            self._build_selected_set(self._monitored_plugins)
        else:
            self._config = {}
            self._enabled = False

        self._load_installed_plugins()
        self._locate_log_file()

        if self._enabled:
            logger.info(f"日志监控已启用 | 监控 {len(self._selected_logger_names)} 个插件 | "
                        f"{len(self._rule_groups)} 组规则 | 延迟 {self._delay_minutes} 分钟 | "
                        f"存储: {self._resolve_storage_name()}")
        else:
            logger.info("日志监控已禁用")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        if not self._installed_plugins:
            self._load_installed_plugins()

        plugin_items = [{"title": p.plugin_name, "value": p.plugin_id} for p in self._installed_plugins]

        # 延迟导入 DirectoryHelper, 抽取 MP 已配置目录作为目标目录下拉选项
        dir_items = []
        seen_paths = set()
        try:
            from app.helper.directory import DirectoryHelper
            for d in DirectoryHelper().get_dirs():
                for path_attr, storage_attr in [("download_path", "storage"), ("library_path", "library_storage")]:
                    p = getattr(d, path_attr, None)
                    s = getattr(d, storage_attr, "") or "local"
                    if p and p not in seen_paths:
                        seen_paths.add(p)
                        dir_items.append({"title": f"[{s}] {p}", "value": p})
        except Exception as e:
            logger.warn(f"获取已配置目录失败: {e}")
            dir_items = []
        if not dir_items:
            dir_items = [{"title": "(无已配置目录，请先在 MP 设置→目录中配置)", "value": ""}]

        # 生成 5 行结构化规则 UI
        rule_rows = []
        for i in range(1, 6):
            rule_rows.append({
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [{
                        "component": "VCombobox",
                        "props": {
                            "model": f"target_folder_{i}",
                            "label": f"规则{i} 目标目录",
                            "items": dir_items,
                            "hint": "留空跳过此规则，可选择 MP 已配置目录或直接输入任意路径",
                            "persistentHint": True,
                            "clearable": True,
                        }
                        }],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [{
                            "component": "VTextField",
                            "props": {
                                "model": f"keywords_{i}",
                                "label": f"规则{i} 关键词（逗号分隔）",
                                "placeholder": "转存完成,下载成功",
                            }
                        }],
                    },
                ],
            })

        return [
            {
                "component": "VForm",
                "content": [
                    # 启用开关 + 通知开关
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 插件选择
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "monitored_plugins",
                                            "label": "监控的插件",
                                            "items": plugin_items,
                                            "multiple": True,
                                            "chips": True,
                                            "hint": "选择要监控日志输出的插件，可多选",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 规则组（结构化，最多 5 组）
                    *rule_rows,
                    # 延迟 + 间隔
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "delay_minutes",
                                            "label": "延迟分钟数",
                                            "type": "number",
                                            "hint": "命中关键词后等待多少分钟再加入整理队列",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "check_interval",
                                            "label": "检查间隔(秒)",
                                            "type": "number",
                                            "hint": "每隔多少秒检查一次日志",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 存储类型 + 自定义名称
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "storage_type",
                                            "label": "存储类型",
                                            "items": STORAGE_ITEMS,
                                            "hint": "目标文件夹所属的存储。本地路径选 local；clouddrivedisk 插件接入选 CloudDrive储存；其他网盘插件选自定义",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "custom_storage_name",
                                            "label": "自定义存储名称",
                                            "hint": "仅当上方选择「自定义存储名称」时生效",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 说明
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
"text": ("最多支持 5 组规则。每个规则选择一个目标目录并填写关键词（英文逗号分隔）。\n"
         "CloudDrive2 用户: 在 MP 设置→目录中配置网盘路径并选对应的存储类型；"
         "若用 FUSE 把 CD2 挂载到本地目录, 路径选本地挂载点, 存储类型选 local;\n"
         "若上方下拉为空, 请先在 MP 设置→目录中配置目标路径。\n"
         "整理方式跟随系统全局设定。\n"
         "CloudDrive2 用户可直接输入网盘内部路径，如 /115/电影（无需在 MP 设置→目录中预先配置）。"),
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "monitored_plugins": [],
            "delay_minutes": 5,
            "check_interval": 30,
            "storage_type": "local",
            "custom_storage_name": "",
            "target_folder_1": "",
            "keywords_1": "",
            "target_folder_2": "",
            "keywords_2": "",
            "target_folder_3": "",
            "keywords_3": "",
            "target_folder_4": "",
            "keywords_4": "",
            "target_folder_5": "",
            "keywords_5": "",
        }

    def get_page(self) -> List[dict]:
        pending_info = []
        for key, trigger_time in self._pending_transfers.items():
            remaining = trigger_time - datetime.now()
            if remaining.total_seconds() > 0:
                kw, folder = key.split("|", 1)
                pending_info.append(
                    f"关键词 [{kw}] -> 文件夹 {folder} | "
                    f"剩余 {remaining.seconds // 60} 分钟 {remaining.seconds % 60} 秒"
                )
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info" if pending_info else "success",
                    "variant": "tonal",
                    "text": "\n".join(pending_info) if pending_info else "暂无待执行的延迟任务",
                },
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [
            {
                "id": "LogMonitorTransfer.Check",
                "name": "日志监控检查",
                "trigger": IntervalTrigger(seconds=int(self._check_interval)),
                "func": self._check_logs,
                "kwargs": {},
            }
        ]

    def stop_service(self):
        try:
            with self._scheduler_lock:
                if self._scheduler:
                    self._scheduler.remove_all_jobs()
                    if self._scheduler.running:
                        self._scheduler.shutdown(wait=False)
                    self._scheduler = None
            self._pending_transfers.clear()
        except Exception as e:
            logger.error(f"停止日志监控服务失败: {e}")

    # ========== 核心逻辑 ==========

    def _resolve_storage_name(self) -> str:
        """根据 storage_type 计算最终传给 manual_transfer 的 storage 字符串"""
        if self._storage_type == "__custom__":
            return self._custom_storage_name or "local"
        return self._storage_type or "local"

    def _check_logs(self):
        if not self._enabled:
            return
        if not self._log_file_path or not os.path.exists(self._log_file_path):
            self._locate_log_file()
            if not self._log_file_path:
                return
        if not self._selected_logger_names and not self._rule_groups:
            return
        try:
            new_position, new_lines = self._read_log_increment()
            if new_position > self._log_position:
                self._log_position = new_position
        except Exception as e:
            logger.error(f"读取日志文件失败: {e}")
            return
        if not new_lines:
            return
        for line in new_lines:
            match = LOG_LINE_RE.match(line)
            if not match:
                continue
            logger_name = match.group(1).strip()
            message = match.group(2).strip()
            if not message:
                continue
            if self._selected_logger_names and logger_name not in self._selected_logger_names:
                continue
            for group in self._rule_groups:
                matched_kw = group.matches(message)
                if matched_kw:
                    self._schedule_delayed_transfer(matched_kw, group.target_folder, self._delay_minutes)

    def _read_log_increment(self):
        file_stat = os.stat(self._log_file_path)
        file_size = file_stat.st_size
        file_inode = file_stat.st_ino
        if file_size < self._log_position or (self._log_file_inode and file_inode != self._log_file_inode):
            self._log_position = 0
            logger.info("检测到日志文件轮转，重置读取位置")
        self._log_file_inode = file_inode
        if file_size <= self._log_position:
            return self._log_position, []
        with open(self._log_file_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(self._log_position)
            new_content = f.read()
        new_position = self._log_position + len(new_content.encode("utf-8"))
        lines = [ln.strip() for ln in new_content.split("\n") if ln.strip()]
        return new_position, lines

    def _schedule_delayed_transfer(self, keyword, folder_path, delay_minutes):
        pending_key = f"{keyword}|{folder_path}"
        if pending_key in self._pending_transfers:
            return

        storage_name = self._resolve_storage_name()
        # 本地存储做存在性检查；网盘存储不做，避免本地文件系统找不到路径而被跳过
        if storage_name == "local" and not os.path.isdir(folder_path):
            logger.warn(f"目标文件夹不存在，跳过: {folder_path}")
            return
        if storage_name != "local":
            # 网盘存储的路径在本地文件系统通常不可见，只打 info 日志
            logger.info(f"网盘存储 [{storage_name}] 目标路径: {folder_path}")

        trigger_time = datetime.now() + timedelta(minutes=delay_minutes)
        self._pending_transfers[pending_key] = trigger_time

        with self._scheduler_lock:
            if not self._scheduler:
                try:
                    self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
                    self._scheduler.start()
                except Exception as e:
                    logger.error(f"初始化调度器失败: {e}")
                    self._pending_transfers.pop(pending_key, None)
                    return

        try:
            from apscheduler.triggers.date import DateTrigger
            job_id = f"transfer_{abs(hash(pending_key)) & 0x7FFFFFFF}"
            self._scheduler.add_job(
                func=self._execute_transfer,
                trigger=DateTrigger(run_date=trigger_time),
                args=[keyword, folder_path, pending_key, storage_name],
                id=job_id,
                replace_existing=True,
            )
            logger.info(f"已调度延迟 {delay_minutes} 分钟整理: [{keyword}] -> {folder_path} (storage={storage_name})")
        except Exception as e:
            logger.error(f"调度延迟任务失败: {e}")
            self._pending_transfers.pop(pending_key, None)

    def _execute_transfer(self, keyword, folder_path, pending_key, storage_name):
        try:
            from app.chain.transfer import TransferChain
            logger.info(f"开始执行整理: [{keyword}] -> {folder_path} (storage={storage_name})")
            TransferChain().manual_transfer(
                storage=storage_name,
                in_path=Path(folder_path),
            )
            logger.info(f"整理已加入队列: {folder_path}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="日志监控转存整理",
                    text=f"已触发整理\n关键词: {keyword}\n文件夹: {folder_path}\n存储: {storage_name}",
                )
        except Exception as e:
            logger.error(f"整理失败 [{folder_path}] (storage={storage_name}): {e}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="日志监控转存整理 - 失败",
                    text=f"整理失败\n文件夹: {folder_path}\n存储: {storage_name}\n错误: {str(e)}",
                )
        finally:
            self._pending_transfers.pop(pending_key, None)

    # ========== 日志文件定位 ==========

    def _locate_log_file(self):
        if self._log_file_path and os.path.exists(self._log_file_path):
            return
        candidates = []
        try:
            from app.core.config import settings
            if hasattr(settings, "LOG_PATH"):
                candidates.append(os.path.join(settings.LOG_PATH, "moviepilot.log"))
            if hasattr(settings, "CONFIG_PATH"):
                candidates.append(os.path.join(settings.CONFIG_PATH, "logs", "moviepilot.log"))
        except Exception:
            pass
        candidates.extend([
            "/config/logs/moviepilot.log",
            "/app/logs/moviepilot.log",
            "/moviepilot/config/logs/moviepilot.log",
        ])
        for path in candidates:
            if os.path.isfile(path):
                self._log_file_path = path
                self._log_position = os.path.getsize(path)
                self._log_file_inode = os.stat(path).st_ino
                logger.info(f"日志文件: {path} (当前大小: {self._log_position} bytes)")
                return
        logger.warn("未找到 MP 主日志文件，请确认日志路径")

    # ========== 已安装插件列表 ==========

    def _load_installed_plugins(self):
        self._installed_plugins = []
        installed = self._get_installed_plugins_safe()
        if installed:
            if isinstance(installed, list):
                self._parse_installed_from_list(installed)
            elif isinstance(installed, dict):
                self._parse_installed_from_dict(installed)
        if not self._installed_plugins:
            self._scan_plugin_directories()
        logger.info(f"已发现 {len(self._installed_plugins)} 个已安装插件")

    def _get_installed_plugins_safe(self):
        key = "UserInstalledPlugins"
        try:
            if hasattr(self, "get_system_config"):
                return self.get_system_config(key)
        except Exception:
            pass
        try:
            from app.helper.systemconfig import SystemConfigHelper
            return SystemConfigHelper().get(key)
        except Exception:
            pass
        return None

    def _parse_installed_from_list(self, installed):
        for item in installed:
            if isinstance(item, str):
                pid = item.strip()
                if pid and pid != self.__class__.__name__:
                    self._installed_plugins.append(PluginInfo(plugin_id=pid, plugin_name=pid))
            elif isinstance(item, dict):
                pid = item.get("id") or item.get("plugin_id") or ""
                name = item.get("name") or item.get("plugin_name") or pid
                if pid and pid != self.__class__.__name__:
                    self._installed_plugins.append(PluginInfo(plugin_id=pid, plugin_name=name))

    def _parse_installed_from_dict(self, installed):
        for pid, info in installed.items():
            if pid == self.__class__.__name__:
                continue
            if isinstance(info, dict):
                name = info.get("name") or info.get("plugin_name") or pid
            else:
                name = str(info) if info else pid
            self._installed_plugins.append(PluginInfo(plugin_id=pid, plugin_name=name))

    def _scan_plugin_directories(self):
        try:
            search_dirs = []
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            search_dirs.append(base_dir)
            try:
                from app.core.config import settings
                if hasattr(settings, "PLUGIN_PATH"):
                    search_dirs.append(settings.PLUGIN_PATH)
                if hasattr(settings, "CONFIG_PATH"):
                    search_dirs.append(os.path.join(settings.CONFIG_PATH, "plugins"))
                    search_dirs.append(os.path.join(settings.CONFIG_PATH, "plugins.v2"))
            except Exception:
                pass
            for search_dir in search_dirs:
                if not os.path.isdir(search_dir):
                    continue
                for entry in os.listdir(search_dir):
                    entry_path = os.path.join(search_dir, entry)
                    if os.path.isdir(entry_path) and entry != "logmonitortransfer":
                        init_file = os.path.join(entry_path, "__init__.py")
                        if os.path.isfile(init_file):
                            plugin_name = self._extract_plugin_name(init_file)
                            self._installed_plugins.append(
                                PluginInfo(plugin_id=entry, plugin_name=plugin_name or entry)
                            )
            seen = set()
            unique = []
            for p in self._installed_plugins:
                if p.plugin_id not in seen:
                    seen.add(p.plugin_id)
                    unique.append(p)
            self._installed_plugins = unique
        except Exception as e:
            logger.error(f"扫描插件目录失败: {e}")

    def _extract_plugin_name(self, init_file):
        try:
            with open(init_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            match = re.search(r'plugin_name\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                return match.group(1)
        except Exception:
            pass
        return None

    # ========== 规则组解析 ==========

    def _load_rule_groups(self, raw_text):
        self._rule_groups = []
        if not raw_text or not raw_text.strip():
            return
        for line_num, line in enumerate(raw_text.strip().split("\n"), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 1)
            if len(parts) != 2:
                logger.warn(f"规则组第 {line_num} 行格式错误，跳过: {line}")
                continue
            folder_path = parts[0].strip()
            keywords_raw = parts[1].strip()
            if not folder_path or not keywords_raw:
                logger.warn(f"规则组第 {line_num} 行内容为空，跳过")
                continue
            keywords = [kw.strip() for kw in keywords_raw.split(",") if kw.strip()]
            if not keywords:
                continue
            self._rule_groups.append(RuleGroup(folder_path, keywords))
        logger.info(f"已加载 {len(self._rule_groups)} 组规则")

    def _parse_structured_rules(self, config):
        """解析结构化规则组配置 (5 组), 返回 RuleGroup 列表"""
        rules = []
        for i in range(1, 6):
            folder = (config.get(f"target_folder_{i}") or "").strip()
            keywords_raw = (config.get(f"keywords_{i}") or "").strip()
            if not folder or not keywords_raw:
                continue
            keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
            if not keywords:
                continue
            rules.append(RuleGroup(folder, keywords))
        return rules

    # ========== 选中插件管理 ==========

    def _build_selected_set(self, selected_ids):
        self._selected_logger_names = set()
        if not selected_ids:
            return
        for pid in selected_ids:
            pid = pid.strip()
            if not pid:
                continue
            found = False
            for info in self._installed_plugins:
                if info.plugin_id == pid:
                    self._selected_logger_names.add(info.logger_name)
                    self._selected_logger_names.add(f"plugins.v2.{pid.lower()}")
                    self._selected_logger_names.add(pid.lower())
                    found = True
                    break
            if not found:
                self._selected_logger_names.add(f"plugins.v2.{pid.lower()}")
                self._selected_logger_names.add(pid.lower())