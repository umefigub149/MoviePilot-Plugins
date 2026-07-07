#!/usr/bin/env python3
"""Static verifier for P115FollowTransfer plugin."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins.v2" / "p115followtransfer" / "__init__.py"
README = ROOT / "plugins.v2" / "p115followtransfer" / "README.md"
REQ = ROOT / "plugins.v2" / "p115followtransfer" / "requirements.txt"
MANIFEST = ROOT / "package.v2.json"
LEGACY_MANIFEST = ROOT / "package.json"

source = PLUGIN.read_text(encoding="utf-8")
readme = README.read_text(encoding="utf-8")
manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
legacy_manifest = json.loads(LEGACY_MANIFEST.read_text(encoding="utf-8")) if LEGACY_MANIFEST.exists() else {}

assert "class P115FollowTransfer(_PluginBase)" in source
assert 'plugin_name = "联动115追更"' in source
assert 'plugin_author = "umefigub149"' in source
assert 'plugin_version = "1.0.5"' in source
assert "def get_command" in source, "_PluginBase abstract get_command must be implemented"
assert "DownloadHistory" not in source or "downloadhistory" in source
assert "SELECT id FROM downloadhistory" in source
assert "username = :username" in source
assert "add_share_115" in source
assert "_p115followtransfer_original_add_share_115" in source
assert "_p115followtransfer_owner" in source
assert "stop_service" in source and "_restore_share_hook" in source
assert "StorageChain" in source and "get_file_item" in source
assert "TransferChain" in source and "do_transfer" in source
assert "CloudDrive储存" in source
assert "first_run_ignore_existing" in source
assert "cursor_initialized" in source and "最新记录ID" in source
assert "记录ID不是记录条数" in readme
assert "书签位置" in readme and "每个功能是什么意思" in readme
assert "本插件直接运行，不做演练" in source and "第一次启用时跳过以前的记录" in source
assert "已连接到 STRM助手" in source
assert "app.plugins.p115strmhelper.service" in source and "sharetransferhelper" in source
assert "debounce_seconds" in source
assert "dry_run" in source and "self._dry_run = False" in source
assert "VCombobox" in source and "115网盘Plus" in source and "u115" in source
assert "本插件直接运行，不做演练" in source and "开始检查追更记录" in source
assert "开始交给 MoviePilot 整理" in source and "已成功交给 MoviePilot 整理" in source
assert "test_follow_paths" in source and "test_share_paths" in source
assert "share_hook_status" in source and "share_hook_message" in source
assert "_path_help_message" in source and "这个目录现在不能交给 MP 整理" in source
assert "和 STRM助手网盘管理的区别" in readme
assert "直接运行，不做演练模式" in readme and "日志怎么看" in readme
assert '"auth": "bear"' in source or "'auth': 'bear'" in source or "'auth': \"bear\"" in source or '"auth": \'bear\'' in source

plugin = manifest["P115FollowTransfer"]
assert plugin["name"] == "联动115追更"
assert plugin["version"] == "1.0.5"
assert plugin["author"] == "umefigub149"
assert plugin.get("release") is False
assert "v1.0.5" in plugin["history"] and "v1.0.4" in plugin["history"] and "v1.0.3" in plugin["history"] and "v1.0.2" in plugin["history"] and "v1.0.1" in plugin["history"] and "v1.0.0" in plugin["history"]

for key in ("UID=", "CID=", "SEID=", "KID=", "ghp_", "sk-"):
    for path, text in [(PLUGIN, source), (README, readme)]:
        if key in text and "xxx" not in text:
            raise AssertionError(f"possible secret marker {key} in {path}")

assert REQ.read_text(encoding="utf-8").strip() == "# no external dependencies"

for manifest_name, payload in (("package.v2.json", manifest), ("package.json", legacy_manifest)):
    for plugin_id, meta in payload.items():
        if isinstance(meta, dict) and "author" in meta:
            assert meta["author"] == "umefigub149", f"{manifest_name}:{plugin_id} author must be umefigub149"

for stale in ("WorkBuddy", "mrtian2016", "outxool"):
    assert stale not in source, f"stale author marker in plugin source: {stale}"

print("P115FollowTransfer verification passed")
