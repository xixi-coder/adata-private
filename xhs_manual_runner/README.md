# XHS Manual Runner 使用说明

这个目录用于从小红书直播回放 `m3u8` 链接拉取音视频，并用 `whisper-cli` 转写文本。

核心脚本：
- `run_from_last_link.sh`：从 `reply_links.txt` 读取最后一条链接并执行整条流水线（推荐）
- `xhs_pipeline.sh`：直接传入 `m3u8` 链接执行流水线

## 1. 环境依赖

请先确保以下命令可用：
- `ffmpeg`
- `whisper-cli`

并准备 Whisper 模型文件（默认会按以下顺序查找）：
- `$WHISPER_MODEL_PATH`（如果你显式设置了）
- `~/Downloads/ggml-medium.bin`
- `~/下载/ggml-medium.bin`
- `ggml-medium.bin`（当前工作目录）

## 2. 快速开始（推荐方式）

### 2.1 准备链接文件

编辑 `reply_links.txt`，每行一个链接，脚本会取最后一条非空且非注释行：

```txt
# 可写注释
https://example.com/xxx.m3u8
```

### 2.2 运行脚本

```bash
sh /Users/xixi/pythonProject/adata/xhs_manual_runner/run_from_last_link.sh
```

或给执行权限后直接运行：

```bash
chmod +x /Users/xixi/pythonProject/adata/xhs_manual_runner/run_from_last_link.sh
/Users/xixi/pythonProject/adata/xhs_manual_runner/run_from_last_link.sh
```

### 2.3 输出结果

输出在当前目录（`xhs_manual_runner`）下，包括：
- `output_<url_id>.mp4`（视频，非 fast 模式）
- `audio_<url_id>.mp3`（音频）
- `audio_<url_id>.mp3.txt`（转写文本）
- `source_<url_id>.url`（原始链接）
- `pipeline_YYYYMMDD_HHMMSS.log`（日志）

## 3. 直接运行 pipeline

如果你想跳过 `reply_links.txt`，可直接运行：

```bash
sh /Users/xixi/pythonProject/adata/xhs_manual_runner/xhs_pipeline.sh "<m3u8_url>" "/Users/xixi/pythonProject/adata/xhs_manual_runner"
```

## 4. 常用环境变量

- `WHISPER_MODEL_PATH`：模型路径
- `WHISPER_LANG`：语言，默认 `zh`
- `WHISPER_THREADS`：线程数，默认 CPU 核心数
- `FAST_AUDIO_ONLY`：`1` 表示跳过 mp4，仅生成 mp3（更快）
- `XHS_PIPELINE_PATH`：在 `run_from_last_link.sh` 中覆盖 pipeline 脚本路径

示例：

```bash
export WHISPER_MODEL_PATH="$HOME/Downloads/ggml-medium.bin"
export FAST_AUDIO_ONLY=1
sh /Users/xixi/pythonProject/adata/xhs_manual_runner/run_from_last_link.sh
```

## 5. 常见问题

### 5.1 `SyntaxError: invalid syntax`

如果你是这样运行：

```bash
python run_from_last_link.sh
```

这是解释器用错了。`run_from_last_link.sh` 是 Shell 脚本，不是 Python 脚本。

请改为：

```bash
sh run_from_last_link.sh
```

### 5.2 `Error: command not found: ffmpeg` 或 `whisper-cli`

说明依赖未安装或不在 `PATH`。安装后重新打开终端再执行。

### 5.3 `Error: Whisper model file not found`

设置 `WHISPER_MODEL_PATH` 到真实模型路径，或把模型放到默认查找位置。

### 5.4 `Error: no valid m3u8 URL found in reply_links.txt`

请在 `reply_links.txt` 末尾追加一条有效 `m3u8` 链接后重试。

## 6. 日常使用建议

- 长视频优先用 `FAST_AUDIO_ONLY=1`，更快拿到文本。
- 日志保留在 `pipeline_*.log`，失败时优先看日志末尾。
- 输出文件较大（尤其 mp4），建议定期清理旧文件。
