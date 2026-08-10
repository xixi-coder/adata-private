# YouTube 字幕 TXT

一个无需后端、无需 API Key 的 Chrome / Edge Manifest V3 扩展。它读取当前 YouTube 视频已有的官方字幕或自动字幕，可预览、复制并下载为 UTF-8 `.txt` 文件。

`1.1.0` 起支持捕获播放器带 Proof-of-Origin 令牌的字幕响应，解决直接请求字幕地址返回空正文的问题。

## 安装

1. 打开 `chrome://extensions/`（Edge 使用 `edge://extensions/`）。
2. 打开右上角“开发者模式”。
3. 点击“加载已解压的扩展程序”。
4. 选择目录：

```text
/Users/xixi/pythonProject/adata/browser_extensions/youtube-subtitles
```

## 使用

1. 打开一个带字幕的 YouTube 视频。
2. 点击页面右下角红色“字”按钮。
3. 选择字幕语言，并决定是否保留时间戳。
4. 点击“复制”或“下载 TXT”。

TXT 包含视频标题、频道、字幕语言、原视频链接和字幕正文。文件以 UTF-8 BOM 保存，中文在 Windows 记事本和 Excel 等软件中也能正确识别。

## 限制

- 只能导出 YouTube 已经提供的字幕，包括官方字幕和自动生成字幕。
- 没有字幕的视频不会自动进行语音识别；如需此能力，需要额外接入 Whisper 等转写服务。
- YouTube 页面结构可能变化。若插件提示无法读取，先刷新视频页；仍失败时需更新扩展的读取逻辑。
- 请只下载你有权使用的字幕，并遵守视频版权和 YouTube 服务条款。

## 本地测试

```bash
node test-core.js
```
