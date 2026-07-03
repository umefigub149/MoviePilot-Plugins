"""
Minimal HDHive browser client.

This client intentionally avoids the old bundled ``lib.hdhive`` dependency.
It uses the public HDHive web pages with Playwright, keeps the same resource
shape expected by SearchHandler, and leaves all transfer work to the existing
115 client.
"""
import re
from contextlib import contextmanager
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
    ):
        self._cookie = (cookie or "").strip()
        self._username = (username or "").strip()
        self._password = password or ""
        self._proxy = proxy
        self._get_data = get_data_func
        self._save_data = save_data_func
        self._headless = headless

    @property
    def is_ready(self) -> bool:
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
        if self._cookie:
            return self._cookie
        if self._get_data:
            try:
                saved = self._get_data(self.COOKIE_DATA_KEY) or ""
                if saved:
                    self._cookie = str(saved).strip()
            except Exception as e:
                logger.debug(f"HDHive browser: failed to load saved cookie: {e}")
        return self._cookie

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
                can_retry_with_password = bool(self._cookie and self._username and self._password)
                if attempt == 0 and can_retry_with_password:
                    logger.warning("HDHive browser: Cookie invalid, retrying with username/password")
                    self._clear_runtime_cookie()
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
        page.goto(f"{self.BASE_URL}/login", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)
        username_selectors = [
            "input[name='username']",
            "input[name='email']",
            "input[type='email']",
            "#username",
        ]
        password_selectors = ["input[name='password']", "input[type='password']", "#password"]
        if not self._fill_first(page, username_selectors, self._username):
            raise HDHiveLoginError("HDHive login page username field was not found")
        if not self._fill_first(page, password_selectors, self._password):
            raise HDHiveLoginError("HDHive login page password field was not found")
        button = page.locator("button[type='submit'], button:has-text('登录'), button:has-text('Login')")
        if button.count():
            button.first.click()
        else:
            page.keyboard.press("Enter")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(3000)
        self._refresh_cookie_from_context(context)
        if not self._cookie:
            raise HDHiveLoginError("HDHive login did not return a token cookie")

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
            const sizeRe = /(\d+\.?\d*)\s*(TB|GB|MB|G(?!B)|M(?!B))/i;
            const pointsRe = /(\d+)\s*\u79ef\u5206/;
            const dateRe = /\u53d1\u5e03\u4e8e\s*([\d/\-]+)/;
            const resRe = /\b(4K|8K|2K|1080[piP]?|720[piP]?|480[piP]?)\b/;
            const candidates = [];
            for (const el of document.querySelectorAll('a,div,article,li,section')) {
                const text = el.innerText || '';
                if (text.length < 20 || text.length > 5000) continue;
                if (!sizeRe.test(text)) continue;
                if (!text.includes('\u53d1\u5e03\u4e8e') && !text.includes('\u79ef\u5206') && !text.includes('\u514d\u8d39')) continue;
                let hrefEl = el;
                while (hrefEl && hrefEl.tagName !== 'A') hrefEl = hrefEl.parentElement;
                let href = hrefEl ? (hrefEl.getAttribute('href') || '') : '';
                if (!href) {
                    const childLink = el.querySelector('a[href*="/resource/"]');
                    href = childLink ? (childLink.getAttribute('href') || '') : '';
                }
                if (href && !href.includes('/resource/115/') && !href.includes('/resource/')) continue;
                candidates.push({el, text, href});
            }

            const minimal = candidates.filter(
                item => !candidates.some(other => other.el !== item.el && item.el.contains(other.el))
            );

            const metaTerms = new Set([
                '4K','8K','2K','WEB-DL','WEBRip','BDRip','REMUX','HDTV',
                '\u514d\u8d39','\u5b98\u7ec4','\u7ba1\u7406\u5458',
                '1080P','1080p','720P','720p','480P','480p'
            ]);

            const cards = [];
            for (const item of minimal) {
                const text = item.text;
                const href = item.href;
                const lines = text.split('\n').map(v => v.trim()).filter(Boolean);
                const points = text.match(pointsRe);
                const date = text.match(dateRe);
                const size = text.match(sizeRe);
                const resolution = text.match(resRe);
                const isFree = text.includes('\u514d\u8d39') || !points;
                const title = lines
                    .filter(line => !line.includes('\u53d1\u5e03\u4e8e'))
                    .filter(line => !/^\d+\s*\u79ef\u5206$/.test(line))
                    .filter(line => !/^\d+\.?\d*\s*(TB|GB|MB|G|M)$/i.test(line))
                    .filter(line => !metaTerms.has(line))
                    .slice(0, 3)
                    .join(' ')
                    .trim();
                cards.push({
                    title,
                    href,
                    is_free: isFree,
                    unlock_points: isFree ? 0 : parseInt(points[1]),
                    created_at: date ? date[1] : '',
                    size: size ? (size[1] + ' ' + size[2].toUpperCase()) : '',
                    resolution: resolution ? resolution[1] : '',
                    pan_type: '115',
                    is_official: text.includes('\u5b98\u7ec4') || text.includes('\u7ba1\u7406\u5458')
                });
            }
            const seen = new Set();
            return cards.filter(item => {
                const key = (item.href || '') + '|' + (item.title || '');
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
            page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
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
        match = re.search(r"https?://(?:115cdn|115)\.com/\S+", value or "")
        return match.group(0).rstrip(" \n\r\t\"'<>") if match else ""

    def unlock_resource(self, slug: str) -> Dict[str, Any]:
        if not slug:
            raise HDHiveBrowserError("Missing HDHive resource slug")
        def _operation(context: Any) -> Dict[str, Any]:
            page = context.new_page()
            captured_url = ""

            def on_response(response: Any):
                nonlocal captured_url
                try:
                    if response.status != 200:
                        return
                    if "json" not in response.headers.get("content-type", ""):
                        return
                    body = response.json()
                    data = body.get("data") if isinstance(body, dict) else None
                    if isinstance(data, dict):
                        for key in ("full_url", "url", "link", "resource_url", "share_url"):
                            found = self._extract_115_url(str(data.get(key) or ""))
                            if found:
                                captured_url = found
                                return
                except Exception:
                    pass

            page.on("response", on_response)
            page.goto(f"{self.BASE_URL}/resource/115/{slug}", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            if "/login" in page.url:
                raise HDHiveLoginError("HDHive cookie was redirected to login page")

            extract_script = r"""
            () => {
                const re = /^https?:\/\/(115cdn|115)\.com\//;
                for (const el of document.querySelectorAll('input,textarea')) {
                    const value = (el.value || '').trim();
                    if (re.test(value)) return value;
                }
                const text = document.body ? document.body.innerText || '' : '';
                const match = text.match(/https?:\/\/(115cdn|115)\.com\/\S+/);
                return match ? match[0] : '';
            }
            """
            existing = page.evaluate(extract_script)
            if existing:
                url = self._extract_115_url(existing)
                return {"url": url, "full_url": url, "already_owned": True}

            confirm = page.locator(
                "button:has-text('\u786e\u5b9a\u89e3\u9501'), "
                "button:has-text('\u89e3\u9501'), "
                "[role='button']:has-text('\u786e\u5b9a\u89e3\u9501')"
            )
            if not confirm.count():
                raise HDHiveBrowserError("HDHive unlock button was not found")
            confirm.first.click()

            deadline = time() + 20
            url = ""
            while time() < deadline:
                url = captured_url or self._extract_115_url(page.evaluate(extract_script))
                if url:
                    break
                page.wait_for_timeout(500)

            if not url:
                raise HDHiveBrowserError("HDHive unlock did not expose a 115 URL")
            return {"url": url, "full_url": url, "already_owned": False}
        return self._run_authenticated(_operation)
