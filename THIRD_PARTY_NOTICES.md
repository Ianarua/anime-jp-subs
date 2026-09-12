# 第三方组件与许可

本项目**不打包、不随仓库分发**下列外部程序与模型，需要使用者自行安装/下载。
下面列出它们的许可，方便你确认自己的用法是否合规。

程序提供外部程序的「自动下载」（`--download-tools`）：从**官方源**下载到项目自己的 `tools\`
目录，不写进系统、不放进仓库。第三方组件的许可仍然约束这些下载下来的副本——例如 FFmpeg 的
essentials 构建是 GPL、MKVToolNix 是 GPL-2.0，自己再分发时要遵守它们的许可。
**Whisper 模型不做自动下载**，由使用者自己下载放进 `models\`。

| 组件 | 用途 | 许可 | 备注 |
|---|---|---|---|
| [FFmpeg](https://ffmpeg.org/) | 抽音频、读流信息 | LGPL / GPL（取决于构建） | **以外部程序方式调用**，不修改、不链接；自行安装 |
| [MKVToolNix](https://mkvtoolnix.download/)（mkvmerge / mkvpropedit） | 内封、打轨道标签 | GPL-2.0 | 同样是外部程序调用 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 日语听写 | MIT | pip 依赖 |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) | 推理后端 | MIT | pip 依赖 |
| [OpenAI Whisper large-v3 模型](https://huggingface.co/openai/whisper-large-v3) | 听写模型 | MIT | 需自行下载（约 3GB） |
| [Janome](https://github.com/mocobeta/janome) | 形态素分析（查汉字读音） | BSD-3-Clause | pip 依赖，内含 IPADIC |
| [Pillow](https://python-pillow.org/) | 读字体字宽 | MIT-CMU | pip 依赖 |

## 仓库里唯一随项目分发的字体

`src/anime_jp_sub/fonts/MPLUS1Code-VF.ttf`

- 字体：**M PLUS 1 Code**（作者：coz-m / Fontworks Inc.）
- 许可：**SIL Open Font License 1.1**（可自由使用、修改、再分发，含商用）
- 用途：注音字幕的排版与显示；生成时会内附进 mkv，观众无需另装字体

OFL 要求：再分发时保留版权声明与许可文本（字体文件的 name 表里已含），
且**不得单独售卖字体本身**。本项目只是把字体随软件一起分发，符合许可要求。

## 为什么不用系统的日文字体

Windows 自带的 MS Gothic、以及常见的 BIZ UDGothic 属于**商业/受限许可字体**，
不能随开源仓库分发，所以项目自带一份开放许可的等宽黑体。
