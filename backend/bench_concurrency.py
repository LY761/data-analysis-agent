# -*- coding: utf-8 -*-
"""高并发「阻塞 vs 异步」概念演示（纯演示，不调真实API）

两种写法对比：
  A. 阻塞式   —— 同步调用直接写在 async 函数里（改造前的样子）
  B. to_thread —— 把阻塞调用丢进线程池（改造后的样子）
"""
import asyncio
import time

LLM_DELAY = 1.0    # 模拟一次 LLM 调用的耗时（秒）
CONCURRENCY = 8    # 同时进来的请求数


async def blocking_style(name: str):
    """改造前：同步 OpenAI client 的阻塞网络调用，会卡住整个事件循环"""
    time.sleep(LLM_DELAY)   # 模拟同步阻塞
    return name


async def thread_style(name: str):
    """改造后：asyncio.to_thread 把阻塞调用丢到线程池，事件循环保持空闲"""
    await asyncio.to_thread(time.sleep, LLM_DELAY)
    return name


async def batch(fn, n: int) -> float:
    t0 = time.time()
    await asyncio.gather(*(fn(f"req{i}") for i in range(n)))
    return time.time() - t0


async def main():
    print(f"场景：每个请求要等 {LLM_DELAY}s（LLM），同时进来 {CONCURRENCY} 个请求")

    tb = await batch(blocking_style, CONCURRENCY)
    print(f"\n[改造前] 阻塞式调用   : {tb:.1f}s   ← 8个请求串行排队，最后一个人等了 8 秒")
    print(f"         原因: time.sleep 阻塞了唯一的事件循环，后面的请求全都堵在门口")

    ta = await batch(thread_style, CONCURRENCY)
    print(f"[改造后] to_thread调用 : {ta:.1f}s   ← 8个请求在线程池里并行，互不阻塞")
    print(f"         原因: 阻塞操作被丢到线程池，事件循环可以继续接新的请求")

    print(f"\n结论：")
    print(f"  1. 单请求本身没有变快 —— LLM 还是要等 {LLM_DELAY}s")
    print(f"  2. 并发时天差地别 —— 改造前最后一人等 {CONCURRENCY*LLM_DELAY:.0f}s，改造后 ~{ta:.1f}s 全拿到")
    print(f"  3. 这就是 demo 单用户没感觉、上线一多人就『卡死』的根因")


asyncio.run(main())
