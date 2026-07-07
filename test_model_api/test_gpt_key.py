import os
import requests

# 1. 从系统环境变量中读取你配置的 Key
api_key = os.getenv("OPENAI_API_KEY")
base_url = "https://codex.ciii.club/v1/chat/completions"

print("--- 正在检测配置 ---")
if not api_key:
    print("❌ 错误：未检测到环境变量 OPENAI_API_KEY！")
    print("请先在终端运行：export OPENAI_API_KEY='你的真实sk-key'")
    exit(1)
else:
    # 打印前几位和后几位，方便你肉眼确认有没有复制错，同时保护隐私
    print(f"✅ 已检测到 Key: {api_key[:6]}...{api_key[-4:] if len(api_key) > 4 else ''}")

# 2. 构造测试请求
headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}

# 简单发一个最基础的对话，用来测通管道
data = {
    "model": "gpt-5.5",  # 如果报错提示模型不存在，可以尝试换成 "gpt-4o"
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 10
}

# 3. 设置你的本地代理（和你的 config 保持一致）
# proxies = {
#     "http": "http://127.0.0.1:7890",
#     "https": "http://127.0.0.1:7890"
# }

print("\n--- 正在向中转站发送请求 ---")
try:
    # response = requests.post(base_url, json=data, headers=headers, proxies=proxies, timeout=10)
    response = requests.post(base_url, json=data, headers=headers, timeout=10)
    if response.status_code == 200:
        print("🎉 恭喜！Key 验证成功，网络也完全通畅！")
        print("AI 回复内容:", response.json())
    else:
        print(f"❌ 请求失败，状态码: {response.status_code}")
        print("错误信息反馈:", response.text)
        print("\n💡 提示：如果是 401，说明 Key 真的错了或额度没了；如果是 404，可能是中转站还不支持 'gpt-5.5' 这个模型名字，可以改用 'gpt-4o' 再试。")

except requests.exceptions.ProxyError:
    print("❌ 代理连接失败！请检查你的本地 7890 代理软件是否真正开启。")
except Exception as e:
    print(f"❌ 发生未知错误: {e}")