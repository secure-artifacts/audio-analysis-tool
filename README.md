# 录音分析工具

本地浏览器工具：上传法语音频，用 Groq Whisper 分段转录，再生成法中对照和录音评价。

## 需要

- Python 3.11 或 3.12
- ffmpeg
- Groq API Key

## 启动

首次运行先安装依赖：

```powershell
pip install -r requirements.txt
```

Windows：

```powershell
.\start_windows.ps1
```

macOS / Linux：

```bash
sh start_mac_linux.sh
```

也可以直接运行：

```powershell
python server.py
```

打开：

```text
http://127.0.0.1:8765
```

如果 `python` 不可用，Windows 可以试：

```powershell
py -3.11 server.py
```

## 说明

- API Key 只在本机服务内存里使用，不写入配置文件。
- 长音频会先用 ffmpeg 切成 8 分钟 FLAC，再逐段发送给 Groq。
- 整理、翻译、录音评价也使用 Groq 文本模型，默认 `meta-llama/llama-4-scout-17b-16e-instruct`。
- 遇到 Groq `429` 限流会等待后继续。
- 说话人目前默认是“发言人”，页面里可以手动改成“听众/其他”。

## 自检

```powershell
python server.py --self-test
```
