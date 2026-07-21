from openai import OpenAI

# 修改这里
API_KEY = "sk-0307cfddbda23383dd89d0d37e77f6933ae9ce3d7bc1cc82abc5720e44de8520"
BASE_URL = "https://codex.ciii.club/v1"
# 例如：
# https://api.openai.com/v1
# https://api.xxx.com/v1

client = OpenAI(
    api_key=API_KEY,
    base_url=BASE_URL,
)

try:
    response = client.chat.completions.create(
        model="gpt-5.5",
        messages=[
            {"role": "user", "content": "你好，回复 OK 即可。"}
        ],
        max_tokens=10,
    )

    print("✅ Key 和域名正常")
    print("模型回复：", response.choices[0].message.content)

except Exception as e:
    print("❌ 请求失败")
    print(type(e).__name__)
    print(e)
