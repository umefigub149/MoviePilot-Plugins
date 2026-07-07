"""
MoviePilot V2 插件：联动115追更

极简职责：
1. 轮询 115网盘订阅追更-自用版(P115StrgmSub) 写入的 DownloadHistory 新记录。
2. 桥接 115网盘STRM助手(P115StrmHelper) 分享转存成功信号。
3. 检测到成功信号后延迟指定秒数，将用户手动配置的固定目录加入 MoviePilot 整理队列。

边界：
- 不解析本次转存的精确子目录。
- 不扫描 115 网盘目录。
- 不做目录 diff。
- 不读取、不保存、不输出任何 Cookie/Token。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from time import sleep, time
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.plugin import PluginManager
from app.db import SessionFactory
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import FileItem


class P115FollowTransfer(_PluginBase):
    """联动115追更：成功转存后延迟把固定目录加入 MP 整理队列。"""

    plugin_name = "联动115追更"
    plugin_desc = "检测115追更/STRM助手成功转存后，稍等一会儿把指定目录交给MoviePilot整理"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "1.0.5"
    plugin_author = "umefigub149"
    author_url = "https://github.com/umefigub149"
    plugin_config_prefix = "p115followtransfer_"
    plugin_order = 1
    auth_level = 1

    RUNTIME_STATE_KEY = "runtime_state"
    RECENT_EVENTS_KEY = "recent_events"
    DEFAULT_CRON = "*/5 * * * *"
    DEFAULT_SOURCE_USERNAME = "P115StrgmSub"
    DEFAULT_STORAGE_NAME = "CloudDrive储存"
    DEFAULT_FOLLOW_DIRS = "/网盘整理/网盘待整理目录/Movie\n/网盘整理/网盘待整理目录/TV"
    DEFAULT_SHARE_DIRS = "/最近接收\n/网盘整理/分享转存目录"
    DEFAULT_ALLOWED_ROOTS = "/网盘整理\n/最近接收\n/我的接收"

    _enabled: bool = False
    _onlyonce: bool = False
    _dry_run: bool = False
    _cron: str = DEFAULT_CRON
    _source_username: str = DEFAULT_SOURCE_USERNAME
    _first_run_ignore_existing: bool = True
    _follow_enabled: bool = True
    _follow_delay_seconds: int = 60
    _follow_dirs_text: str = DEFAULT_FOLLOW_DIRS
    _share_hook_enabled: bool = True
    _share_delay_seconds: int = 60
    _share_dirs_text: str = DEFAULT_SHARE_DIRS
    _storage_name: str = DEFAULT_STORAGE_NAME
    _allowed_roots_text: str = DEFAULT_ALLOWED_ROOTS
    _debounce_seconds: int = 300
    _recent_events_limit: int = 50

    _runtime_lock: Lock = Lock()
    _hook_lock: Lock = Lock()
    _generation: int = 0
    _hooked_helper: Any = None
    _storagechain: Optional[StorageChain] = None
    _transferchain: Optional[TransferChain] = None

    def init_plugin(self, config: dict = None):
        """加载配置并安装 Hook。"""
        self.stop_service()
        self._generation += 1
        self._storagechain = StorageChain()
        self._transferchain = TransferChain()

        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._dry_run = False
        self._cron = str(config.get("cron") or self.DEFAULT_CRON).strip() or self.DEFAULT_CRON
        self._source_username = str(config.get("source_username") or self.DEFAULT_SOURCE_USERNAME).strip() or self.DEFAULT_SOURCE_USERNAME
        self._first_run_ignore_existing = bool(config.get("first_run_ignore_existing", True))
        self._follow_enabled = bool(config.get("follow_enabled", True))
        self._follow_delay_seconds = self._safe_int(config.get("follow_delay_seconds"), 60)
        self._follow_dirs_text = str(config.get("follow_dirs") or self.DEFAULT_FOLLOW_DIRS)
        self._share_hook_enabled = bool(config.get("share_hook_enabled", True))
        self._share_delay_seconds = self._safe_int(config.get("share_delay_seconds"), 60)
        self._share_dirs_text = str(config.get("share_dirs") or self.DEFAULT_SHARE_DIRS)
        self._storage_name = str(config.get("storage_name") or self.DEFAULT_STORAGE_NAME).strip() or self.DEFAULT_STORAGE_NAME
        self._allowed_roots_text = str(config.get("allowed_roots") or self.DEFAULT_ALLOWED_ROOTS)
        self._debounce_seconds = self._safe_int(config.get("debounce_seconds"), 300)
        self._recent_events_limit = max(10, min(self._safe_int(config.get("recent_events_limit"), 50), 200))

        if self._enabled and self._share_hook_enabled:
            self._install_share_hook()

        if self._enabled and self._onlyonce:
            self._onlyonce = False
            self.__update_config()
            worker = Thread(target=self.scan_follow_history, name="P115FollowTransfer-OnlyOnce", daemon=True)
            worker.start()

        logger.info(
            "【联动115追更】插件初始化完成：启用=%s，追更联动=%s，STRM助手分享联动=%s，目标存储=%s，运行方式=直接运行",
            self._enabled,
            self._follow_enabled,
            self._share_hook_enabled,
            self._storage_name,
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "联动115追更状态",
            },
            {
                "path": "/poll_now",
                "endpoint": self.api_poll_now,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即检测追更转存",
            },
            {
                "path": "/enqueue_follow_roots",
                "endpoint": self.api_enqueue_follow_roots,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即入队追更目录",
            },
            {
                "path": "/enqueue_share_roots",
                "endpoint": self.api_enqueue_share_roots,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即入队分享目录",
            },
            {
                "path": "/test_follow_paths",
                "endpoint": self.api_test_follow_paths,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "测试追更目录解析",
            },
            {
                "path": "/test_share_paths",
                "endpoint": self.api_test_share_paths,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "测试分享目录解析",
            },
            {
                "path": "/clear_cache",
                "endpoint": self.api_clear_cache,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清理防抖缓存",
            },
            {
                "path": "/reset_cursor",
                "endpoint": self.api_reset_cursor,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "从当前记录重新开始",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._follow_enabled or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as err:
            logger.error("【联动115追更】cron 表达式无效: %s - %s", self._cron, err)
            return []
        return [
            {
                "id": "P115FollowTransferScan",
                "name": "联动115追更检测",
                "trigger": trigger,
                "func": self.scan_follow_history,
                "kwargs": {},
            }
        ]

    def stop_service(self):
        self._generation += 1
        self._restore_share_hook()

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "检测到 115追更 或 STRM助手分享转存成功后，只把你手动配置的固定目录交给 MoviePilot 整理；不扫描115目录、不乱猜子目录。",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "density": "compact", "text": "本插件直接运行，不做演练；请先确认目录配置正确。"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "first_run_ignore_existing", "label": "第一次启用时跳过以前的记录"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VCombobox", "props": {"model": "storage_name", "label": "目标存储", "items": [{"title": "CloudDrive2（CloudDrive储存）", "value": "CloudDrive储存"}, {"title": "u115", "value": "u115"}, {"title": "115网盘Plus", "value": "115网盘Plus"}, {"title": "本地存储（local）", "value": "local"}], "placeholder": "CloudDrive储存", "hint": "115内部路径通常选 CloudDrive储存；也可按实际 MP 存储名称选择 u115、115网盘Plus、local 或手动输入。", "persistentHint": True}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "cron", "label": "多久检查一次追更", "placeholder": "*/5 * * * *"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "source_username", "label": "追更记录来源", "placeholder": "P115StrgmSub"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "follow_enabled", "label": "追更转存后自动整理"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "follow_delay_seconds", "label": "追更成功后等待几秒再整理", "type": "number"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "share_hook_enabled", "label": "STRM助手分享转存后自动整理"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "share_delay_seconds", "label": "分享转存成功后等待几秒再整理", "type": "number"}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextarea", "props": {"model": "follow_dirs", "label": "追更成功后要整理的目录（一行一个）", "rows": 4, "hint": "115追更插件转存成功后，会把这些目录交给 MoviePilot 整理。", "persistentHint": True}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextarea", "props": {"model": "share_dirs", "label": "分享转存成功后要整理的目录（一行一个）", "rows": 4, "hint": "STRM助手分享转存成功后，会把这些目录交给 MoviePilot 整理。", "persistentHint": True}}]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [{"component": "VTextarea", "props": {"model": "allowed_roots", "label": "只允许整理这些路径下面的目录（一行一个）", "rows": 3, "hint": "安全保护：只有这些路径下面的目录才会被整理；不要填写 / 。", "persistentHint": True}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [{"component": "VTextField", "props": {"model": "debounce_seconds", "label": "短时间内防重复整理秒数", "type": "number"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [{"component": "VTextField", "props": {"model": "recent_events_limit", "label": "页面保留多少条记录", "type": "number"}}]},
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "dry_run": False,
            "cron": self.DEFAULT_CRON,
            "source_username": self.DEFAULT_SOURCE_USERNAME,
            "first_run_ignore_existing": True,
            "follow_enabled": True,
            "follow_delay_seconds": 60,
            "follow_dirs": self.DEFAULT_FOLLOW_DIRS,
            "share_hook_enabled": True,
            "share_delay_seconds": 60,
            "share_dirs": self.DEFAULT_SHARE_DIRS,
            "storage_name": self.DEFAULT_STORAGE_NAME,
            "allowed_roots": self.DEFAULT_ALLOWED_ROOTS,
            "debounce_seconds": 300,
            "recent_events_limit": 50,
        }

    def get_page(self) -> Optional[List[dict]]:
        state = self._load_runtime_state()
        stats = state.get("stats") or {}
        events = self.get_data(self.RECENT_EVENTS_KEY) or []
        rows = []
        for event in list(events)[-self._recent_events_limit:][::-1]:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": str(event.get("time", ""))},
                    {"component": "td", "text": str(event.get("status", ""))},
                    {"component": "td", "text": str(event.get("path", ""))},
                    {"component": "td", "text": str(event.get("message", ""))},
                ],
            })
        return [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal"},
                "text": (
                    f"状态：{'启用' if self._enabled else '停用'}；"
                    f"STRM助手分享联动：{state.get('share_hook_status', '未连接')}（{state.get('share_hook_message', '无')}）；"
                    f"已处理到记录ID：{state.get('last_seen_id', 0)}；"
                    f"已成功交给MP整理：{stats.get('enqueue_success', 0)}；失败：{stats.get('enqueue_error', 0)}；跳过：{stats.get('enqueue_skip', 0)}"
                ),
            },
            {
                "component": "VTable",
                "props": {"hover": True, "density": "compact"},
                "content": [
                    {
                        "component": "thead",
                        "content": [{"component": "tr", "content": [
                            {"component": "th", "text": "时间"},
                            {"component": "th", "text": "状态"},
                            {"component": "th", "text": "目录"},
                            {"component": "th", "text": "说明"},
                        ]}],
                    },
                    {"component": "tbody", "content": rows},
                ],
            },
        ]

    def api_status(self) -> Dict[str, Any]:
        return {"success": True, "data": self._status_payload()}

    def api_poll_now(self) -> Dict[str, Any]:
        result = self.scan_follow_history(reason="手动立即检查")
        return {"success": True, "data": result}

    def api_enqueue_follow_roots(self) -> Dict[str, Any]:
        result = self.enqueue_follow_dirs_now(reason="手动把追更目录交给MP整理")
        return {"success": True, "data": result}

    def api_enqueue_share_roots(self) -> Dict[str, Any]:
        result = self.enqueue_share_dirs_now(reason="手动把分享目录交给MP整理")
        return {"success": True, "data": result}

    def api_test_follow_paths(self) -> Dict[str, Any]:
        return {"success": True, "data": self._test_paths(self._parse_lines(self._follow_dirs_text), "测试追更目录能不能整理")}

    def api_test_share_paths(self) -> Dict[str, Any]:
        return {"success": True, "data": self._test_paths(self._parse_lines(self._share_dirs_text), "测试分享目录能不能整理")}

    def api_clear_cache(self) -> Dict[str, Any]:
        with self._runtime_lock:
            state = self._load_runtime_state()
            state["debounce"] = {}
            self.save_data(self.RUNTIME_STATE_KEY, state)
        self._record_event("CACHE", "-", "已清空防抖缓存")
        return {"success": True}

    def api_reset_cursor(self) -> Dict[str, Any]:
        latest_id = self._get_latest_history_id()
        with self._runtime_lock:
            state = self._load_runtime_state()
            state["last_seen_id"] = latest_id
            state["cursor_initialized"] = True
            self.save_data(self.RUNTIME_STATE_KEY, state)
        self._record_event("CURSOR", "-", f"已从当前最新位置重新开始：当前最新记录ID={latest_id}，以前的记录不会再处理")
        return {"success": True, "last_seen_id": latest_id}

    def scan_follow_history(self, reason: str = "定时检测") -> Dict[str, int]:
        logger.info("【联动115追更】开始检查追更记录：原因=%s，插件启用=%s，追更联动=%s，来源=%s", reason, self._enabled, self._follow_enabled, self._source_username)
        if not self._enabled or not self._follow_enabled:
            logger.info("【联动115追更】本次检查跳过：插件未启用或追更联动未打开")
            return {"seen": 0, "scheduled": 0, "skipped": 1, "errors": 0}
        self._install_share_hook()
        state = self._load_runtime_state()
        last_seen_id = self._safe_int(state.get("last_seen_id"), 0)
        latest_id = self._get_latest_history_id()
        logger.info("【联动115追更】当前记录位置：已处理到ID=%s，数据库最新ID=%s，是否已经完成首次跳过旧记录=%s", last_seen_id, latest_id, bool(state.get("cursor_initialized", False)))
        if latest_id <= 0:
            self._record_event("INFO", "-", "未找到追更来源转存历史")
            return {"seen": 0, "scheduled": 0, "skipped": 0, "errors": 0}
        if self._first_run_ignore_existing and not bool(state.get("cursor_initialized", False)):
            state["last_seen_id"] = latest_id
            state["cursor_initialized"] = True
            self.save_data(self.RUNTIME_STATE_KEY, state)
            self._record_event("CURSOR", "-", f"第一次启用或升级后，从当前最新位置开始：当前最新记录ID={latest_id}，以前的记录已跳过")
            return {"seen": 0, "scheduled": 0, "skipped": 1, "errors": 0}
        ids = self._get_new_history_ids(last_seen_id)
        logger.info("【联动115追更】本次发现新增记录数量=%s，新增记录ID=%s", len(ids), ids)
        if not ids:
            if not bool(state.get("cursor_initialized", False)):
                state["cursor_initialized"] = True
                self.save_data(self.RUNTIME_STATE_KEY, state)
            return {"seen": 0, "scheduled": 0, "skipped": 0, "errors": 0}
        scheduled = self._schedule_dirs(
            dirs=self._parse_lines(self._follow_dirs_text),
            delay_seconds=self._follow_delay_seconds,
            reason=f"{reason}: 检测到 {self._source_username} 新增 {len(ids)} 条转存记录，最新记录ID={ids[-1]}",
            source="follow",
            cursor_id=ids[-1],
        )
        return {"seen": len(ids), "scheduled": scheduled, "skipped": 0, "errors": 0}

    def enqueue_follow_dirs_now(self, reason: str = "手动入队追更目录") -> Dict[str, int]:
        return self._enqueue_dirs(self._parse_lines(self._follow_dirs_text), reason=reason, source="manual-follow", cursor_id=None)

    def enqueue_share_dirs_now(self, reason: str = "手动入队分享目录") -> Dict[str, int]:
        return self._enqueue_dirs(self._parse_lines(self._share_dirs_text), reason=reason, source="manual-share", cursor_id=None)

    def _install_share_hook(self) -> None:
        if not self._enabled or not self._share_hook_enabled:
            self._set_hook_status("未开启", "插件未启用，或没有打开“STRM助手分享转存后自动整理”")
            return
        with self._hook_lock:
            if self._hooked_helper is not None:
                self._set_hook_status("已连接", "已连接到 STRM助手的分享转存功能")
                return
            helper = self._find_p115strmhelper_share_helper()
            if helper is None:
                self._set_hook_status("没连上STRM助手", "没有找到 STRM助手的分享转存功能；请确认 STRM助手已启用")
                self._record_event("INFO", "-", "没有找到 STRM助手的分享转存功能；稍后会再试")
                return
            current_func = getattr(helper, "add_share_115", None)
            if current_func is None:
                self._set_hook_status("版本不兼容", "当前 STRM助手版本没有可连接的分享转存接口")
                self._record_event("ERROR", "-", "当前 STRM助手版本没有可连接的分享转存接口")
                return
            owner = getattr(helper, "_p115followtransfer_owner", None)
            if owner is self:
                self._hooked_helper = helper
                self._set_hook_status("已连接", "已连接到 STRM助手的分享转存功能")
                return
            if owner is not None:
                self._set_hook_status("已被占用", "STRM助手分享转存接口已被另一个实例连接，本插件先不重复连接")
                self._record_event("INFO", "-", "STRM助手分享转存接口已被另一个实例连接，本插件先不重复连接")
                return
            original_func = getattr(helper, "_p115followtransfer_original_add_share_115", None) or current_func
            bridge = self
            generation = self._generation

            def wrapped_add_share_115(*args, **kwargs):
                result = original_func(*args, **kwargs)
                try:
                    if bridge._is_share_transfer_success(result):
                        bridge._schedule_dirs(
                            dirs=bridge._parse_lines(bridge._share_dirs_text),
                            delay_seconds=bridge._share_delay_seconds,
                            reason="P115StrmHelper 分享转存成功",
                            source="share",
                            cursor_id=None,
                            generation=generation,
                        )
                except Exception as err:
                    logger.error("【联动115追更】分享转存 Hook 调度失败: %s", err, exc_info=True)
                return result

            setattr(helper, "add_share_115", wrapped_add_share_115)
            setattr(helper, "_p115followtransfer_original_add_share_115", original_func)
            setattr(helper, "_p115followtransfer_owner", self)
            self._hooked_helper = helper
            self._set_hook_status("已连接", "已连接到 STRM助手的分享转存功能")
            self._record_event("INFO", "-", "已连接到 STRM助手分享转存功能")

    def _restore_share_hook(self) -> None:
        with self._hook_lock:
            helper = self._hooked_helper
            if helper is None:
                return
            if getattr(helper, "_p115followtransfer_owner", None) is not self:
                self._hooked_helper = None
                return
            original_func = getattr(helper, "_p115followtransfer_original_add_share_115", None)
            if original_func is not None:
                try:
                    setattr(helper, "add_share_115", original_func)
                    delattr(helper, "_p115followtransfer_original_add_share_115")
                    delattr(helper, "_p115followtransfer_owner")
                    self._set_hook_status("未连接", "插件停止或重载，已断开 STRM助手分享转存联动")
                    self._record_event("INFO", "-", "已断开 STRM助手分享转存联动")
                except Exception as err:
                    logger.debug("【联动115追更】恢复 Hook 失败: %s", err)
            self._hooked_helper = None

    @staticmethod
    def _is_share_transfer_success(result: Any) -> bool:
        return isinstance(result, tuple) and len(result) >= 1 and bool(result[0])

    @staticmethod
    def _find_p115strmhelper_share_helper() -> Any:
        try:
            from app.plugins.p115strmhelper.service import servicer as p115strm_servicer
            helper = getattr(p115strm_servicer, "sharetransferhelper", None)
            if helper is not None:
                return helper
        except Exception as err:
            logger.debug("【联动115追更】读取 STRM助手服务对象失败: %s", err)

        manager = PluginManager()
        candidate_ids = ["P115StrmHelper", "p115strmhelper"] + manager.get_running_plugin_ids()
        seen = set()
        for pid in candidate_ids:
            if not pid or pid in seen:
                continue
            seen.add(pid)
            helper = manager.get_plugin_attr(pid, "sharetransferhelper")
            if helper is not None:
                return helper
            helper = manager.get_plugin_attr(pid, "_sharetransferhelper")
            if helper is not None:
                return helper
        return None

    def _schedule_dirs(
        self,
        dirs: List[str],
        delay_seconds: int,
        reason: str,
        source: str,
        cursor_id: Optional[int],
        generation: Optional[int] = None,
    ) -> int:
        dirs = self._dedupe_paths(dirs)
        logger.info("【联动115追更】准备安排整理：来源=%s，等待秒数=%s，目录=%s，原因=%s", source, max(delay_seconds, 0), dirs, reason)
        if not dirs:
            self._record_event("SKIP", "-", f"{reason}: 未配置入队目录")
            return 0
        generation = self._generation if generation is None else generation
        worker = Thread(
            target=self._delayed_enqueue_dirs,
            args=(dirs, max(delay_seconds, 0), reason, source, cursor_id, generation),
            name=f"P115FollowTransfer-{source}",
            daemon=True,
        )
        worker.start()
        self._record_event("SCHEDULE", "\n".join(dirs), f"{reason}，{max(delay_seconds, 0)}秒后入队")
        return len(dirs)

    def _delayed_enqueue_dirs(
        self,
        dirs: List[str],
        delay_seconds: int,
        reason: str,
        source: str,
        cursor_id: Optional[int],
        generation: int,
    ) -> None:
        if delay_seconds > 0:
            logger.info("【联动115追更】等待 %s 秒后开始整理：来源=%s，目录=%s", delay_seconds, source, dirs)
            sleep(delay_seconds)
        logger.info("【联动115追更】开始执行整理：来源=%s，目录=%s，原因=%s", source, dirs, reason)
        if generation != self._generation or not self._enabled:
            self._record_event("SKIP", "-", f"{reason}: 插件已重载或停用，取消入队")
            return
        result = self._enqueue_dirs(dirs=dirs, reason=reason, source=source, cursor_id=cursor_id)
        if cursor_id and result.get("errors", 0) == 0:
            state = self._load_runtime_state()
            state["last_seen_id"] = max(self._safe_int(state.get("last_seen_id"), 0), cursor_id)
            state["cursor_initialized"] = True
            self.save_data(self.RUNTIME_STATE_KEY, state)
            self._record_event("CURSOR", "-", f"已处理到记录ID={cursor_id}")

    def _enqueue_dirs(self, dirs: List[str], reason: str, source: str, cursor_id: Optional[int]) -> Dict[str, int]:
        enqueued = 0
        skipped = 0
        errors = 0
        for path in self._dedupe_paths(dirs):
            ok, skipped_one = self._enqueue_path(path=path, reason=reason, source=source)
            if ok:
                enqueued += 1
            elif skipped_one:
                skipped += 1
            else:
                errors += 1
        self._stats_increment("enqueue_success", enqueued)
        self._stats_increment("enqueue_skip", skipped)
        self._stats_increment("enqueue_error", errors)
        return {"enqueued": enqueued, "skipped": skipped, "errors": errors}

    def _enqueue_path(self, path: str, reason: str, source: str) -> Tuple[bool, bool]:
        normalized_path = self._normalize_path(path)
        if not normalized_path or normalized_path == "/":
            self._record_event("SKIP", normalized_path or "-", "空路径或根路径禁止入队")
            return False, True
        if not self._is_allowed_path(normalized_path):
            self._record_event("SKIP", normalized_path, "路径不在允许前缀内")
            return False, True
        if self._is_debounced(source, normalized_path):
            self._record_event("SKIP", normalized_path, f"防抖窗口内重复触发: {source}")
            return False, True
        try:
            logger.info("【联动115追更】开始处理目录：来源=%s，目标存储=%s，目录=%s，原因=%s", source, self._storage_name, normalized_path, reason)
            file_item = self._build_file_item(normalized_path)
            if not file_item:
                self._record_event("ERROR", normalized_path, self._path_help_message(self._storage_name, normalized_path))
                return False, False
            logger.info("【联动115追更】目录解析成功：storage=%s，path=%s，type=%s，name=%s，fileid=%s", getattr(file_item, "storage", None), getattr(file_item, "path", None), getattr(file_item, "type", None), getattr(file_item, "name", None), getattr(file_item, "fileid", None))
            transferchain = self._transferchain or TransferChain()
            logger.info("【联动115追更】开始交给 MoviePilot 整理：storage=%s，path=%s", file_item.storage, file_item.path)
            transferchain.do_transfer(fileitem=file_item, background=True, manual=True)
            logger.info("【联动115追更】已成功交给 MoviePilot 整理：storage=%s，path=%s", file_item.storage, file_item.path)
            self._record_event("ENQUEUE", normalized_path, f"已交给 MoviePilot 整理：目标存储={file_item.storage}，原因={reason}")
            return True, False
        except Exception as err:
            logger.error("【联动115追更】入队失败: %s %s", normalized_path, err, exc_info=True)
            self._record_event("ERROR", normalized_path, f"入队失败: {err}")
            return False, False

    def _build_file_item(self, normalized_path: str) -> Optional[FileItem]:
        storage_name = self._storage_name.strip() or self.DEFAULT_STORAGE_NAME
        path_obj = Path(normalized_path)
        storagechain = self._storagechain or StorageChain()
        logger.info("【联动115追更】正在解析目录：目标存储=%s，目录=%s", storage_name, normalized_path)
        try:
            file_item = storagechain.get_file_item(storage=storage_name, path=path_obj)
            if file_item:
                logger.info("【联动115追更】StorageChain 解析目录成功：storage=%s，path=%s，fileid=%s", getattr(file_item, "storage", None), getattr(file_item, "path", None), getattr(file_item, "fileid", None))
                return file_item
        except Exception as err:
            logger.debug("【联动115追更】StorageChain.get_file_item 失败: storage=%s path=%s err=%s", storage_name, normalized_path, err)

        if storage_name != "local":
            name = path_obj.name or normalized_path.strip("/") or normalized_path
            logger.info("【联动115追更】StorageChain 没有返回目录信息，改用固定目录方式交给 MP：storage=%s，path=%s", storage_name, normalized_path)
            return FileItem(
                storage=storage_name,
                type="dir",
                path=normalized_path,
                name=name,
                basename=name,
            )

        if not path_obj.exists():
            return None
        stat_result = path_obj.stat()
        if path_obj.is_dir():
            return FileItem(
                storage="local",
                type="dir",
                path=path_obj.as_posix(),
                name=path_obj.name or path_obj.as_posix(),
                basename=path_obj.stem or path_obj.name or path_obj.as_posix(),
                modify_time=stat_result.st_mtime,
            )
        return FileItem(
            storage="local",
            type="file",
            path=path_obj.as_posix(),
            name=path_obj.name,
            basename=path_obj.stem,
            extension=path_obj.suffix[1:].lower(),
            size=stat_result.st_size,
            modify_time=stat_result.st_mtime,
        )

    def _test_paths(self, dirs: List[str], reason: str) -> List[Dict[str, Any]]:
        results = []
        for path in self._dedupe_paths(dirs):
            allowed = self._is_allowed_path(path)
            item = self._build_file_item(path) if allowed else None
            message = "可解析，演练通过" if item else self._path_help_message(self._storage_name, path)
            if not allowed:
                message = "路径不在允许入队路径前缀内，请检查允许入队路径前缀配置"
            results.append({
                "path": path,
                "storage": self._storage_name,
                "allowed": allowed,
                "resolvable": bool(item),
                "message": message,
            })
            self._record_event("DRYRUN" if item and allowed else "ERROR", path, f"{reason}: {message}")
        if not results:
            self._record_event("SKIP", "-", f"{reason}: 未配置目录")
        return results

    @staticmethod
    def _path_help_message(storage_name: str, path: str) -> str:
        return (
            f"这个目录现在不能交给 MP 整理：目标存储={storage_name}，目录={path}。请检查："
            "1. 对应的 MP 存储插件是否已经启用；"
            "2. 目标存储名称是否填对，例如 CloudDrive储存、u115、115网盘Plus；"
            "3. 目录路径是否是这个存储里能看到的路径；"
            "4. 如果你用的是 CloudDrive2，通常目标存储填 CloudDrive储存。"
        )

    def _set_hook_status(self, status: str, message: str) -> None:
        with self._runtime_lock:
            state = self._load_runtime_state()
            state["share_hook_status"] = status
            state["share_hook_message"] = message
            self.save_data(self.RUNTIME_STATE_KEY, state)

    def _get_latest_history_id(self) -> int:
        with SessionFactory() as db:
            row = db.execute(
                text("SELECT COALESCE(MAX(id), 0) FROM downloadhistory WHERE username = :username"),
                {"username": self._source_username},
            ).first()
            return int(row[0] or 0) if row else 0

    def _get_new_history_ids(self, last_seen_id: int) -> List[int]:
        with SessionFactory() as db:
            rows = db.execute(
                text(
                    "SELECT id FROM downloadhistory "
                    "WHERE username = :username AND id > :last_seen_id "
                    "ORDER BY id ASC LIMIT 50"
                ),
                {"username": self._source_username, "last_seen_id": int(last_seen_id or 0)},
            ).fetchall()
            return [int(row[0]) for row in rows]

    def _load_runtime_state(self) -> Dict[str, Any]:
        state = self.get_data(self.RUNTIME_STATE_KEY) or {}
        if not isinstance(state, dict):
            state = {}
        state.setdefault("last_seen_id", 0)
        state.setdefault("cursor_initialized", False)
        state.setdefault("debounce", {})
        state.setdefault("stats", {})
        return state

    def _status_payload(self) -> Dict[str, Any]:
        state = self._load_runtime_state()
        return {
            "enabled": self._enabled,
            "hooked": bool(self._hooked_helper),
            "share_hook_status": state.get("share_hook_status", "未安装"),
            "share_hook_message": state.get("share_hook_message", "无"),
            "storage_name": self._storage_name,
            "last_seen_id": state.get("last_seen_id", 0),
            "cursor_initialized": bool(state.get("cursor_initialized", False)),
            "stats": state.get("stats", {}),
            "follow_dirs": self._parse_lines(self._follow_dirs_text),
            "share_dirs": self._parse_lines(self._share_dirs_text),
        }

    def _is_debounced(self, source: str, path: str) -> bool:
        if self._debounce_seconds <= 0:
            return False
        now = time()
        key = f"{source}|{self._storage_name}|{path}"
        with self._runtime_lock:
            state = self._load_runtime_state()
            debounce = state.setdefault("debounce", {})
            last = float(debounce.get(key) or 0)
            if now - last < self._debounce_seconds:
                return True
            debounce[key] = now
            cutoff = now - max(self._debounce_seconds * 4, 3600)
            state["debounce"] = {k: v for k, v in debounce.items() if float(v or 0) >= cutoff}
            self.save_data(self.RUNTIME_STATE_KEY, state)
            return False

    def _stats_increment(self, key: str, amount: int = 1) -> None:
        if amount <= 0:
            return
        with self._runtime_lock:
            state = self._load_runtime_state()
            stats = state.setdefault("stats", {})
            stats[key] = self._safe_int(stats.get(key), 0) + amount
            stats["last_run_at"] = self._now_text()
            self.save_data(self.RUNTIME_STATE_KEY, state)

    def _record_event(self, status: str, path: str, message: str) -> None:
        event = {
            "time": self._now_text(),
            "timestamp": time(),
            "status": self._status_text(status),
            "path": path,
            "message": message,
        }
        events = self.get_data(self.RECENT_EVENTS_KEY) or []
        if not isinstance(events, list):
            events = []
        events.append(event)
        events = events[-self._recent_events_limit:]
        self.save_data(self.RECENT_EVENTS_KEY, events)
        if status in {"ERROR"}:
            logger.error("【联动115追更】%s %s - %s", event["status"], path, message)
        else:
            logger.info("【联动115追更】%s %s - %s", event["status"], path, message)

    @staticmethod
    def _status_text(status: str) -> str:
        return {
            "ENQUEUE": "✅ 已交给MP整理",
            "DRYRUN": "🧪 只演练未真正整理",
            "SKIP": "⚠️ 已跳过",
            "ERROR": "❌ 错误",
            "INFO": "ℹ️ 信息",
            "CURSOR": "🔰 已处理位置",
            "CACHE": "🧹 缓存",
            "SCHEDULE": "⏰ 已安排稍后整理",
        }.get(status, status)

    def _is_allowed_path(self, path: str) -> bool:
        roots = self._parse_lines(self._allowed_roots_text)
        if not roots:
            return True
        for root in roots:
            normalized_root = self._normalize_path(root)
            if not normalized_root or normalized_root == "/":
                continue
            if path == normalized_root or path.startswith(normalized_root.rstrip("/") + "/"):
                return True
        return False

    @staticmethod
    def _dedupe_paths(paths: List[str]) -> List[str]:
        result = []
        seen = set()
        for path in paths:
            normalized = P115FollowTransfer._normalize_path(path)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    @staticmethod
    def _parse_lines(text_value: str) -> List[str]:
        values = []
        for raw in str(text_value or "").replace(",", "\n").splitlines():
            value = raw.strip()
            if value and not value.startswith("#"):
                values.append(value)
        return values

    @staticmethod
    def _normalize_path(path: str) -> str:
        value = str(path or "").strip()
        if not value:
            return ""
        value = value.replace("\\", "/")
        while "//" in value:
            value = value.replace("//", "/")
        if not value.startswith("/"):
            value = "/" + value
        return value.rstrip("/") or "/"

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%m-%d %H:%M:%S")

    def __update_config(self) -> None:
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "dry_run": False,
            "cron": self._cron,
            "source_username": self._source_username,
            "first_run_ignore_existing": self._first_run_ignore_existing,
            "follow_enabled": self._follow_enabled,
            "follow_delay_seconds": self._follow_delay_seconds,
            "follow_dirs": self._follow_dirs_text,
            "share_hook_enabled": self._share_hook_enabled,
            "share_delay_seconds": self._share_delay_seconds,
            "share_dirs": self._share_dirs_text,
            "storage_name": self._storage_name,
            "allowed_roots": self._allowed_roots_text,
            "debounce_seconds": self._debounce_seconds,
            "recent_events_limit": self._recent_events_limit,
        })
