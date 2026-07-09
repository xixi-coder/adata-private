import os
import sys

import requests

BASE_URL = "https://codex.ciii.club/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o"


def masked_key(api_key: str) -> str:
    """Show enough of the key for manual confirmation without exposing it."""
    return f"{api_key[:6]}...{api_key[-4:] if len(api_key) > 4 else ''}"


def main() -> int:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)

    print("--- 正在检测配置 ---")
    if not api_key:
        print("错误：未检测到环境变量 OPENAI_API_KEY！")
        print("请先在终端运行：export OPENAI_API_KEY='你的真实 sk-key'")
        return 1

    print(f"已检测到 Key: {masked_key(api_key)}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 10,
    }

    print("\n--- 正在向中转站发送请求 ---")
    try:
        response = requests.post(BASE_URL, json=data, headers=headers, timeout=10)
    except requests.exceptions.ProxyError:
        print("代理连接失败：请检查本地 7890 代理软件是否真正开启。")
        return 1
    except requests.RequestException as exc:
        print(f"请求失败：{exc}")
        return 1

    if response.status_code == 200:
        print("Key 验证成功，网络也完全通畅。")
        print("AI 回复内容:", response.json())
        return 0

    print(f"请求失败，状态码: {response.status_code}")
    print("错误信息反馈:", response.text)
    print("提示：401 通常是 Key 或额度问题；404 通常是模型名或中转站路由不支持。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
