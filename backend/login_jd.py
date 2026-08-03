# -*- coding: utf-8 -*-
"""
京东登录脚本 — 手动登录一次，会话持久化到 jd_session/，之后爬虫复用。

用法（在 backend 目录）:
  ../.venv/Scripts/python.exe login_jd.py

浏览器会打开京东首页 → 你手动扫码/账号登录 → 脚本检测到登录 → 保存会话退出。

关键设计：登录页用 session.context.new_page() 单独打开，脚本绝不导航/关闭它。
检测用 session.fetch() 开"临时页"访问订单中心看是否跳 passport（临时页导航后自动
关闭，不影响用户的稳定登录页）。之前的版本用 fetch() 反复导航同一个页面，导致登录页
"一闪而过"没法操作——那是个 bug，已修复。

注意：cookie 检测（读 pt_key 等）在新版京东上不可靠，故统一用行为检测。
"""
import sys
import io
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from scrapling.fetchers import StealthySession

PROFILE = str(Path("jd_session").resolve())
Path(PROFILE).mkdir(parents=True, exist_ok=True)


def _is_logged_in(session) -> bool:
    """行为检测：访问订单中心，看是否被重定向到 passport 登录页。
    session.fetch() 开的是临时页（导航后自动关闭），不打扰用户的稳定登录页。"""
    try:
        v = session.fetch("https://order.jd.com/center/list.action",
                          timeout=15000, wait=1500)
        url = str(getattr(v, "url", ""))
        return "passport" not in url and "login" not in url.lower()
    except Exception:
        return False


def main():
    print("=" * 55)
    print("  京东登录")
    print("=" * 55)
    print("1. 浏览器将打开京东首页（这个页面脚本不会动它）。")
    print("2. 请在浏览器里登录（扫码或账号密码）。")
    print("3. 脚本每 15 秒检测登录态（临时页，不打扰登录页）。")
    print("4. 最多等待 5 分钟。\n")

    with StealthySession(headless=False, user_data_dir=PROFILE,
                         hide_canvas=True, block_webrtc=True) as session:
        # 稳定登录页：脚本绝不导航/关闭它
        page = session.context.new_page()
        page.set_default_navigation_timeout(30000)
        try:
            page.goto("https://www.jd.com")
        except Exception as e:
            print(f"⚠️ 打开京东首页失败: {e}")
            print("   会话可能已部分保存，可重跑本脚本再试。")
            return

        confirmed = False
        for i in range(20):  # 每 15s 检测一次，最多 5 分钟
            time.sleep(15)
            if _is_logged_in(session):
                confirmed = True
                break
            print(f"  … 等待登录中（{15*(i+1)}s/300s）")

        if confirmed:
            print(f"✅ 登录成功，会话已保存到 {PROFILE}")
        else:
            print("⚠️ 未检测到登录成功，会话可能部分可用。可重跑本脚本再试。")


if __name__ == "__main__":
    main()
