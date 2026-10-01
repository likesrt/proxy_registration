import os
import sys
import time
import uuid
import random
import logging
import asyncio
from typing import Optional, Union
import argparse
from quart import Quart, request, jsonify
from camoufox.async_api import AsyncCamoufox
from patchright.async_api import async_playwright
from db_results import init_db, save_result, load_result, cleanup_old_results
from browser_configs import browser_config
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box



COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}


class CustomLogger(logging.Logger):
    @staticmethod
    def format_message(level, color, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{COLORS.get(color)}{level}{COLORS.get('RESET')}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', 'MAGENTA', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', 'BLUE', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', 'GREEN', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', 'YELLOW', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', 'RED', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger: CustomLogger = logging.getLogger("TurnstileAPIServer")  # type: ignore
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)


class TurnstileAPIServer:

    def __init__(self, headless: bool, useragent: Optional[str], debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool = False, browser_name: Optional[str] = None, browser_version: Optional[str] = None, idle_timeout: float = 600.0):
        """
        初始化 Solver 服务。

        参数:
            headless / useragent / debug / browser_type / proxy_support: 见命令行参数。
            thread: 浏览器池大小（= 可并发求解的任务数）。本项目注册流程串行，取 1 即可。
            use_random_config / browser_name / browser_version: chromium 系 UA 配置来源。
            idle_timeout: 空闲多少秒后关闭浏览器释放内存；>0 同时启用懒加载
                （启动时不建浏览器，首个请求才建）；<=0 保持旧行为（启动即建、永不释放）。

        副作用: 仅设置状态与注册路由，不启动浏览器（浏览器在 _startup 或首个请求时创建）。
        """
        self.app = Quart(__name__)
        self.debug = debug
        self.browser_type = browser_type
        self.headless = headless
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        self.use_random_config = use_random_config
        self.browser_name = browser_name
        self.browser_version = browser_version
        self.console = Console()

        # 浏览器按需启停：内存几乎全在浏览器上（每个 camoufox/Firefox 实例数百 MB），
        # 而注册流程是串行的、两轮求解之间可能隔着几分钟（验证邮件窗口最长 240s）。
        # idle_timeout > 0：启动时不起浏览器，首个请求才起；空闲超过该秒数即关闭并释放内存。
        # idle_timeout <= 0：保持旧行为（启动即起、永不释放）。
        self.idle_timeout = float(idle_timeout)
        self.lazy_browsers = self.idle_timeout > 0
        self._pool_lock = asyncio.Lock()      # 保证同一时刻只有一次浏览器创建/释放
        self._browsers_ready = False
        self._next_browser_index = 0          # 单调递增，补齐时不会与在池中的索引重复
        self._inflight = 0                    # 正在求解的任务数，>0 时不做空闲释放
        self._last_activity = time.monotonic()
        self._playwright = None
        self._camoufox = None

        # Initialize useragent and sec_ch_ua attributes
        self.useragent = useragent
        self.sec_ch_ua = None


        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            if browser_name and browser_version:
                config = browser_config.get_browser_config(browser_name, browser_version)
                if config:
                    useragent, sec_ch_ua = config
                    self.useragent = useragent
                    self.sec_ch_ua = sec_ch_ua
            elif useragent:
                self.useragent = useragent
            else:
                browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                self.browser_name = browser
                self.browser_version = version
                self.useragent = useragent
                self.sec_ch_ua = sec_ch_ua

        self.browser_args = []
        if self.useragent:
            self.browser_args.append(f"--user-agent={self.useragent}")

        self._setup_routes()

    def display_welcome(self):
        """Displays welcome screen with logo."""
        self.console.clear()

        combined_text = Text()
        combined_text.append("\n📢 Channel: ", style="bold white")
        combined_text.append("https://t.me/D3_vin", style="cyan")
        combined_text.append("\n💬 Chat: ", style="bold white")
        combined_text.append("https://t.me/D3vin_chat", style="cyan")
        combined_text.append("\n📁 GitHub: ", style="bold white")
        combined_text.append("https://github.com/D3-vin", style="cyan")
        combined_text.append("\n📁 Version: ", style="bold white")
        combined_text.append("1.2a", style="green")
        combined_text.append("\n")

        info_panel = Panel(
            Align.left(combined_text),
            title="[bold blue]Turnstile Solver[/bold blue]",
            subtitle="[bold magenta]Dev by D3vin[/bold magenta]",
            box=box.ROUNDED,
            border_style="bright_blue",
            padding=(0, 1),
            width=50
        )

        self.console.print(info_panel)
        self.console.print()




    def _setup_routes(self) -> None:
        """Set up the application routes."""
        self.app.before_serving(self._startup)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)
        self.app.route('/')(self.index)


    async def _startup(self) -> None:
        """Initialize the browser and page pool on startup."""
        self.display_welcome()
        try:
            await init_db()

            if self.lazy_browsers:
                # 懒加载：此处不起浏览器（省内存），首个 /turnstile 请求时再起。
                # 健康检查打的是 '/'（静态页面），不依赖浏览器，所以容器照样能报 healthy。
                logger.info(
                    f"懒加载模式：浏览器将在首个请求时启动，"
                    f"空闲 {self.idle_timeout:.0f}s 后自动关闭释放内存"
                )
            else:
                logger.info("Starting browser initialization")
                await self._initialize_browser()

            # Запускаем периодическую очистку старых результатов
            asyncio.create_task(self._periodic_cleanup())

            if self.lazy_browsers:
                asyncio.create_task(self._idle_watchdog())

        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
        """
        创建（或补齐）浏览器池，池大小为 ``thread_count``。

        与旧实现的区别：playwright / camoufox 句柄保存在 ``self`` 上，可被
        ``_release_browsers()`` 关闭后再次创建；且只补齐到 ``thread_count`` 个，
        因此某个浏览器断开被 ``_return_browser_to_pool`` 丢弃后，
        下一次 ``_ensure_pool()`` 会自动补上，不会让池慢慢变空。
        """
        # 浏览器驱动句柄：已存在则复用（懒加载释放后会置回 None，从而重新创建）
        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            playwright = self._playwright
            camoufox = None
        elif self.browser_type == "camoufox":
            if self._camoufox is None:
                self._camoufox = AsyncCamoufox(headless=self.headless)
            camoufox = self._camoufox
            playwright = None
        else:
            playwright = None
            camoufox = None

        missing = self.thread_count - self.browser_pool.qsize()
        if missing <= 0:
            self._browsers_ready = True
            return

        browser_configs = self._build_browser_configs(missing)
        await self._launch_browsers(browser_configs, playwright, camoufox)

        self._browsers_ready = True
        logger.info(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")

        if self.use_random_config:
            logger.info(f"Each browser in pool received random configuration")
        elif self.browser_name and self.browser_version:
            logger.info(f"All browsers using configuration: {self.browser_name} {self.browser_version}")
        else:
            logger.info("Using custom configuration")

        if self.debug:
            for i, config in enumerate(browser_configs):
                logger.debug(f"Browser {i+1} config: {config['browser_name']} {config['browser_version']}")
                logger.debug(f"Browser {i+1} User-Agent: {config['useragent']}")
                logger.debug(f"Browser {i+1} Sec-CH-UA: {config['sec_ch_ua']}")

    def _build_browser_configs(self, count: int) -> list:
        """
        构造 ``count`` 份浏览器配置（UA / sec-ch-ua / 版本）。

        参数:
            count: 需要补齐的浏览器数量。

        返回:
            配置字典列表，长度等于 ``count``。

        说明: 分支完全沿用原有逻辑——chromium 系支持 ``--random`` 与
        ``--browser/--version`` 指定；camoufox 使用默认配置。
        """
        configs = []
        for _ in range(count):
            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                if self.use_random_config:
                    browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                elif self.browser_name and self.browser_version:
                    config = browser_config.get_browser_config(self.browser_name, self.browser_version)
                    if config:
                        useragent, sec_ch_ua = config
                        browser = self.browser_name
                        version = self.browser_version
                    else:
                        browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                else:
                    browser = getattr(self, 'browser_name', 'custom')
                    version = getattr(self, 'browser_version', 'custom')
                    useragent = self.useragent
                    sec_ch_ua = getattr(self, 'sec_ch_ua', '')
            else:
                # Для camoufox и других браузеров используем значения по умолчанию
                browser = self.browser_type
                version = 'custom'
                useragent = self.useragent
                sec_ch_ua = getattr(self, 'sec_ch_ua', '')

            configs.append({
                'browser_name': browser,
                'browser_version': version,
                'useragent': useragent,
                'sec_ch_ua': sec_ch_ua
            })
        return configs

    async def _launch_browsers(self, browser_configs: list, playwright, camoufox) -> None:
        """
        按配置逐个启动浏览器并放入池中。

        参数:
            browser_configs: ``_build_browser_configs`` 的结果。
            playwright / camoufox: 驱动句柄；未启动对应浏览器类型时传 None。

        返回:
            无。启动成功的浏览器进池；``browser`` 为 None 的（驱动缺失）跳过。

        副作用: 启动浏览器进程（昂贵，单实例数百 MB）；索引用单调计数器分配，
        避免补齐时与池中已有索引重复。
        """
        for config in browser_configs:
            browser_args = [
                "--window-position=0,0",
                "--force-device-scale-factor=1"
            ]
            if config['useragent']:
                browser_args.append(f"--user-agent={config['useragent']}")

            browser = None
            if self.browser_type in ['chromium', 'chrome', 'msedge'] and playwright:
                browser = await playwright.chromium.launch(
                    channel=self.browser_type,
                    headless=self.headless,
                    args=browser_args
                )
            elif self.browser_type == "camoufox" and camoufox:
                browser = await camoufox.start()

            if browser:
                self._next_browser_index += 1
                await self.browser_pool.put(
                    (self._next_browser_index, browser, config)
                )

            if self.debug:
                logger.info(
                    f"Browser {self._next_browser_index} initialized successfully "
                    f"with {config['browser_name']} {config['browser_version']}"
                )

    async def _periodic_cleanup(self):
        """Periodic cleanup of old results every hour"""
        while True:
            try:
                await asyncio.sleep(3600)
                deleted_count = await cleanup_old_results(days_old=7)
                if deleted_count > 0:
                    logger.info(f"Cleaned up {deleted_count} old results")
            except Exception as e:
                logger.error(f"Error during periodic cleanup: {e}")

    async def _ensure_pool(self) -> None:
        """
        确保浏览器池可用（懒加载的唯一入口）。

        池已就绪且数量足够时直接返回；否则创建/补齐浏览器。用 ``_pool_lock``
        串行化，避免多个请求同时到达时重复创建（会把内存翻倍）。

        同时修掉一个既有问题：``_return_browser_to_pool`` 在浏览器断开时不再放回池，
        旧实现下池只减不增，耗尽后 ``browser_pool.get()`` 会永久阻塞；现在每次
        求解前都会补齐到 ``thread_count``。

        副作用: 可能启动浏览器（冷启动数秒到数十秒），并写出日志。
        """
        async with self._pool_lock:
            if self._browsers_ready and self.browser_pool.qsize() >= self.thread_count:
                return
            if self.browser_pool.qsize() < self.thread_count:
                logger.info(
                    f"准备浏览器：池内 {self.browser_pool.qsize()}/{self.thread_count}，"
                    f"正在创建（首次请求会有冷启动等待）"
                )
                await self._initialize_browser()

    async def _release_browsers(self) -> None:
        """
        关闭池内全部浏览器并释放内存（空闲回收）。

        调用前提: ``_inflight == 0``，即没有正在求解的任务——否则会关掉任务正在用的浏览器。
        释放后 ``_browsers_ready`` 复位，下次请求由 ``_ensure_pool`` 重新创建。
        逐个 close 并吞掉单个失败，保证一个浏览器关不掉不会阻断整体释放。
        """
        async with self._pool_lock:
            closed = 0
            while not self.browser_pool.empty():
                try:
                    _index, browser, _config = self.browser_pool.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await browser.close()
                    closed += 1
                except Exception as e:
                    logger.warning(f"关闭浏览器失败（忽略）: {str(e)[:120]}")

            # 驱动句柄也要关，否则 Firefox/Chromium 的辅助进程可能残留
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as e:
                    logger.warning(f"停止 playwright 失败（忽略）: {str(e)[:120]}")
                self._playwright = None
            self._camoufox = None

            self._browsers_ready = False
            self._next_browser_index = 0
            if closed:
                logger.info(f"空闲回收：已关闭 {closed} 个浏览器并释放内存")

    def _idle_check_interval(self) -> float:
        """
        看门狗检查间隔：``idle_timeout`` 的 1/10，限制在 1~60 秒。

        取 1/10 是为了让释放时机与超时值同量级；上下限避免空转（<1s）或反应过慢（>60s）。
        抽成方法便于单测覆写，避免测试真等几十秒。
        """
        return max(1.0, min(60.0, self.idle_timeout / 10.0))

    async def _idle_watchdog(self) -> None:
        """
        空闲看门狗：连续 ``idle_timeout`` 秒没有求解任务就关闭浏览器释放内存。

        仅在懒加载模式（``idle_timeout > 0``）下启动。先 sleep 再检查，
        所以不会在服务刚起来、还没接到请求时就把浏览器回收掉。
        """
        interval = self._idle_check_interval()
        while True:
            try:
                await asyncio.sleep(interval)
                if self._inflight > 0 or not self._browsers_ready:
                    continue
                idle_for = time.monotonic() - self._last_activity
                if idle_for >= self.idle_timeout:
                    await self._release_browsers()
            except Exception as e:
                logger.error(f"空闲回收异常（忽略，下轮重试）: {str(e)[:160]}")

    async def _antishadow_inject(self, page):
        await page.add_init_script("""
          (function() {
            const originalAttachShadow = Element.prototype.attachShadow;
            Element.prototype.attachShadow = function(init) {
              const shadow = originalAttachShadow.call(this, init);
              if (init.mode === 'closed') {
                window.__lastClosedShadowRoot = shadow;
              }
              return shadow;
            };
          })();
        """)



    async def _optimized_route_handler(self, route):
        """Оптимизированный обработчик маршрутов для экономии ресурсов."""
        url = route.request.url
        resource_type = route.request.resource_type

        allowed_types = {'document', 'script', 'xhr', 'fetch'}

        allowed_domains = [
            'challenges.cloudflare.com',
            'static.cloudflareinsights.com',
            'cloudflare.com'
        ]

        if resource_type in allowed_types:
            await route.continue_()
        elif any(domain in url for domain in allowed_domains):
            await route.continue_()
        else:
            await route.abort()

    async def _block_rendering(self, page):
        """Блокировка рендеринга для экономии ресурсов"""
        await page.route("**/*", self._optimized_route_handler)

    async def _unblock_rendering(self, page):
        """Разблокировка рендеринга"""
        await page.unroute("**/*", self._optimized_route_handler)

    async def _find_turnstile_elements(self, page, index: int):
        """Умная проверка всех возможных Turnstile элементов"""
        selectors = [
            '.cf-turnstile',
            '[data-sitekey]',
            'iframe[src*="turnstile"]',
            'iframe[title*="widget"]',
            'div[id*="turnstile"]',
            'div[class*="turnstile"]'
        ]

        elements = []
        for selector in selectors:
            try:
                # Безопасная проверка count()
                try:
                    count = await page.locator(selector).count()
                except Exception:
                    # Если count() дает ошибку, пропускаем этот селектор
                    continue

                if count > 0:
                    elements.append((selector, count))
                    if self.debug:
                        logger.debug(f"Browser {index}: Found {count} elements with selector '{selector}'")
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Selector '{selector}' failed: {str(e)}")
                continue

        return elements

    async def _find_and_click_checkbox(self, page, index: int):
        """Найти и кликнуть по чекбоксу Turnstile CAPTCHA внутри iframe"""
        try:
            # Пробуем разные селекторы iframe с защитой от ошибок
            iframe_selectors = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                'iframe[title*="widget"]'
            ]

            iframe_locator = None
            for selector in iframe_selectors:
                try:
                    test_locator = page.locator(selector).first
                    # Безопасная проверка count для iframe
                    try:
                        iframe_count = await test_locator.count()
                    except Exception:
                        iframe_count = 0

                    if iframe_count > 0:
                        iframe_locator = test_locator
                        if self.debug:
                            logger.debug(f"Browser {index}: Found Turnstile iframe with selector: {selector}")
                        break
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Iframe selector '{selector}' failed: {str(e)}")
                    continue

            if iframe_locator:
                try:
                    # Получаем frame из iframe
                    iframe_element = await iframe_locator.element_handle()
                    frame = await iframe_element.content_frame()

                    if frame:
                        # Ищем чекбокс внутри iframe
                        checkbox_selectors = [
                            'input[type="checkbox"]',
                            '.cb-lb input[type="checkbox"]',
                            'label input[type="checkbox"]'
                        ]

                        for selector in checkbox_selectors:
                            try:
                                # Полностью избегаем locator.count() в iframe - используем альтернативный подход
                                try:
                                    # Пробуем кликнуть напрямую без count проверки
                                    checkbox = frame.locator(selector).first
                                    await checkbox.click(timeout=2000)
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Successfully clicked checkbox in iframe with selector '{selector}'")
                                    return True
                                except Exception as click_e:
                                    # Если прямой клик не сработал, записываем в debug но не падаем
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Direct checkbox click failed for '{selector}': {str(click_e)}")
                                    continue
                            except Exception as e:
                                if self.debug:
                                    logger.debug(f"Browser {index}: Iframe checkbox selector '{selector}' failed: {str(e)}")
                                continue

                        # Если нашли iframe, но не смогли кликнуть чекбокс, пробуем клик по iframe
                        try:
                            if self.debug:
                                logger.debug(f"Browser {index}: Trying to click iframe directly as fallback")
                            await iframe_locator.click(timeout=1000)
                            return True
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"Browser {index}: Iframe direct click failed: {str(e)}")

                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Failed to access iframe content: {str(e)}")

        except Exception as e:
            if self.debug:
                logger.debug(f"Browser {index}: General iframe search failed: {str(e)}")

        return False

    async def _try_click_strategies(self, page, index: int):
        strategies = [
            ('checkbox_click', lambda: self._find_and_click_checkbox(page, index)),
            ('direct_widget', lambda: self._safe_click(page, '.cf-turnstile', index)),
            ('iframe_click', lambda: self._safe_click(page, 'iframe[src*="turnstile"]', index)),
            ('js_click', lambda: page.evaluate("document.querySelector('.cf-turnstile')?.click()")),
            ('sitekey_attr', lambda: self._safe_click(page, '[data-sitekey]', index)),
            ('any_turnstile', lambda: self._safe_click(page, '*[class*="turnstile"]', index)),
            ('xpath_click', lambda: self._safe_click(page, "//div[@class='cf-turnstile']", index))
        ]

        for strategy_name, strategy_func in strategies:
            try:
                result = await strategy_func()
                if result is True or result is None:  # None означает успех для большинства стратегий
                    if self.debug:
                        logger.debug(f"Browser {index}: Click strategy '{strategy_name}' succeeded")
                    return True
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Click strategy '{strategy_name}' failed: {str(e)}")
                continue

        return False

    async def _safe_click(self, page, selector: str, index: int):
        """Полностью безопасный клик с максимальной защитой от ошибок"""
        try:
            # Пробуем кликнуть напрямую без count() проверки
            locator = page.locator(selector).first
            await locator.click(timeout=1000)
            return True
        except Exception as e:
            # Логируем ошибку только в debug режиме
            if self.debug and "Can't query n-th element" not in str(e):
                logger.debug(f"Browser {index}: Safe click failed for '{selector}': {str(e)}")
            return False

    async def _inject_captcha_directly(self, page, websiteKey: str, action: str = '', cdata: str = '', index: int = 0):
        """Inject CAPTCHA directly into the target website"""
        script = f"""
        // Remove any existing turnstile widgets first
        document.querySelectorAll('.cf-turnstile').forEach(el => el.remove());
        document.querySelectorAll('[data-sitekey]').forEach(el => el.remove());

        // Create turnstile widget directly on the page
        const captchaDiv = document.createElement('div');
        captchaDiv.className = 'cf-turnstile';
        captchaDiv.setAttribute('data-sitekey', '{websiteKey}');
        captchaDiv.setAttribute('data-callback', 'onTurnstileCallback');
        {f'captchaDiv.setAttribute("data-action", "{action}");' if action else ''}
        {f'captchaDiv.setAttribute("data-cdata", "{cdata}");' if cdata else ''}
        captchaDiv.style.position = 'fixed';
        captchaDiv.style.top = '20px';
        captchaDiv.style.left = '20px';
        captchaDiv.style.zIndex = '9999';
        captchaDiv.style.backgroundColor = 'white';
        captchaDiv.style.padding = '15px';
        captchaDiv.style.border = '2px solid #0f79af';
        captchaDiv.style.borderRadius = '8px';
        captchaDiv.style.boxShadow = '0 4px 12px rgba(0, 0, 0, 0.3)';

        // Add to body immediately
        document.body.appendChild(captchaDiv);

        // Load Turnstile script and render widget
        const loadTurnstile = () => {{
            const script = document.createElement('script');
            script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
            script.async = true;
            script.defer = true;
            script.onload = function() {{
                console.log('Turnstile script loaded');
                // Wait a bit for script to initialize
                setTimeout(() => {{
                    if (window.turnstile && window.turnstile.render) {{
                        try {{
                            window.turnstile.render(captchaDiv, {{
                                sitekey: '{websiteKey}',
                                {f'action: "{action}",' if action else ''}
                                {f'cdata: "{cdata}",' if cdata else ''}
                                callback: function(token) {{
                                    console.log('Turnstile solved with token:', token);
                                    // Create hidden input for token
                                    let tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                                    if (!tokenInput) {{
                                        tokenInput = document.createElement('input');
                                        tokenInput.type = 'hidden';
                                        tokenInput.name = 'cf-turnstile-response';
                                        document.body.appendChild(tokenInput);
                                    }}
                                    tokenInput.value = token;
                                }},
                                'error-callback': function(error) {{
                                    console.log('Turnstile error:', error);
                                }}
                            }});
                        }} catch (e) {{
                            console.log('Turnstile render error:', e);
                        }}
                    }} else {{
                        console.log('Turnstile API not available');
                    }}
                }}, 1000);
            }};
            script.onerror = function() {{
                console.log('Failed to load Turnstile script');
            }};
            document.head.appendChild(script);
        }};

        // Check if Turnstile is already loaded
        if (window.turnstile) {{
            console.log('Turnstile already loaded, rendering immediately');
            try {{
                window.turnstile.render(captchaDiv, {{
                    sitekey: '{websiteKey}',
                    {f'action: "{action}",' if action else ''}
                    {f'cdata: "{cdata}",' if cdata else ''}
                    callback: function(token) {{
                        console.log('Turnstile solved with token:', token);
                        let tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                        if (!tokenInput) {{
                            tokenInput = document.createElement('input');
                            tokenInput.type = 'hidden';
                            tokenInput.name = 'cf-turnstile-response';
                            document.body.appendChild(tokenInput);
                        }}
                        tokenInput.value = token;
                    }},
                    'error-callback': function(error) {{
                        console.log('Turnstile error:', error);
                    }}
                }});
            }} catch (e) {{
                console.log('Immediate render error:', e);
                loadTurnstile();
            }}
        }} else {{
            loadTurnstile();
        }}

        // Setup global callback
        window.onTurnstileCallback = function(token) {{
            console.log('Global turnstile callback executed:', token);
        }};
        """

        await page.evaluate(script)
        if self.debug:
            logger.debug(f"Browser {index}: Injected CAPTCHA directly into website with sitekey: {websiteKey}")

    def _parse_proxy(self, proxy: str) -> dict:
        """Parse proxy string into Playwright proxy options."""
        if '@' in proxy:
            scheme_part, auth_part = proxy.split('://', 1)
            auth, address = auth_part.split('@', 1)
            username, password = auth.split(':', 1)
            return {
                "server": f"{scheme_part}://{address}",
                "username": username,
                "password": password,
            }

        parts = proxy.split(':')
        if len(parts) == 5:
            proxy_scheme, proxy_ip, proxy_port, proxy_user, proxy_pass = parts
            return {
                "server": f"{proxy_scheme}://{proxy_ip}:{proxy_port}",
                "username": proxy_user,
                "password": proxy_pass,
            }
        if len(parts) == 3 or '://' in proxy:
            return {"server": proxy}
        raise ValueError(f"Invalid proxy format: {proxy}")

    def _select_proxy(self, index: int) -> Optional[str]:
        """Pick a proxy line from proxies.txt when proxy support is enabled."""
        if not self.proxy_support:
            return None

        proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
        try:
            with open(proxy_file_path) as proxy_file:
                proxies = [
                    line.strip()
                    for line in proxy_file
                    if line.strip() and not line.strip().startswith('#')
                ]
            proxy = random.choice(proxies) if proxies else None
            if self.debug and proxy:
                logger.debug(f"Browser {index}: Selected proxy: {proxy}")
            elif self.debug and not proxy:
                logger.debug(f"Browser {index}: No proxies available")
            return proxy
        except FileNotFoundError:
            logger.warning(f"Proxy file not found: {proxy_file_path}")
            return None
        except Exception as e:
            logger.error(f"Error reading proxy file: {str(e)}")
            return None

    async def _create_browser_context(self, browser, browser_config: dict, proxy: Optional[str] = None, index: int = 0):
        """
        Create a browser context compatible with Chromium and Camoufox.

        Playwright Python 注意:
          - viewport=None 表示「使用默认 1280x720」，不是关闭 viewport
          - 关闭默认 viewport 必须 no_viewport=True
        Camoufox/Firefox 的 setDefaultViewport 不接受 isMobile 字段，
        因此对 camoufox 必须 no_viewport=True。
        """
        context_options: dict = {}

        if self.browser_type == "camoufox":
            # 关键：必须是 no_viewport=True，不能写 viewport=None
            context_options["no_viewport"] = True
        else:
            if browser_config.get('useragent'):
                context_options["user_agent"] = browser_config['useragent']
            if browser_config.get('sec_ch_ua') and str(browser_config['sec_ch_ua']).strip():
                context_options["extra_http_headers"] = {
                    'sec-ch-ua': browser_config['sec_ch_ua']
                }

        if proxy:
            proxy_opts = self._parse_proxy(proxy)
            context_options["proxy"] = proxy_opts
            if self.debug:
                logger.debug(f"Browser {index}: Creating context with proxy {proxy_opts.get('server')}")
        elif self.debug:
            logger.debug(
                f"Browser {index}: Creating context without proxy "
                f"(no_viewport={context_options.get('no_viewport')})"
            )

        # Camoufox: 优先 new_page(no_viewport=True)；失败再尝试 new_context
        if self.browser_type == "camoufox":
            try:
                page = await browser.new_page(**context_options)
                if self.debug:
                    logger.debug(f"Browser {index}: Camoufox context created via new_page(no_viewport=True)")
                return page.context, page
            except Exception as e1:
                msg1 = str(e1)
                if self.debug:
                    logger.warning(f"Browser {index}: new_page failed: {msg1[:160]}")
                try:
                    context = await browser.new_context(**context_options)
                    page = await context.new_page()
                    if self.debug:
                        logger.debug(f"Browser {index}: Camoufox context created via new_context(no_viewport=True)")
                    return context, page
                except Exception as e2:
                    raise RuntimeError(
                        f"Camoufox context creation failed. new_page: {msg1[:120]}; new_context: {str(e2)[:120]}"
                    ) from e2

        context = await browser.new_context(**context_options)
        page = await context.new_page()
        return context, page

    async def _return_browser_to_pool(self, index, browser, browser_config):
        """Safely return a browser instance to the pool if still connected."""
        try:
            # camoufox 的 Browser 不一定有 is_connected；有则检查，没有则直接归还
            if hasattr(browser, 'is_connected'):
                if not browser.is_connected():
                    if self.debug:
                        logger.warning(f"Browser {index}: Browser disconnected, not returning to pool")
                    return
            await self.browser_pool.put((index, browser, browser_config))
            if self.debug:
                logger.debug(f"Browser {index}: Browser returned to pool")
        except Exception as e:
            if self.debug:
                logger.warning(f"Browser {index}: Error returning browser to pool: {str(e)}")

    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str, action: Optional[str] = None, cdata: Optional[str] = None):
        """Solve the Turnstile challenge."""
        proxy = None
        context = None
        start_time = time.time()

        # 先登记 in-flight：看门狗据此判断"有任务在跑"，不会把浏览器回收掉
        self._inflight += 1
        self._last_activity = time.monotonic()
        index = None
        browser = None
        browser_config = None

        try:
            await self._ensure_pool()
            index, browser, browser_config = await self.browser_pool.get()
            try:
                if hasattr(browser, 'is_connected') and not browser.is_connected():
                    if self.debug:
                        logger.warning(f"Browser {index}: Browser disconnected, skipping")
                    await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0})
                    return
            except Exception as e:
                if self.debug:
                    logger.warning(f"Browser {index}: Cannot check browser state: {str(e)}")

            proxy = self._select_proxy(index)
            context, page = await self._create_browser_context(browser, browser_config, proxy=proxy, index=index)

            await self._antishadow_inject(page)
            await self._block_rendering(page)

            # Chromium 伪装；Camoufox 自带指纹，避免强行注入 chrome 对象
            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });

                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                };
                """)
                await page.set_viewport_size({"width": 500, "height": 100})
                if self.debug:
                    logger.debug(f"Browser {index}: Set viewport size to 500x100")

            if self.debug:
                logger.debug(
                    f"Browser {index}: Starting Turnstile solve for URL: {url} "
                    f"with Sitekey: {sitekey} | Action: {action} | Cdata: {cdata} | Proxy: {proxy}"
                )
                logger.debug(f"Browser {index}: Loading real website directly: {url}")

            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await self._unblock_rendering(page)

            if self.debug:
                logger.debug(f"Browser {index}: Injecting Turnstile widget directly into target site")

            await self._inject_captcha_directly(page, sitekey, action or '', cdata or '', index)
            await asyncio.sleep(3)

            locator = page.locator('input[name="cf-turnstile-response"]')
            max_attempts = 30
            click_count = 0
            max_clicks = 10

            for attempt in range(max_attempts):
                try:
                    try:
                        count = await locator.count()
                    except Exception as e:
                        if self.debug:
                            logger.debug(f"Browser {index}: Locator count failed on attempt {attempt + 1}: {str(e)}")
                        count = 0

                    if count == 0:
                        if self.debug and attempt % 5 == 0:
                            logger.debug(f"Browser {index}: No token elements found on attempt {attempt + 1}")
                    elif count == 1:
                        try:
                            token = await locator.input_value(timeout=500)
                            if token:
                                elapsed_time = round(time.time() - start_time, 3)
                                logger.success(
                                    f"Browser {index}: Successfully solved captcha - "
                                    f"{COLORS.get('MAGENTA')}{token[:10]}{COLORS.get('RESET')} in "
                                    f"{COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds"
                                )
                                await save_result(task_id, "turnstile", {"value": token, "elapsed_time": elapsed_time})
                                return
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"Browser {index}: Single token element check failed: {str(e)}")
                    else:
                        if self.debug:
                            logger.debug(f"Browser {index}: Found {count} token elements, checking all")

                        for i in range(count):
                            try:
                                element_token = await locator.nth(i).input_value(timeout=500)
                                if element_token:
                                    elapsed_time = round(time.time() - start_time, 3)
                                    logger.success(
                                        f"Browser {index}: Successfully solved captcha - "
                                        f"{COLORS.get('MAGENTA')}{element_token[:10]}{COLORS.get('RESET')} in "
                                        f"{COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds"
                                    )
                                    await save_result(
                                        task_id, "turnstile",
                                        {"value": element_token, "elapsed_time": elapsed_time}
                                    )
                                    return
                            except Exception as e:
                                if self.debug:
                                    logger.debug(f"Browser {index}: Token element {i} check failed: {str(e)}")
                                continue

                    if attempt > 2 and attempt % 3 == 0 and click_count < max_clicks:
                        click_success = await self._try_click_strategies(page, index)
                        click_count += 1
                        if self.debug:
                            if click_success:
                                logger.debug(f"Browser {index}: Click successful (click #{click_count}/{max_clicks})")
                            else:
                                logger.debug(
                                    f"Browser {index}: All click strategies failed on attempt "
                                    f"{attempt + 1} (click #{click_count}/{max_clicks})"
                                )

                    wait_time = min(0.5 + (attempt * 0.05), 2.0)
                    await asyncio.sleep(wait_time)

                    if self.debug and attempt % 5 == 0:
                        logger.debug(
                            f"Browser {index}: Attempt {attempt + 1}/{max_attempts} - "
                            f"Waiting for token (clicks: {click_count}/{max_clicks})"
                        )

                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Attempt {attempt + 1} error: {str(e)}")
                    continue

            elapsed_time = round(time.time() - start_time, 3)
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time})
            if self.debug:
                logger.error(
                    f"Browser {index}: Error solving Turnstile in "
                    f"{COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')} Seconds"
                )
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time})
            logger.error(f"Browser {index}: Error solving Turnstile: {str(e)}")
        finally:
            if self.debug:
                logger.debug(f"Browser {index}: Closing browser context and cleaning up")

            if context is not None:
                try:
                    await context.close()
                    if self.debug:
                        logger.debug(f"Browser {index}: Context closed successfully")
                except Exception as e:
                    if self.debug:
                        logger.warning(f"Browser {index}: Error closing context: {str(e)}")

            # 懒加载或补齐失败时可能没取到浏览器，此时无事可归还
            if browser is not None:
                await self._return_browser_to_pool(index, browser, browser_config)

            self._inflight -= 1
            self._last_activity = time.monotonic()






    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        action = request.args.get('action')
        cdata = request.args.get('cdata')

        if not url or not sitekey:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_PAGEURL",
                "errorDescription": "Both 'url' and 'sitekey' are required"
            }), 200

        task_id = str(uuid.uuid4())
        await save_result(task_id, "turnstile", {
            "status": "CAPTCHA_NOT_READY",
            "createTime": int(time.time()),
            "url": url,
            "sitekey": sitekey,
            "action": action,
            "cdata": cdata
        })

        try:
            asyncio.create_task(self._solve_turnstile(task_id=task_id, url=url, sitekey=sitekey, action=action, cdata=cdata))

            if self.debug:
                logger.debug(f"Request completed with taskid {task_id}.")
            return jsonify({
                "errorId": 0,
                "taskId": task_id
            }), 200
        except Exception as e:
            logger.error(f"Unexpected error processing request: {str(e)}")
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_UNKNOWN",
                "errorDescription": str(e)
            }), 200

    async def get_result(self):
        """Return solved data"""
        task_id = request.args.get('id')

        if not task_id:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_CAPTCHA_ID",
                "errorDescription": "Invalid task ID/Request parameter"
            }), 200

        result = await load_result(task_id)
        if not result:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Task not found"
            }), 200

        if result == "CAPTCHA_NOT_READY" or (isinstance(result, dict) and result.get("status") == "CAPTCHA_NOT_READY"):
            return jsonify({"status": "processing"}), 200

        if isinstance(result, dict) and result.get("value") == "CAPTCHA_FAIL":
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Workers could not solve the Captcha"
            }), 200

        if isinstance(result, dict) and result.get("value") and result.get("value") != "CAPTCHA_FAIL":
            return jsonify({
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "token": result["value"]
                }
            }), 200
        else:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Workers could not solve the Captcha"
            }), 200



    @staticmethod
    async def index():
        """Serve the API documentation page."""
        return """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Turnstile Solver API</title>
                <script src="https://cdn.tailwindcss.com"></script>
            </head>
            <body class="bg-gray-900 text-gray-200 min-h-screen flex items-center justify-center">
                <div class="bg-gray-800 p-8 rounded-lg shadow-md max-w-2xl w-full border border-red-500">
                    <h1 class="text-3xl font-bold mb-6 text-center text-red-500">Welcome to Turnstile Solver API</h1>

                    <p class="mb-4 text-gray-300">To use the turnstile service, send a GET request to
                       <code class="bg-red-700 text-white px-2 py-1 rounded">/turnstile</code> with the following query parameters:</p>

                    <ul class="list-disc pl-6 mb-6 text-gray-300">
                        <li><strong>url</strong>: The URL where Turnstile is to be validated</li>
                        <li><strong>sitekey</strong>: The site key for Turnstile</li>
                    </ul>

                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">Example usage:</p>
                        <code class="text-sm break-all text-red-300">/turnstile?url=https://example.com&sitekey=sitekey</code>
                    </div>


                    <div class="bg-gray-700 p-4 rounded-lg mb-6">
                        <p class="text-gray-200 font-semibold mb-3">📢 Connect with Us</p>
                        <div class="space-y-2 text-sm">
                            <p class="text-gray-300">
                                📢 <strong>Channel:</strong>
                                <a href="https://t.me/D3_vin" class="text-red-300 hover:underline">https://t.me/D3_vin</a>
                                - Latest updates and releases
                            </p>
                            <p class="text-gray-300">
                                💬 <strong>Chat:</strong>
                                <a href="https://t.me/D3vin_chat" class="text-red-300 hover:underline">https://t.me/D3vin_chat</a>
                                - Community support and discussions
                            </p>
                            <p class="text-gray-300">
                                📁 <strong>GitHub:</strong>
                                <a href="https://github.com/D3-vin" class="text-red-300 hover:underline">https://github.com/D3-vin</a>
                                - Source code and development
                            </p>
                        </div>
                    </div>
                </div>
            </body>
            </html>
        """


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Turnstile API Server")

    parser.add_argument('--no-headless', action='store_true', help='Run the browser with GUI (disable headless mode). By default, headless mode is enabled.')
    parser.add_argument('--useragent', type=str, help='User-Agent string (if not specified, random configuration is used)')
    parser.add_argument('--debug', action='store_true', help='Enable or disable debug mode for additional logging and troubleshooting information (default: False)')
    parser.add_argument('--browser_type', type=str, default='chromium', help='Specify the browser type for the solver. Supported options: chromium, chrome, msedge, camoufox (default: chromium)')
    parser.add_argument('--thread', type=int, default=4, help='Set the number of browser threads to use for multi-threaded mode. Increasing this will speed up execution but requires more resources (default: 1)')
    parser.add_argument('--proxy', action='store_true', help='Enable proxy support for the solver (Default: False)')
    parser.add_argument('--random', action='store_true', help='Use random User-Agent and Sec-CH-UA configuration from pool')
    parser.add_argument('--browser', type=str, help='Specify browser name to use (e.g., chrome, firefox)')
    parser.add_argument('--version', type=str, help='Specify browser version to use (e.g., 139, 141)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Specify the IP address where the API solver runs. (Default: 127.0.0.1)')
    parser.add_argument('--port', type=str, default='5072', help='Set the port for the API solver to listen on. (Default: 5072)')
    parser.add_argument('--idle-timeout', type=float, default=600.0, help='Seconds of inactivity after which browsers are closed to free memory; browsers are also started lazily on the first request. 0 disables both (old behavior: start at boot, never release). (default: 600)')
    return parser.parse_args()


def create_app(headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool, browser_name: str, browser_version: str, idle_timeout: float = 600.0) -> Quart:
    server = TurnstileAPIServer(headless=headless, useragent=useragent, debug=debug, browser_type=browser_type, thread=thread, proxy_support=proxy_support, use_random_config=use_random_config, browser_name=browser_name, browser_version=browser_version, idle_timeout=idle_timeout)
    return server.app


if __name__ == '__main__':
    args = parse_args()
    browser_types = [
        'chromium',
        'chrome',
        'msedge',
        'camoufox',
    ]
    if args.browser_type not in browser_types:
        logger.error(f"Unknown browser type: {COLORS.get('RED')}{args.browser_type}{COLORS.get('RESET')} Available browser types: {browser_types}")
    else:
        app = create_app(
            headless=not args.no_headless,
            debug=args.debug,
            useragent=args.useragent,
            browser_type=args.browser_type,
            thread=args.thread,
            proxy_support=args.proxy,
            use_random_config=args.random,
            browser_name=args.browser,
            browser_version=args.version,
            idle_timeout=args.idle_timeout
        )
        app.run(host=args.host, port=int(args.port))
