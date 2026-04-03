import os
from google import genai
from google.genai import types


def generate() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "AIzaSyBYLz-qkFePjy3MnuMnwBm0K90-4YS79qo")
    client = genai.Client(api_key=api_key)

    model = "gemini-3-flash-preview"
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text="请用一句话介绍上海。"),
            ],
        ),
    ]

    tools = [
        types.Tool(
            googleSearch=types.GoogleSearch(),
        ),
    ]

    generate_content_config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(
            thinking_level="HIGH",
        ),
        tools=tools,
    )

    for chunk in client.models.generate_content_stream(
        model=model,
        contents=contents,
        config=generate_content_config,
    ):
        print(chunk.text, end="")


if __name__ == "__main__":
    generate()
