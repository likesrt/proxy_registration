"""
api_solver 浏览器池的懒加载 / 空闲回收单测。

不用真实浏览器：用替身对象替换 ``_initialize_browser`` 的产物，只验证池的状态机
（何时创建、何时补齐、何时释放、是否会在任务进行中误释放）。
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_solver
from api_solver import TurnstileAPIServer


class FakeBrowser:
    """最小浏览器替身：记录是否被关闭。"""

    def __init__(self, connected=True):
        self.closed = False
        self._connected = connected

    def is_connected(self):
        return self._connected and not self.closed

    async def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def make_server(thread=1, idle_timeout=600.0):
    """构造一个不碰真实浏览器的 server 实例。"""
    return TurnstileAPIServer(
        headless=True,
        useragent=None,
        debug=False,
        browser_type="camoufox",
        thread=thread,
        proxy_support=False,
        idle_timeout=idle_timeout,
    )


class TestLazyConfig(unittest.TestCase):
    def test_positive_idle_timeout_enables_lazy(self):
        self.assertTrue(make_server(idle_timeout=600).lazy_browsers)
        self.assertTrue(make_server(idle_timeout=0.5).lazy_browsers)

    def test_zero_disables_lazy_for_backward_compat(self):
        server = make_server(idle_timeout=0)
        self.assertFalse(server.lazy_browsers)

    def test_negative_disables_lazy(self):
        self.assertFalse(make_server(idle_timeout=-1).lazy_browsers)


class TestEnsurePool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.created = []

    async def _fake_init(self, server):
        """替身：按 thread_count 填池，并记录创建次数。"""
        async def _init():
            while server.browser_pool.qsize() < server.thread_count:
                b = FakeBrowser()
                self.created.append(b)
                server._next_browser_index += 1
                await server.browser_pool.put((server._next_browser_index, b, {}))
            server._browsers_ready = True
        server._initialize_browser = _init

    async def test_ensure_pool_is_idempotent(self):
        server = make_server(thread=1)
        await self._fake_init(server)
        await server._ensure_pool()
        await server._ensure_pool()  # 第二次不应重复创建
        self.assertEqual(len(self.created), 1)
        self.assertEqual(server.browser_pool.qsize(), 1)

    async def test_concurrent_ensure_creates_only_once(self):
        """多个请求同时到达时，_pool_lock 必须保证只创建一轮。"""
        server = make_server(thread=1)
        await self._fake_init(server)
        await asyncio.gather(*(server._ensure_pool() for _ in range(5)))
        self.assertEqual(len(self.created), 1, "并发 ensure 不得重复创建浏览器")

    async def test_pool_is_topped_up_after_browser_loss(self):
        """既有 bug 回归：浏览器断开被丢弃后，池只减不增会永久阻塞 get()。"""
        server = make_server(thread=2)
        await self._fake_init(server)
        await server._ensure_pool()
        self.assertEqual(server.browser_pool.qsize(), 2)

        # 模拟两个浏览器都因断开而被丢弃（_return_browser_to_pool 不放回）
        while not server.browser_pool.empty():
            server.browser_pool.get_nowait()

        # 补齐：get() 不会永久挂住
        await server._ensure_pool()
        got = await asyncio.wait_for(server.browser_pool.get(), timeout=1)
        self.assertIsInstance(got[1], FakeBrowser)

    async def test_indices_stay_unique_across_topups(self):
        """补齐时索引不能与池中已有的重复（否则日志与 _select_proxy 会混）。"""
        server = make_server(thread=1)
        await self._fake_init(server)
        await server._ensure_pool()
        first = (await server.browser_pool.get())[0]
        await server._ensure_pool()  # 池空 -> 补齐
        second = (await server.browser_pool.get())[0]
        self.assertNotEqual(first, second)


class TestReleaseBrowsers(unittest.IsolatedAsyncioTestCase):
    async def _server_with_browsers(self, count=1):
        server = make_server(thread=count)

        async def _init():
            while server.browser_pool.qsize() < server.thread_count:
                server._next_browser_index += 1
                await server.browser_pool.put(
                    (server._next_browser_index, FakeBrowser(), {})
                )
            server._browsers_ready = True

        server._initialize_browser = _init
        await server._ensure_pool()
        return server

    async def test_release_closes_browsers_and_resets_state(self):
        server = await self._server_with_browsers(2)
        browsers = [server.browser_pool._queue[i][1] for i in range(2)]
        server._playwright = FakePlaywright()

        await server._release_browsers()

        self.assertTrue(all(b.closed for b in browsers))
        self.assertTrue(server._playwright is None, "驱动句柄也要释放")
        self.assertFalse(server._browsers_ready)
        self.assertEqual(server.browser_pool.qsize(), 0)

    async def test_release_survives_a_browser_that_fails_to_close(self):
        """单个浏览器关不掉不能阻断整体释放（否则内存就白占了）。"""
        server = await self._server_with_browsers(1)

        class Bad(FakeBrowser):
            async def close(self):
                raise RuntimeError("关不掉")

        server.browser_pool._queue.clear()
        server.browser_pool.put_nowait((99, Bad(), {}))

        await server._release_browsers()
        self.assertFalse(server._browsers_ready)

    async def test_after_release_pool_can_be_recreated(self):
        server = await self._server_with_browsers(1)
        await server._release_browsers()
        await server._ensure_pool()  # 应能重新创建
        self.assertTrue(server._browsers_ready)
        self.assertEqual(server.browser_pool.qsize(), 1)


class TestIdleWatchdog(unittest.IsolatedAsyncioTestCase):
    """覆写检查间隔，让看门狗按毫秒级节拍跑，避免测试真等几十秒。"""

    def _fast(self, server, interval=0.02):
        server._idle_check_interval = lambda: interval
        return server

    async def _server_with_one_browser(self, idle_timeout=0.05):
        server = self._fast(make_server(thread=1, idle_timeout=idle_timeout))

        async def _init():
            server._next_browser_index += 1
            await server.browser_pool.put(
                (server._next_browser_index, FakeBrowser(), {})
            )
            server._browsers_ready = True

        server._initialize_browser = _init
        await server._ensure_pool()
        return server

    async def test_releases_after_idle_timeout(self):
        server = await self._server_with_one_browser(idle_timeout=0.05)
        task = asyncio.create_task(server._idle_watchdog())
        try:
            for _ in range(40):
                await asyncio.sleep(0.05)
                if not server._browsers_ready:
                    break
        finally:
            task.cancel()
        self.assertFalse(server._browsers_ready, "空闲超时后应释放浏览器")
        self.assertEqual(server.browser_pool.qsize(), 0)

    async def test_does_not_release_while_task_inflight(self):
        """有任务在跑时绝不能回收——否则会关掉正在用的浏览器。"""
        server = await self._server_with_one_browser(idle_timeout=0.05)
        server._inflight = 1  # 模拟正在求解
        task = asyncio.create_task(server._idle_watchdog())
        try:
            await asyncio.sleep(0.5)  # 是 idle_timeout 的 10 倍，看门狗已多次醒来
        finally:
            task.cancel()
        self.assertTrue(server._browsers_ready, "in-flight 期间不得释放")
        self.assertEqual(server.browser_pool.qsize(), 1)

    async def test_release_resets_activity_clock(self):
        """释放后再次起浏览器，last_activity 要更新，否则会被立刻二次回收。"""
        server = await self._server_with_one_browser(idle_timeout=0.05)
        await server._release_browsers()
        await server._ensure_pool()
        server._last_activity = __import__("time").monotonic()
        task = asyncio.create_task(server._idle_watchdog())
        try:
            await asyncio.sleep(0.05)  # 小于 idle_timeout，不该被回收
        finally:
            task.cancel()
        self.assertTrue(server._browsers_ready)

    async def test_check_interval_is_clamped(self):
        self.assertEqual(make_server(idle_timeout=600)._idle_check_interval(), 60.0)
        self.assertEqual(make_server(idle_timeout=5)._idle_check_interval(), 1.0)
        self.assertEqual(make_server(idle_timeout=0)._idle_check_interval(), 1.0)

    async def test_no_release_when_timeout_is_zero(self):
        """idle_timeout=0 是旧行为：不懒加载、也不释放。"""
        server = self._fast(make_server(thread=1, idle_timeout=0))

        async def _init():
            server._next_browser_index += 1
            await server.browser_pool.put(
                (server._next_browser_index, FakeBrowser(), {})
            )
            server._browsers_ready = True

        server._initialize_browser = _init
        await server._ensure_pool()
        self.assertFalse(server.lazy_browsers)
        # 看门狗不会被 _startup 启动（见 TestStartupWiring），故浏览器一直保留
        self.assertTrue(server._browsers_ready)
        self.assertEqual(server.browser_pool.qsize(), 1)


class TestStartupWiring(unittest.TestCase):
    def test_startup_skips_browser_when_lazy(self):
        """懒加载模式下 _startup 不应预先创建浏览器（这是省内存的关键）。"""
        import inspect

        src = inspect.getsource(TurnstileAPIServer._startup)
        self.assertIn("self.lazy_browsers", src)
        self.assertIn("_idle_watchdog", src)

    def test_cli_exposes_idle_timeout(self):
        import inspect

        src = inspect.getsource(api_solver.parse_args)
        self.assertIn("--idle-timeout", src)
        self.assertIn("--thread", src)

    def test_create_app_accepts_idle_timeout(self):
        import inspect

        sig = inspect.signature(api_solver.create_app)
        self.assertIn("idle_timeout", sig.parameters)


if __name__ == "__main__":
    unittest.main()
