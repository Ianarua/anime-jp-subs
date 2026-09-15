# 架构：五步流水线 + 模块职责

## 一句话

输入一集**没有日语字幕轨**的 mkv，输出同一个文件（**只加一条字幕轨**，音视频一帧不动）：
日语轨带平假名注音、字体一起内封、日语轨排在所有字幕最前面并设为默认。

---

## 数据流

```
   Anime/2026.7/某番/E11.mkv
            │
            │ ① 判定：ffprobe 有没有 jpn 字幕轨？ 有 → 跳过（幂等）
            ▼
   ┌──────────────────────┐
   │ ② 抽音频             │  ffmpeg -map 0:<音频轨> -vn -ac 1 -ar 16000
   │    .tmp_E11/audio.wav│  16kHz 单声道（Whisper 最合适的输入）
   └──────────┬───────────┘
              │
              │ ③ 听写：faster-whisper large-v3（日语，词级时间戳，VAD 250ms/50ms）
              │     ↳ 默认 CPU/GPU 自动选；CUDA 起不来会自动退回 CPU
              ▼
   ┌──────────────────────┐
   │ ④ 断句               │  silero VAD 找停顿 → 词整块落在语音片 → DP 拼行
   │    [(起, 止, 文本)]   │  详见 docs/segmentation.md
   │    ↳ 第二遍补漏       │  对"字幕空档"关掉 VAD 重听，救回被 VAD 滤掉的句子
   └──────────┬───────────┘
              │
              │ ⑤ 出字幕文件：注音 ASS（janome 读音 + PIL 量字宽 + 换算行高缩放）
              ▼
   ┌──────────────────────┐
   │ ⑥ 内封               │  mkvmerge 流拷贝加轨 + --attach-file 内附字体
   │                      │  → mkvpropedit 打 language=jpn / name=Japanese / 默认轨
   │                      │  → mkvmerge -s 只留第一日语 + 第一中文
   └──────────┬───────────┘
              │
              │ ⑦ 自验证：独立 ffprobe 复核（语言标签 / 轨道顺序 / 只剩两条字幕 / 音视频 codec 未变）
              ▼
      E11.mkv（替换原文件；os.replace，不留备份）
```

---

## 模块职责

| 模块 | 干什么 | 备注 |
|---|---|---|
| `cli.py` | 子命令入口：`process`（默认）/ `scan` / `doctor` / `version`；dev 分支还有 `review` / `score` | 不写子命令时按 `process` 处理，兼容老用法 |
| `pipeline.py` | 五步流水线本体 + 断句算法 + 内封与自检 + 运行汇总 | **2049 行，还没拆**（见下面「后续」） |
| `furigana.py` | 注音：Janome 分词/读音、送假名对齐、人名词典、字体度量（行高缩放补偿）、ASS 排版（注音用独立 Dialogue 行 + `\pos`） | 可单独跑：`python furigana.py --video E11.mkv` 生成 ASS |
| `common.py` | 底座：工具路径解析（环境变量 → config.ini → PATH → 项目 `tools/` → 本机默认）、自动下载、配置分层、子进程、时间戳、SRT 读写、日志、`doctor` | 所有模块都依赖它 |
| `qa/`（**dev 分支**） | `common.py`（读字幕正文）+ `review/`（评审页）+ `score/`（拿人工标记打分） | 用户版没有这些 |
| `fonts/MPLUS1Code-VF.ttf` | 随项目分发的注音字体（OFL），内附进 mkv | 观众没装字体也能正确渲染 |

根目录的 `run_jp_sub.py`（.bat 调它）和 `furigana.py` 只是**启动壳**，真正代码都在 `src/`。

---

## 几个关键设计

### 工具路径：系统里已有的永远优先

```
环境变量(ANIME_JP_SUB_FFMPEG / _FFPROBE / _MKVMERGE / _MKVPROPEDIT / _MODEL_DIR / _TOOLS_DIR)
  → config.ini 的 [paths]
  → PATH（shutil.which）
  → 项目 tools\（自动下载的兜底）
  → 本机默认路径
```

候选找到后还要**真跑一次版本命令**才算采用（PATH 里有个坏掉的 exe 会自动跳过）。
`tools\` 只是别人机器上没有 ffmpeg/MKVToolNix 时的兜底，**不会锁死用户自己装的版本**。

### 外部程序自动下载（ffmpeg / MKVToolNix）

- **只下缺的那一份**；交互式问一句 `[y/N]`，`--download-tools` 直接下；
  **非交互（脚本/管道/定时任务）只提示，绝不偷偷下**；
- 断点续传（`tools\_download\*.part`）；官方 sha256 拿得到就校验（拿不到明说"跳过校验"）；
- 装完**真跑一次版本命令**才算数；`tools\` 已 gitignore，删掉即还原，不装进系统、不改 PATH。

### whisper 模型：**不自动下**

3GB 的模型由用户自己下、手动放进 `models\large-v3\`（仓库必须是 **`Systran/faster-whisper-large-v3`**，
CTranslate2 格式、正好 5 个文件）。`check_model()` 只检查 + 打印"去哪儿下哪几个文件"，缺就直接退出。

### 自验证：不信工具自报

`mkvmerge` 打印 "muxed ok" 不等于成功。`verify_muxed()` 会独立 `ffprobe` 复核：
新轨 `language=jpn` + `title=Japanese`、日语轨排在最前、只剩两条字幕、音视频 codec 未变
（附件只比数量——同一个附件在原始文件和重封装后 codec_name 报得不一样，踩过）。**过了才替换原文件**。

### 日志

控制台输出的同时追加写一份到被扫描目录下的 `anime_jp_sub.log`（`[paths] log_dir` 或
`ANIME_JP_SUB_LOG_DIR` 可改位置，`ANIME_JP_SUB_NO_LOG=1` 关掉）。跑完还有一份汇总：
扫描 N 集 / 已有日语轨跳过 M 集 / 新处理成功 X 集（含平均耗时）/ 失败 Y 集（逐条列原因）。

---

## 分支与仓库

| 分支 | 内容 |
|---|---|
| `main` | **用户版**：只有用户用得上的东西（`process` / `scan` / `doctor` / `version`） |
| `dev` | main + **开发工具**：`qa/`（评审页、打分）以及以后的实验脚本；提交前先在 dev 上验证 |

规矩：`main` 有了新的用户可见提交之后，**把 main 合进 dev**，保持 dev 是超集；反过来不要拿 dev 覆盖 main。

---

## 后续（还没做）

`pipeline.py` 现在 2049 行、74 个函数，一个人扛了 6 件事。计划按函数族拆成
`media/`（probe / audio / mux）、`asr/`（vad / whisper / recover）、`segment/`（grammar / retime / dp / cn），
`pipeline.py` 只留编排层。规矩是**函数名不动、测试不改**，每搬一个模块立刻跑
「159 个单元测试 + 44/80 打分 + 端到端跑一集副本」三连回归，任何一步不过就单独 revert 那一个提交。
