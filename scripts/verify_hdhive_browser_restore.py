#!/usr/bin/env python3
"""Verify HDHive browser/Cookie mode restoration without importing MoviePilot runtime."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "plugins.v2" / "p115strgmsub" / "__init__.py"
SEARCH = ROOT / "plugins.v2" / "p115strgmsub" / "handlers" / "search.py"
UI = ROOT / "plugins.v2" / "p115strgmsub" / "ui" / "config.py"
BROWSER = ROOT / "plugins.v2" / "p115strgmsub" / "clients" / "hdhive_browser.py"
MANIFEST = ROOT / "package.v2.json"
README = ROOT / "README.md"

init = INIT.read_text(encoding="utf-8")
search = SEARCH.read_text(encoding="utf-8")
ui = UI.read_text(encoding="utf-8")
browser = BROWSER.read_text(encoding="utf-8")
manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
readme = README.read_text(encoding="utf-8")

assert 'config.get("hdhive_query_mode", "playwright")' in init, "__init__.py must default missing hdhive_query_mode to playwright/browser mode"
assert '"hdhive_query_mode": "playwright"' in ui, "UI default_config must default hdhive_query_mode to playwright"
assert "推荐：浏览器模式" in ui or "无需 OpenAPI" in ui, "UI must clearly tell users browser mode does not require OpenAPI"
assert "HDHive Cookie" in readme and "无需 OpenAPI" in readme, "README must document HDHive browser/Cookie mode without OpenAPI"
assert "'model': 'hdhive_cookie'" in ui, "UI must expose hdhive_cookie input for browser/Cookie mode"
assert 'if self._hdhive_query_mode == "api" or self._hdhive_auth_code:' in init, "OpenAPI client must not initialize unconditionally in browser mode"

source_check = re.search(r'if self\._hdhive_query_mode == "playwright" and \(\s*self\._hdhive_cookie or \(self\._hdhive_username and self\._hdhive_password\)\s*\)', search, re.S)
assert source_check is not None, "SearchHandler.get_enabled_sources must enable HDHive playwright mode with cookie OR username/password"
assert 'if self._hdhive_query_mode == "playwright":' in search, "SearchHandler.unlock_hdhive_resource must branch to browser unlock in playwright mode"
assert "client.unlock_resource(slug)" in search, "Playwright/browser unlock path must call HDHiveBrowserClient.unlock_resource(slug)"
assert "self._get_hdhive_browser_client()" in search, "SearchHandler must use HDHiveBrowserClient for browser search/unlock"

assert "cloakbrowser" in browser and "sync_playwright" in browser, "HDHiveBrowserClient must keep cloakbrowser first and Playwright fallback"
assert "/tmdb/" in browser, "HDHiveBrowserClient must search HDHive TMDB detail pages"
assert "/resource/115/{slug}" in browser, "HDHiveBrowserClient must unlock115 resource pages by slug"
assert "标签点击后 DOM 预解析资源" in browser, "HDHiveBrowserClient must pre-scrape DOM after tab click to catch already-rendered MUI cards"
assert "发布于" in browser and "minimal.map" in browser, "HDHiveBrowserClient DOM scraper must use DDSRem-style minimal resource card parsing"
assert "p115strmhelper/hdhive_cookies.json" in browser, "HDHiveBrowserClient must reuse P115StrmHelper HDHive cookie as fallback"
assert "P115StrmHelper Cookie" in browser, "HDHiveBrowserClient must retry with P115StrmHelper cookie when configured cookie is stale"
assert "safe_extract_url" in browser and "Execution context was destroyed" in browser, "HDHiveBrowserClient unlock must tolerate navigation during 115 URL extraction"

plugin = manifest["P115StrgmSub"]
assert plugin["version"] == "1.5.8", "package.v2.json must bump P115StrgmSub version to 1.5.8"
assert "v1.5.8" in plugin["history"], "package.v2.json must add v1.5.8 history entry"
assert "不用 OpenAPI" in plugin["history"]["v1.5.8"] or "无需 OpenAPI" in plugin["history"]["v1.5.8"], "v1.5.8 history must state OpenAPI is not required for browser mode"

print("HDHive browser restore verification passed")
