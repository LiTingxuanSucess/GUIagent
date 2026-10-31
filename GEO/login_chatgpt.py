"""
一次性登录：打开浏览器 -> 手动登录 ChatGPT -> 保存 cookies
之后 test.py 会直接复用，无需再复制 profile
"""
import asyncio
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from browser_use import Agent, Browser, ChatOpenAI

COOKIES_FILE = os.path.join(os.path.dirname(__file__), "chatgpt_cookies.json")
LLM_CONFIG = {
    "api_key": os.environ.get("DASHSCOPE_API_KEY", ""),
    "model": "qwen-plus",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}


async def main():
    if not LLM_CONFIG["api_key"]:
        raise RuntimeError("Set the DASHSCOPE_API_KEY environment variable")
    print("1. 正在启动浏览器并打开 ChatGPT...")
    llm = ChatOpenAI(model=LLM_CONFIG["model"], api_key=LLM_CONFIG["api_key"], base_url=LLM_CONFIG["base_url"], temperature=0.1)
    browser = Browser(headless=False, user_data_dir=None)
    agent = Agent(
        task="打开 https://chatgpt.com 并等待页面加载完成。完成后无需其他操作。",
        llm=llm,
        browser=browser,
    )
    await agent.run()
    print("\n2. 请在浏览器中完成 ChatGPT 登录")
    input("   登录完成后，回到这里按 Enter 保存 cookies...")
    await browser.export_storage_state(COOKIES_FILE)
    await browser.stop()
    print(f"3. 已保存到 {COOKIES_FILE}，下次运行 test.py 将自动复用登录态")


if __name__ == "__main__":
    asyncio.run(main())
