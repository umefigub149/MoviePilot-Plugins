"""
Minimal HDHive browser client.

This client intentionally avoids the old bundled ``lib.hdhive`` dependency.
It uses the public HDHive web pages with Playwright, keeps the same resource
shape expected by SearchHandler, and leaves all transfer work to the existing
115 client.
"""
import json
import re
from contextlib import contextmanager
from pathlib import Path
from time import time
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlparse

from app.core.config import settings
from app.log import logger

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - MoviePilot runtime may install later
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

try:
    from cloakbrowser import launch_context as cloak_launch_context
except Exception:  # pragma: no cover - depends on MoviePilot runtime
    cloak_launch_context = None


class HDHiveBrowserError(Exception):
    """Raised when browser automation cannot complete the requested action."""


class HDHiveLoginError(HDHiveBrowserError):
    """Raised when HDHive login state is missing or invalid."""


class HDHiveBrowserClient:
    """Small Playwright-backed client for HDHive web search and unlock."""

    BASE_URL = "https://hdhive.com"
    COOKIE_DATA_KEY = "hdhive_browser_cookie"

    def __init__(
        self,
        cookie: str = "",
        username: str = "",
        password: str = "",
        proxy: Optional[Any] = None,
        get_data_func=None,
        save_data_func=None,
        headless: bool = True,
        force_password_login: bool = False,
    ):
        self._force_password_login = bool(force_password_login)
        self._cookie = "" if self._force_password_login else (cookie or "").strip()
        self._username = (username or "").strip()
        self._password = password or ""
        self._proxy = proxy
        self._get_data = get_data_func
        self._save_data = save_data_func
        self._headless = headless
        self._skip_external_cookie_once = False

    @property
    def is_ready(self) -> bool:
        if self._force_password_login:
            return bool(self._username and self._password)
        return bool(self._cookie or self._load_saved_cookie() or (self._username and self._password))

    @staticmethod
    def _parse_proxy(proxy) -> Optional[Dict[str, str]]:
        if not proxy:
            return None
        if isinstance(proxy, dict):
            proxy_url = proxy.get("http") or proxy.get("https") or proxy.get("server")
        else:
            proxy_url = str(proxy)
        if not proxy_url:
            return None
        parsed = urlparse(proxy_url)
        if not parsed.scheme:
            return {"server": proxy_url}
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
        result = {"server": server}
        if parsed.username:
            result["username"] = parsed.username
        if parsed.password:
            result["password"] = parsed.password
        return result

    @staticmethod
    def _parse_cookie_string(cookie: str) -> Dict[str, str]:
        pairs: Dict[str, str] = {}
        for part in (cookie or "").split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            if key:
                pairs[key] = value
        return pairs

    @staticmethod
    def _cookie_string_from_raw(raw_cookies: List[Dict[str, Any]]) -> str:
        keep = []
        for cookie in raw_cookies or []:
            name = cookie.get("name")
            value = cookie.get("value")
            if name and value:
                keep.append(f"{name}={value}")
        return "; ".join(keep)

    def _load_saved_cookie(self) -> str:
        if self._force_password_login:
            logger.info("HDHive browser: 已开启强制账号密码登录，本轮不使用任何 Cookie")
            self._cookie = ""
            return ""
        candidates = []
        # P115StrmHelper already maintains a working HDHive cookie in this MP environment.
        # Prefer it over the plugin's saved/configured cookie because stale configured cookies can still
        # open the public detail page but hide all resource cards, producing a misleading zero-result search.
        skip_external_cookie = bool(getattr(self, "_skip_external_cookie_once", False))
        if skip_external_cookie:
            logger.info("HDHive browser: 上次 Cookie 已失效，本轮跳过 P115StrmHelper/缓存/配置 Cookie，改用账号密码重新登录")
            self._skip_external_cookie_once = False
        else:
            strmhelper_cookie = self._load_strmhelper_cookie()
            if strmhelper_cookie:
                candidates.append(("P115StrmHelper", strmhelper_cookie))
        if self._get_data and not skip_external_cookie:
            try:
                saved = self._get_data(self.COOKIE_DATA_KEY) or ""
                if saved:
                    candidates.append(("插件缓存", str(saved).strip()))
            except Exception as e:
                logger.debug(f"HDHive browser: failed to load saved cookie: {e}")
        if self._cookie and not skip_external_cookie:
            candidates.append(("配置", self._cookie))

        for source, cookie in candidates:
            cookie = str(cookie or "").strip()
            if cookie and "token=" in cookie:
                self._cookie = cookie
                logger.info(f"HDHive browser: 使用 {source} Cookie")
                return self._cookie
        return self._cookie

    @staticmethod
    def _load_strmhelper_cookie() -> str:
        """Reuse HDHive cookie saved by P115StrmHelper when P115StrgmSub config cookie is stale/missing."""
        cookie_file = Path("/config/plugins/p115strmhelper/hdhive_cookies.json")
        try:
            if not cookie_file.exists():
                return ""
            data = json.loads(cookie_file.read_text(encoding="utf-8"))
            cookie = str(data.get("cookie_str") or "").strip()
            return cookie if "token=" in cookie else ""
        except Exception as e:
            logger.debug(f"HDHive browser: failed to load P115StrmHelper cookie: {e}")
            return ""

    def _save_cookie(self, cookie: str):
        cookie = (cookie or "").strip()
        if not cookie:
            return
        self._cookie = cookie
        if self._save_data:
            try:
                self._save_data(self.COOKIE_DATA_KEY, cookie)
            except Exception as e:
                logger.debug(f"HDHive browser: failed to save cookie: {e}")

    def _clear_runtime_cookie(self):
        self._cookie = ""
        if self._save_data:
            try:
                self._save_data(self.COOKIE_DATA_KEY, "")
            except Exception:
                pass

    def _run_authenticated(self, operation):
        last_error = None
        for attempt in range(2):
            try:
                with self._new_context() as context:
                    self._ensure_login(context)
                    result = operation(context)
                    self._refresh_cookie_from_context(context)
                    return result
            except HDHiveLoginError as e:
                last_error = e
                has_password_fallback = bool(self._username and self._password)
                has_strmhelper_fallback = bool(self._load_strmhelper_cookie())
                can_retry = bool(self._cookie and (has_password_fallback or has_strmhelper_fallback))
                if attempt == 0 and can_retry:
                    fallback = "账号密码重新登录" if has_password_fallback else "P115StrmHelper Cookie"
                    logger.warning(f"HDHive browser: Cookie invalid, retrying with {fallback}")
                    self._clear_runtime_cookie()
                    if has_password_fallback:
                        self._skip_external_cookie_once = True
                    continue
                raise
        if last_error:
            raise last_error
        raise HDHiveBrowserError("HDHive browser authenticated operation failed")

    @staticmethod
    def _browser_backend() -> str:
        if cloak_launch_context:
            return "cloakbrowser"
        if sync_playwright:
            return "playwright"
        raise HDHiveBrowserError(
            "HDHive browser mode needs cloakbrowser or playwright. "
            "Newer MoviePilot builds usually include cloakbrowser."
        )

    @contextmanager
    def _new_context(self) -> Iterator[Any]:
        backend = self._browser_backend()
        if backend == "cloakbrowser":
            proxy_url = None
            raw_proxy = self._proxy or settings.PROXY
            if isinstance(raw_proxy, str):
                proxy_url = raw_proxy
            elif isinstance(raw_proxy, dict):
                proxy_url = raw_proxy.get("https") or raw_proxy.get("http") or raw_proxy.get("server")

            kwargs = {
                "headless": self._headless,
                "humanize": getattr(settings, "CLOAKBROWSER_HUMANIZE", True),
            }
            human_preset = getattr(settings, "CLOAKBROWSER_HUMAN_PRESET", None)
            if human_preset:
                kwargs["human_preset"] = human_preset
            if proxy_url:
                kwargs["proxy"] = proxy_url

            context = cloak_launch_context(**kwargs)
            try:
                yield context
            finally:
                context.close()
            return

        proxy_config = self._parse_proxy(self._proxy or settings.PROXY)
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    headless=self._headless,
                    channel="chromium",
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                    ],
                    proxy=proxy_config,
                )
            except Exception as e:
                raise HDHiveBrowserError(
                    "Playwright Chromium executable is missing. "
                    "Install browser binaries with `playwright install chromium`, "
                    "or use a MoviePilot image that includes cloakbrowser."
                ) from e
            context = browser.new_context(
                locale="zh-CN",
                timezone_id=getattr(settings, "TZ", "Asia/Shanghai"),
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0.0.0 Safari/537.36"
                ),
            )
            try:
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                yield context
            finally:
                try:
                    context.close()
                finally:
                    browser.close()

    def _add_cookies(self, context: Any, cookie: str):
        cookies = []
        for name, value in self._parse_cookie_string(cookie).items():
            cookies.append({
                "name": name,
                "value": value,
                "domain": ".hdhive.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            })
        if cookies:
            context.add_cookies(cookies)

    def _refresh_cookie_from_context(self, context: Any):
        try:
            cookie = self._cookie_string_from_raw(context.cookies(self.BASE_URL))
            if "token=" in cookie:
                self._save_cookie(cookie)
        except Exception:
            pass

    def _ensure_login(self, context: Any):
        cookie = self._load_saved_cookie()
        if cookie:
            self._add_cookies(context, cookie)
            return
        if not self._username or not self._password:
            raise HDHiveLoginError("HDHive browser mode needs Cookie or username/password")

        page = context.new_page()
        self._goto_with_retry(page, f"{self.BASE_URL}/login", "打开登录页", attempts=3, timeout=30000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        if not self._wait_for_any_input(page, timeout_seconds=18):
            try:
                logger.info("HDHive browser: 登录页尚未出现输入框，刷新后重试")
                self._goto_with_retry(page, f"{self.BASE_URL}/login", "刷新登录页", attempts=2, timeout=30000)
                page.wait_for_timeout(3000)
            except Exception:
                pass

        username_selectors = [
            "input[name='username']",
            "input[name='email']",
            "input[type='email']",
            "input[type='text']",
            "#username",
            "#email",
        ]
        password_selectors = ["input[name='password']", "input[type='password']", "#password"]
        if not self._fill_first(page, username_selectors, self._username):
            body = ""
            try:
                body = page.locator("body").inner_text(timeout=3000)[:160]
            except Exception:
                pass
            raise HDHiveLoginError(f"HDHive login page username field was not found; url={page.url}; body={body}")
        if not self._fill_first(page, password_selectors, self._password):
            body = ""
            try:
                body = page.locator("body").inner_text(timeout=3000)[:160]
            except Exception:
                pass
            raise HDHiveLoginError(f"HDHive login page password field was not found; url={page.url}; body={body}")
        button = page.locator("button[type='submit'], button:has-text('登录'), button:has-text('Login'), [role='button']:has-text('登录')")
        if button.count():
            button.first.click()
        else:
            page.keyboard.press("Enter")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        self._refresh_cookie_from_context(context)
        if not self._cookie:
            body = ""
            try:
                body = page.locator("body").inner_text(timeout=3000)[:180]
            except Exception:
                pass
            raise HDHiveLoginError(f"HDHive login did not return a token cookie; url={page.url}; body={body}")
        logger.info("HDHive browser: 账号密码登录成功，已刷新 Cookie")

    @staticmethod
    def _fill_first(page: Any, selectors: List[str], value: str) -> bool:
        for selector in selectors:
            try:
                loc = page.locator(selector)
                if loc.count():
                    loc.first.fill(value)
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _wait_for_any_input(page: Any, timeout_seconds: int = 20) -> bool:
        deadline = time() + max(timeout_seconds, 1)
        while time() < deadline:
            try:
                if page.locator("input").count() > 0:
                    return True
            except Exception:
                pass
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass
        return False

    def _goto_with_retry(self, page: Any, url: str, reason: str, attempts: int = 3, timeout: int = 30000) -> None:
        last_error = ""
        for attempt in range(1, max(attempts, 1) + 1):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                return
            except Exception as e:
                last_error = str(e)
                transient = (
                    "ERR_NETWORK_CHANGED" in last_error
                    or "ERR_CONNECTION" in last_error
                    or "ERR_TIMED_OUT" in last_error
                    or "Timeout" in last_error
                    or "Navigation" in last_error
                    or "navigation" in last_error.lower()
                )
                if not transient or attempt >= max(attempts, 1):
                    logger.error(f"HDHive (Browser) 打开页面失败：{reason}，尝试={attempt}/{attempts}，错误={last_error}")
                    raise
                wait_ms = 1000 * attempt
                logger.warning(f"HDHive (Browser) 打开页面失败，将重试：{reason}，尝试={attempt}/{attempts}，错误={last_error}")
                try:
                    page.wait_for_timeout(wait_ms)
                except Exception:
                    pass
        if last_error:
            raise HDHiveBrowserError(last_error)

    @staticmethod
    def _slug_from_resource(resource: Dict[str, Any]) -> str:
        for key in ("slug", "id", "uuid"):
            value = resource.get(key)
            if value:
                return str(value)
        href = str(resource.get("href") or resource.get("url") or "")
        match = re.search(r"/resource/115/([^/?#]+)", href)
        if match:
            return match.group(1)
        parsed = urlparse(href)
        if parsed.path:
            tail = parsed.path.rstrip("/").split("/")[-1]
            if tail and tail not in ("115", "resource"):
                return tail
        return ""

    @staticmethod
    def _int_or_none(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except Exception:
            match = re.search(r"\d+", str(value))
            return int(match.group(0)) if match else None

    @staticmethod
    def _is_115_resource(resource: Dict[str, Any]) -> bool:
        href = str(resource.get("href") or resource.get("url") or "")
        if "/resource/115/" in href:
            return True
        for key in ("pan_type", "website", "source", "type"):
            value = resource.get(key)
            if isinstance(value, dict):
                value = value.get("value") or value.get("name")
            if value and "115" in str(value):
                return True
        text = str(resource.get("title") or "") + " " + str(resource.get("content") or "")
        return "115" in href or "115" in text

    @staticmethod
    def _looks_like_resource(resource: Dict[str, Any]) -> bool:
        keys = (
            "size",
            "resolution",
            "video_resolution",
            "share_size",
            "source",
            "slug",
            "unlock_points",
            "href",
            "title",
        )
        return any(key in resource for key in keys)

    @classmethod
    def _extract_resource_items(cls, payload: Any) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []

        def walk(value: Any):
            if isinstance(value, list):
                dicts = [item for item in value if isinstance(item, dict)]
                if dicts and any(cls._looks_like_resource(item) for item in dicts[:3]):
                    found.extend(dicts)
                    return
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                for key in ("data", "resources", "list", "items", "results", "records"):
                    if key in value:
                        walk(value.get(key))

        walk(payload)
        return found

    @staticmethod
    def _scrape_cards_script() -> str:
        return r"""
        () => {
            const sizeRe = /(\d+\.?\d*)\s*(TB|GB|MB|G(?!B)|M(?!B))\b/i;
            const pointsRe = /(\d+)\s*\u79ef\u5206/;
            const dateRe = /\u53d1\u5e03\u4e8e\s*([\d/\-]+)/;
            const resRe = /\b(4K|8K|2K|1080[piP]?|720[piP]?|480[piP]?)/i;
            const candidates = [];
            for (const el of document.querySelectorAll('a,div,article,li,section')) {
                const text = el.innerText || '';
                if (!text.includes('\u53d1\u5e03\u4e8e') || !sizeRe.test(text)) continue;
                if ((text.match(/\u53d1\u5e03\u4e8e/g) || []).length !== 1) continue;
                if (text.length < 30 || text.length > 5000) continue;
                candidates.push(el);
            }
            const minimal = candidates.filter(
                el => !candidates.some(other => other !== el && el.contains(other))
            );
            const metaTerms = new Set([
                '4K','8K','2K','免费','官组','管理员','WEB-DL','WEBRip','BDRip','REMUX','HDTV',
                '简中','繁中','简英','繁英','内封','外挂','内嵌','简日','繁日','简韩','繁韩',
                '1080P','1080p','720P','720p','480P','480p','蓝光原盘','ISO','加入片单'
            ]);
            const cards = minimal.map(card => {
                const text = card.innerText || '';
                const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
                const dateMatch = text.match(dateRe);
                const sizeMatch = text.match(sizeRe);
                const resMatch = text.match(resRe);
                const pointsMatch = text.match(pointsRe);
                const isFree = text.includes('免费') || !pointsMatch;
                const tags = [];
                if (text.includes('官组') || text.includes('管理员')) tags.push('官组');
                if (isFree) tags.push('免费');
                if (pointsMatch) tags.push(pointsMatch[0].trim());
                const dateLineIdx = lines.findIndex(l => /发布于/.test(l));
                const user = dateLineIdx > 0 ? lines[dateLineIdx - 1] : (lines[0] || '');
                const titleLines = lines.filter(l => {
                    if (l.length < 3) return false;
                    if (metaTerms.has(l)) return false;
                    if (/^发布于/.test(l)) return false;
                    if (/^\d+\s*积分$/.test(l)) return false;
                    if (/^\d+\.?\d*\s*(T?B|G[Bi]?|M[Bi]?)$/i.test(l)) return false;
                    if (l === user) return false;
                    return true;
                });
                let title = titleLines
                    .map(l => l.replace(/^\d+\s*积分\s*/, '').trim())
                    .filter(Boolean).join(' ').trim();
                let hrefEl = card;
                while (hrefEl && hrefEl.tagName !== 'A') hrefEl = hrefEl.parentElement;
                let href = hrefEl ? (hrefEl.getAttribute('href') || '') : '';
                if (!href) {
                    const childLink = card.querySelector('a[href*="/resource/115/"]');
                    href = childLink ? (childLink.getAttribute('href') || '') : '';
                }
                return {
                    user,
                    title,
                    href,
                    posted_at: dateMatch ? dateMatch[1] : '',
                    created_at: dateMatch ? dateMatch[1] : '',
                    tags,
                    resolution: resMatch ? resMatch[1] : '',
                    size: sizeMatch ? (sizeMatch[1] + ' ' + sizeMatch[2].toUpperCase()) : '',
                    is_free: isFree,
                    unlock_points: isFree ? 0 : (pointsMatch ? parseInt(pointsMatch[1]) : null),
                    pan_type: href.includes('/resource/115/') ? '115' : '',
                    is_official: text.includes('官组') || text.includes('管理员')
                };
            }).filter(item => item.href && item.href.includes('/resource/115/'));
            const seen = new Set();
            return cards.filter(item => {
                const key = item.href || item.title || JSON.stringify(item);
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
            });
        }
        """

    def get_resources(self, media_type: str, tmdb_id: Any) -> List[Dict[str, Any]]:
        media_type = "movie" if str(media_type).lower() == "movie" else "tv"
        detail_url = f"{self.BASE_URL}/tmdb/{media_type}/{tmdb_id}"
        def _operation(context: Any) -> List[Dict[str, Any]]:
            page = context.new_page()
            captured: List[Dict[str, Any]] = []
            captured_urls = set()

            def on_response(response: Any):
                try:
                    if response.status != 200:
                        return
                    content_type = response.headers.get("content-type", "")
                    if "json" not in content_type:
                        return
                    body = response.json()
                    items = self._extract_resource_items(body)
                    if items:
                        captured.extend(items)
                        captured_urls.add(response.url)
                except Exception:
                    pass

            page.on("response", on_response)
            logger.info(f"HDHive (Browser) 访问资源页: {detail_url}")
            self._goto_with_retry(page, detail_url, "打开TMDB资源页", attempts=3, timeout=30000)
            page.wait_for_timeout(1500)
            logger.info(f"HDHive (Browser) 当前页面: {page.url}")
            if "/login" in page.url:
                raise HDHiveLoginError("HDHive cookie was redirected to login page")

            try:
                dismiss = page.locator("button:has-text('我知道了')")
                dismiss.first.wait_for(state="visible", timeout=1500)
                dismiss.first.click()
                logger.info("HDHive (Browser) 已关闭提示弹窗")
            except Exception:
                pass

            clicked_115_tab = False
            try:
                tab = page.locator("button:has-text('115网盘'), [role='tab']:has-text('115网盘'), button:has-text('115'), [role='tab']:has-text('115')")
                tab.first.wait_for(state="visible", timeout=10000)
                tab.first.click()
                clicked_115_tab = True
                page.wait_for_timeout(1000)
            except Exception:
                try:
                    clicked_115_tab = bool(page.evaluate("""
                    () => {
                        for (const el of document.querySelectorAll('button,[role="tab"]')) {
                            if ((el.innerText || '').includes('115')) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }
                    """))
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
            logger.info(f"HDHive (Browser) 115标签点击结果: {'成功' if clicked_115_tab else '未确认'}")

            # 部分 HDHive/MUI 页面中 115 标签已经默认选中，但文字由图标+多行文本组成，
            # Playwright 的 has-text('115网盘') 不一定能确认点击；这里先做一次 DOM 兜底解析，
            # 避免在已有资源卡片的页面上空等接口响应导致返回 0。
            try:
                pre_resources = page.evaluate(self._scrape_cards_script()) or []
                if pre_resources:
                    logger.info(f"HDHive (Browser) 标签点击后 DOM 预解析资源: {len(pre_resources)} 条")
                    captured.extend(pre_resources)
            except Exception as e:
                logger.debug(f"HDHive (Browser) DOM 预解析失败: {e}")

            deadline = time() + 8
            while time() < deadline and not captured:
                page.wait_for_timeout(250)

            if captured:
                logger.info(f"HDHive (Browser) 接口捕获资源: {len(captured)} 条，来源接口数: {len(captured_urls)}")
                resources = captured
            else:
                logger.info("HDHive (Browser) 接口未捕获资源，开始 DOM 兜底解析")
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                resources = page.evaluate(self._scrape_cards_script()) or []
                logger.info(f"HDHive (Browser) DOM 兜底解析资源: {len(resources)} 条")

            normalized = []
            skipped_no_slug = 0
            skipped_non_115 = 0
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                slug = self._slug_from_resource(resource)
                href = resource.get("href") or resource.get("url") or ""
                if not slug and not self._is_115_resource(resource):
                    skipped_non_115 += 1
                    continue
                if not slug:
                    skipped_no_slug += 1
                    continue
                title = (
                    resource.get("title")
                    or resource.get("name")
                    or resource.get("resource_name")
                    or ""
                )
                points = self._int_or_none(resource.get("unlock_points") or resource.get("points"))
                is_free = bool(resource.get("is_unlocked")) or bool(resource.get("is_free")) or points in (None, 0)
                normalized.append({
                    "title": str(title),
                    "slug": slug,
                    "href": href,
                    "unlock_points": 0 if is_free else points,
                    "is_free": is_free,
                    "is_unlocked": bool(resource.get("is_unlocked")),
                    "is_official": bool(resource.get("is_official") or resource.get("official")),
                    "created_at": resource.get("created_at") or resource.get("posted_at") or "",
                })
            logger.info(
                f"HDHive (Browser) 规范化资源: {len(normalized)} 条，"
                f"跳过非115/无关: {skipped_non_115}，跳过缺少slug: {skipped_no_slug}"
            )
            return normalized
        return self._run_authenticated(_operation)

    @staticmethod
    def _extract_115_url(value: str) -> str:
        text = value or ""
        patterns = [
            r"https?://(?:115cdn|anxia)\.com/s/[A-Za-z0-9]+(?:\?password=[A-Za-z0-9]{4})?",
            r"https?://115\.com/s/[A-Za-z0-9]+(?:\?password=[A-Za-z0-9]{4})?",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(0).rstrip(" \n\r\t\"'<>")
        return ""

    @classmethod
    def _extract_115_url_from_json(cls, value: Any) -> str:
        """Recursively find a 115 share URL in arbitrary JSON returned by HDHive."""
        if value is None:
            return ""
        if isinstance(value, str):
            return cls._extract_115_url(value)
        if isinstance(value, dict):
            preferred_keys = ("full_url", "url", "link", "resource_url", "share_url", "shareUrl", "content", "text")
            for key in preferred_keys:
                if key in value:
                    found = cls._extract_115_url_from_json(value.get(key))
                    if found:
                        return found
            for item in value.values():
                found = cls._extract_115_url_from_json(item)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = cls._extract_115_url_from_json(item)
                if found:
                    return found
        return ""

    @staticmethod
    def _page_is_login(page: Any) -> bool:
        try:
            return "/login" in str(page.url or "")
        except Exception:
            return False

    def unlock_resource(self, slug: str) -> Dict[str, Any]:
        if not slug:
            raise HDHiveBrowserError("Missing HDHive resource slug")
        def _operation(context: Any) -> Dict[str, Any]:
            page = context.new_page()
            captured_url = ""
            resource_url = f"{self.BASE_URL}/resource/115/{slug}"

            def on_response(response: Any):
                nonlocal captured_url
                try:
                    if response.status != 200:
                        return
                    content_type = response.headers.get("content-type", "")
                    if "json" not in content_type:
                        return
                    found = self._extract_115_url_from_json(response.json())
                    if found:
                        captured_url = found
                except Exception:
                    pass

            page.on("response", on_response)

            extract_script = r"""
            () => {
                const textParts = [];
                const push = (value) => {
                    value = ((value || '') + '').trim();
                    if (value) textParts.push(value);
                };
                for (const el of document.querySelectorAll('input,textarea,a,code,pre,div,span,p,button')) {
                    push(el.value);
                    push(el.href);
                    push(el.getAttribute && el.getAttribute('data-clipboard-text'));
                    push(el.getAttribute && el.getAttribute('data-url'));
                    push(el.getAttribute && el.getAttribute('data-link'));
                    push(el.textContent);
                }
                if (document.body) push(document.body.innerText);
                if (document.documentElement) push(document.documentElement.innerHTML);
                const joined = textParts.join('\n');
                const match = joined.match(/https?:\/\/(115cdn|anxia)\.com\/s\/[A-Za-z0-9]+(?:\?password=[A-Za-z0-9]{4})?|https?:\/\/115\.com\/s\/[A-Za-z0-9]+(?:\?password=[A-Za-z0-9]{4})?/);
                return match ? match[0] : '';
            }
            """

            click_script = r"""
            () => {
                const keywords = ['获取链接', '查看链接', '复制链接', '打开链接', '网盘链接', '115链接', '确认解锁', '解锁', '获取', '查看', '复制'];
                const nodes = Array.from(document.querySelectorAll('button,[role="button"],a'));
                for (const el of nodes) {
                    const text = ((el.innerText || el.textContent || el.getAttribute('aria-label') || '') + '').trim();
                    if (!text) continue;
                    if (keywords.some(k => text.includes(k))) {
                        el.click();
                        return text;
                    }
                }
                return '';
            }
            """

            def wait_page_stable(reason: str, timeout_ms: int = 10000) -> None:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                except Exception:
                    pass
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
                except Exception:
                    pass
                try:
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                logger.debug(f"HDHive (Browser) 页面稳定等待完成：{reason}，当前页面={getattr(page, 'url', '')}")

            def safe_extract_url(timeout_seconds: int = 20, stage: str = "取链接") -> str:
                deadline = time() + timeout_seconds
                last_error = ""
                retry_notified = False
                while time() < deadline:
                    if captured_url:
                        return self._extract_115_url(captured_url)
                    if self._page_is_login(page):
                        raise HDHiveLoginError("HDHive cookie was redirected to login page")
                    try:
                        value = page.evaluate(extract_script)
                        url = self._extract_115_url(value)
                        if url:
                            return url
                    except Exception as e:
                        last_error = str(e)
                        if "Execution context was destroyed" in last_error or "navigation" in last_error.lower():
                            if not retry_notified:
                                logger.info(f"HDHive (Browser) {stage}时页面发生跳转，等待页面稳定后重试")
                                retry_notified = True
                            wait_page_stable(stage, timeout_ms=6000)
                        else:
                            logger.debug(f"HDHive (Browser) {stage}失败，将重试: {e}")
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        pass
                if last_error:
                    logger.debug(f"HDHive (Browser) {stage}超时，最后错误: {last_error}")
                return ""

            def goto_resource(reason: str) -> None:
                logger.info(f"HDHive (Browser) 打开资源详情：{slug}（{reason}）")
                self._goto_with_retry(page, resource_url, reason, attempts=3, timeout=30000)
                wait_page_stable(reason, timeout_ms=12000)
                if self._page_is_login(page):
                    raise HDHiveLoginError("HDHive cookie was redirected to login page")

            def try_click_link_button(stage: str) -> bool:
                try:
                    clicked_text = page.evaluate(click_script) or ""
                    if clicked_text:
                        logger.info(f"HDHive (Browser) {stage}：已点击“{clicked_text}”按钮，等待115链接出现")
                        wait_page_stable(stage, timeout_ms=8000)
                        return True
                except Exception as e:
                    logger.debug(f"HDHive (Browser) {stage}点击按钮失败: {e}")
                return False

            goto_resource("首次进入")

            for attempt in range(1, 4):
                url = safe_extract_url(timeout_seconds=8 if attempt == 1 else 12, stage=f"第{attempt}次直接提取115链接")
                if url:
                    logger.info(f"HDHive (Browser) 已直接获得115链接：slug={slug}，尝试={attempt}")
                    return {"url": url, "full_url": url, "already_owned": True}
                if try_click_link_button(f"第{attempt}次尝试获取/解锁链接"):
                    url = safe_extract_url(timeout_seconds=18, stage=f"第{attempt}次点击后提取115链接")
                    if url:
                        logger.info(f"HDHive (Browser) 点击后获得115链接：slug={slug}，尝试={attempt}")
                        return {"url": url, "full_url": url, "already_owned": attempt == 1}
                if attempt < 3:
                    try:
                        logger.info(f"HDHive (Browser) 未取到115链接，重新进入资源页重试：slug={slug}，下一次={attempt + 1}")
                        goto_resource(f"第{attempt + 1}次重进")
                    except Exception as e:
                        logger.debug(f"HDHive (Browser) 重新进入资源页失败: {e}")

            current_url = ""
            page_text = ""
            try:
                current_url = str(page.url or "")
                page_text = str(page.evaluate("() => document.body ? document.body.innerText.slice(0, 300) : ''") or "")
            except Exception:
                pass
            raise HDHiveBrowserError(
                "HDHive did not expose a 115 URL after retries; "
                f"current_url={current_url}, page_text={page_text[:120]}"
            )
        return self._run_authenticated(_operation)
