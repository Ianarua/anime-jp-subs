# anime-jp-sub

给**日语动画的 mkv** 自动配上**带平假名注音的日语字幕**，再内封回原文件。

从网上下载的片子常常只有中/英等翻译字幕，唯独**没有日语字幕**。

这个工具听写日语原声 → 给汉字标上平假名读音 → 内封进 mkv。看片时选中日语轨，汉字上方就是读音，可以边看边记。

只支持 **Windows**。不重编码视频音频、不烧进画面。

---

## 它给你什么

- **日语字幕带平假名注音**：汉字标的读音；片假名标对应的平假名。
- **内封，不烧录**：只是往 mkv 里加一条字幕轨，画质音质一帧不动；不想要了删掉轨就行。
- **中文轨保留**：日语轨排在最前面并设为默认，中文轨保留。
- **字体一起内附**：注音用的 M PLUS 1 Code 会一起封进 mkv。
- **幂等**：已经有日语字幕轨的集数会自动跳过，重复跑同一个文件夹不会做二次处理。
- **去除无用字幕**：其他语言的字幕轨会被删掉，只留日语 + 中文。

> **断句是听日语自己断的**，句子在哪儿断由识别模型自己判断，每句的起止时间再用人声检测（silero VAD）量出来的换气停顿对齐，最后按「一行放得下」拼成一条条字幕。
>
> 中文轨只影响「保留哪条中文轨」，不影响日语断句。

---

## 长什么样

日语轨每句都带注音，读音是**压在汉字上方的小字**（比主字幕小一半左右）：

```
おれ  きみ  まちが
俺と君は間違いなく友人だね
```

片假名也会被标上对应的平假名，例如 `カタカナ` 上面会写 `かたかな`——刚学日语、片假名还不熟的时候正好边看边记。

- 1080p 片源默认：主字幕 60px、注音 27px、距底边 50px；换个分辨率按画面高度等比缩放，所以在 720p / 4K / 不同尺寸的屏幕上看到的大小是一致的。
- OP / ED 那几分钟没有字幕，这是正常的。

---

## 安装

一共四步：装 Python → 装依赖 → 准备两个外部程序 → 放好模型；最后跑一次 `doctor` 检查。

### 1. 装 Python 3.10 或 3.11

去 [python.org](https://www.python.org/downloads/windows/) 下载安装包，安装时**勾上「Add Python to PATH」**。装完开一个新的 PowerShell / cmd 窗口，`python -V` 能看到版本号就成。

### 2. 装依赖

在项目目录（= 你克隆/解压出来的那个文件夹）里**双击 `setup.bat`**，它会自动：

1. 找到能用的 Python
2. 建好虚拟环境 `.venv`
3. 装齐依赖（几百 MB，要等几分钟）
4. 最后跑一次环境自检，并把"接下来做什么"打在窗口里

窗口结束前会停住，方便你看结果。

### 3. 外部程序 ffmpeg + MKVToolNix

**三选一**

1. 可以让它自己下（见上面第 2 步）。
2. 在运行`run_jp_sub.bat`时也会检测一遍。
3. 可以自定义路径：见**config.ini**配置

如果发现机器上没有这两个程序，会问一句 `现在自动下载吗？[y/N]`——答 `y` 就下载到**项目自己的`tools\` 目录**（约 200MB）。

- 只下**缺的那一个**：机器上已经装好的永远优先，不会覆盖、也不改你的 PATH
- 来源都是官方（ffmpeg 走 gyan.dev，MKVToolNix 走 mkvtoolnix.download），下完会校验 sha256
- 只写进项目的 `tools\`，不想要了直接删掉这个目录就还原

> 主动补工具：`run_jp_sub.bat doctor --download-tools`

### 4. 模型（要自己下）

模型是语音识别用的，体积大又是独立授权，所以本工具**不自动下**，请你下好手动放进去。

需要这 5 个文件：

```
model.bin      config.json      tokenizer.json      preprocessor_config.json      vocabulary.json
```

放进 `项目目录\models\large-v3`（如果没有 `large-v3` 这个子目录，需要自己建）。

**仓库别认错**：要的是 **CTranslate2 格式**的 `Systran/faster-whisper-large-v3`。（`openai/whisper-large-v3` 是 Transformers 格式，里面**没有 `model.bin`**，faster-whisper 用不了。）

下载地址：

```
原站：https://huggingface.co/Systran/faster-whisper-large-v3/tree/main
国内镜像：https://hf-mirror.com/Systran/faster-whisper-large-v3/tree/main
```

### 5. 自检

方法一：`run_jp_sub.bat doctor`方法二：双击`setup.bat`


它会逐项报出 `ffmpeg / ffprobe / mkvmerge / mkvpropedit`、Python 包、模型目录在不在，并且**写明每个程序是从哪找来的**（环境变量 / config.ini / 系统 PATH / 项目 tools/）。**有 ✗ 就先解决它**——真正开跑前也会做同样的检查，缺什么会直接告诉你怎么补。

---

## 怎么用

### 日常用法：双击 `run_jp_sub.bat`

`.bat` 处理的是**它自己所在的文件夹**（复制到哪儿就处理哪儿，会递归找里面的 mkv）。

1. **第一步**：在项目目录里跑：双击项目里的 `run_jp_sub.bat`，它会记忆你的项目目录。
2. **第二步**：复制到番剧文件夹里双击：把这个 `run_jp_sub.bat` 复制到任何一个番剧 / 季度文件夹里双击都能用，处理的还是它所在的那个文件夹（子文件夹里的 mkv 也会一起扫到）。

### 进阶：命令行

在**项目目录**里开一个 cmd 窗口（或 PowerShell，PowerShell 里前面要加 `.\`），然后：

```bat
run_jp_sub.bat                            :: 处理它自己所在的文件夹
run_jp_sub.bat "D:\Anime\2026.7"          :: 处理指定文件夹（会递归找 mkv）
run_jp_sub.bat "D:\Anime\2026.7\某番\E11.mkv"   :: 只处理某一集
run_jp_sub.bat scan "D:\Anime\2026.7"     :: 只看会处理哪些，不碰文件
run_jp_sub.bat doctor                     :: 环境自检
run_jp_sub.bat --download-tools           :: 自检并把缺的 ffmpeg/MKVToolNix 下到 tools\
run_jp_sub.bat --keep-srt                 :: 保留中间字幕文件（排查问题用）
```

不带参数时处理的是 `.bat` 自己所在的文件夹；参数会原样交给程序，所以 `process` / `scan` / `doctor` / `version` 和 `--download-tools`、`--keep-srt`、`--no-furigana` 都能这么加。

> PowerShell 里必须写成 `.\run_jp_sub.bat doctor`（`.\` = "当前目录"，不加会报"无法加载模块"）。嫌麻烦就用 cmd，或者干脆双击 `.bat`。

### 常用参数

| 参数               | 作用                                                   |
| ------------------ | ------------------------------------------------------ |
| `--download-tools` | 缺 ffmpeg / MKVToolNix 时直接下到项目 `tools\`，不再问 |
| `--no-furigana`    | 不生成注音，退回普通日语 SRT 字幕轨                    |
| `--keep-srt`       | 保留中间生成的字幕文件（排查问题用，默认用完即删）     |

---

## 它每次会做什么

1. **看片源**：用 ffprobe 查这一集有没有日语字幕轨。有 → 跳过；没有 → 继续。

2. **抽音频**：把日语（没有就第一条）音轨转成 16kHz 单声道 wav，只读不写原文件。

3. **听写**：faster-whisper large-v3 识别日语原声，带词级时间戳。

4. **断句 + 注音**：先把识别出来的每句话（模型自己断的句）对齐到 VAD 量出来的换气停顿上，

再按「一行放得下」拼成一条条字幕；然后查词典标平假名，排版成 ASS。

5. **内封 + 自检 + 替换**：
   - 用 mkvmerge 把日语轨加进去（**stream copy，不重编码**），字体作为附件一起内附
   - 日语轨排到所有字幕轨最前、语言 `jpn`、轨道名 `Japanese`、默认选中；中文轨设为非默认
   - 其他语言的字幕轨删掉，只留日语 + 中文
   - 自动复核：语言标签对不对、字幕是不是两条、视频音频有没有被动过。**全部通过才替换原文件**；没通过就报错并删掉中间产物，原文件不动
   - 覆盖原文件（**不备份**，所以请确保磁盘够、下载器（如 qBittorrent）没锁着文件）

跑完打印一份汇总：扫到几集、跳过几集、成功几集（含平均耗时）、失败几集（逐条给原因）。同样的内容会追加写到被处理目录下的 `anime_jp_sub.log`。

---

## 跑完检查什么

- **PotPlayer 里「屏蔽 ASS 样式」必须是关的**（字幕 → 字幕样式）。开着的话 ASS 的定位和样式全部失效，注音会从汉字上方掉到屏幕底边、还可能被裁掉。
- 日语轨应该在最上面并且默认选中；中文轨还在、默认关闭。
- OP / ED 没字幕是正常的。
- 想确认字体内附成功：用 MKVToolNix 或 PotPlayer 看附件，能看到 `MPLUS1Code-VF.ttf`。
- 处理完的目录里不该残留 `.jp.mkv` / `.jpc.mkv` / `.tmp_*` 这些东西。

### 想调字号 / 位置 / 换程序路径：`config.ini`

**所有能调的东西都写在一个文件里**：复制 `config.example.ini` 成 `config.ini`，放在**项目根目录**（和 `run_jp_sub.py` 同一层）。想只影响某一部番，就把这份 `config.ini`放进那个番剧文件夹里（番剧级设置优先于全局）。

```ini
[paths]
; 装在 PATH 里的程序不用写；下面是"装在别处 / 想固定版本"时才需要
ffmpeg      = D:\ffmpeg\bin\ffmpeg.exe
ffprobe     = D:\ffmpeg\bin\ffprobe.exe
mkvmerge    = D:\MKVToolNix\mkvmerge.exe
mkvpropedit = D:\MKVToolNix\mkvpropedit.exe

; whisper 模型放哪。填"上一级目录"：它下面要有 large-v3 子目录，那 5 个文件在子目录里
model_dir   = D:\models

; 自动下载的 ffmpeg / MKVToolNix 放哪儿（默认：项目目录下的 tools\）
tools_dir   = D:\anime-jp-sub-tools

; 日志写哪儿（默认：写在你处理的那个文件夹里，文件名 anime_jp_sub.log）
log_dir     = D:\logs

[furigana]
main_px   = 60     ; 主字幕字号（1080p 基准；实际按画面高度等比换算，换分辨率不会跑偏）
bottom_px = 50     ; 字幕距底边多少像素
max_chars = 30     ; 每行最多几个字，超过就拆成前后相继的两条

[align]
mode      = jp     ; jp = 日语自己断句（默认）；cn = 按中文字幕轨的断句边界切（老做法）
```

程序找外部程序的顺序是 **环境变量 → `config.ini` → 系统 PATH → 项目 `tools\`**（先命中先用，系统里已经装好的永远优先）。

对应的环境变量（优先级比配置文件更高）：

| 环境变量                                                     | 作用                                     |
| ------------------------------------------------------------ | ---------------------------------------- |
| `ANIME_JP_SUB_FFMPEG` / `_FFPROBE` / `_MKVMERGE` / `_MKVPROPEDIT` | 指定某个外部程序                         |
| `ANIME_JP_SUB_MODEL_DIR`                                     | 指定模型所在的那一层目录                 |
| `ANIME_JP_SUB_TOOLS_DIR`                                     | 自动下载的外部程序放哪儿                 |
| `ANIME_JP_SUB_LOG_DIR` / `ANIME_JP_SUB_NO_LOG=1`             | 换日志位置 / 干脆不写日志                |
| `ANIME_JP_SUB_FONT`                                          | 换注音用的字体文件                       |
| `ANIME_JP_SUB_CONFIG`                                        | 指定另一份 `config.ini`                  |
| `ANIME_JP_SUB_HOME` / `ANIME_JP_SUB_PYTHON`                  | 告诉 `.bat` 项目在哪 / 用哪个 python.exe |

> 配了却不生效？多半是放错位置了——全局的 `config.ini` 要放**项目根目录**，不是 `src\` 里面。

---

## 人名词典怎么用

字典不认识的名字会读错（比如「小玉」被读成 こだま）。修正办法：在那个番剧的文件夹里放`furigana_dict.txt`——**第一次处理这部番时脚本会自动生成**，里面已经列好了候选词，你只管改。

格式是「左边写字幕里原样的词，右边写平假名读音，中间按一个 Tab」：

```
# ==== 我的词典（你自己写，脚本不会动这一段）====
小玉	たま
月水堂	げっすいどう

# ==== 候选（脚本自动生成，每次运行重写这一段）====
# 下面是从本片字幕里挑出的含汉字固有名詞（括号里是字典给的读音，可能是错的）。
#小玉	こだま	出现 23 次
```

用法：把候选行开头的 `#` 去掉就直接生效；自己知道读音的词写在「我的词典」那一段。片假名不用写（片假名→平假名是自动转换），需要写的只有汉字词。改完直接重跑就行，词典每次运行都会重新读。

> 局限：脚本只能挑出「字典认为是专有名词」的词，像「小玉」这种被归为普通名词的名字不会出现在候选里——看片时发现读错了，手动加一行就行。

---

## 常见问题

**提示「找不到 ffmpeg」/「找不到 mkvmerge」。**

1. 双击`setup.bat`
2. `run_jp_sub.bat doctor --download-tools`，让它自己下到项目的 `tools\`。
3. 也可以自己装好放进 PATH、写进 `config.ini`，或设环境变量（`ANIME_JP_SUB_FFMPEG` 等）。`doctor` 会告诉你缺什么、以及每个程序最终用的是哪一份。

**提示找不到 Whisper 模型。**模型要自己下（约 3GB）：把 `model.bin`、`config.json`、`tokenizer.json`、`preprocessor_config.json`、`vocabulary.json` 放进 `models\large-v3\`。仓库用 `Systran/faster-whisper-large-v3`，别下 `openai/whisper-large-v3`（那个没有 `model.bin`）。连不上 huggingface.co 就用 `hf-mirror.com`。

**注音没压在汉字上，跑到屏幕最底下还被裁掉了。** PotPlayer 开了「屏蔽 ASS 样式」（字幕 → 字幕样式），**关掉它**。

**内封时报「WinError 5 拒绝访问」。**下载器（如 qBittorrent）正在做种、占着文件句柄。暂停那个任务（或退出）再跑。已经生成的 `.jp.mkv` 是好的，也可以手动改名覆盖原文件，不必重跑。

**跑得很慢，显卡好像没被用上。**程序会先试 GPU，跑不动就自动改用 CPU（慢很多，一集十几分钟到几十分钟）并在日志里说明。想真正用上显卡，见下面「已知限制」里的 CUDA 运行库那条。

**我明明配了 `config.ini`，为什么不生效？**全局 `config.ini` 要放在**项目根目录**（和 `run_jp_sub.py` 同一层），不是放进 `src` 里面。只想影响一部番的话，放进那个番剧文件夹。拿不准就跑 `doctor` 看「来源」那一行。

**有几句话没有字幕，或者两句合成了一条长句。**字幕文本来自语音识别，识别错的地方注音也会跟着错。断句是听日语自己的换气停顿来的，碰到「连着说没有明显停顿」的地方偶尔会和原声不完全一致。

**自动下载卡住 / 下不动。**官方源在部分网络下会慢或连不上。已经下了一半的留在`tools\_download\`，重跑接着下；实在不行就自己装（`winget install Gyan.FFmpeg` +`winget install MoritzBunkus.MKVToolNix`），装完程序会优先用系统里的那份。

**日志在哪？**被处理目录下的 `anime_jp_sub.log`，每次运行追加。不想写日志就设 `ANIME_JP_SUB_NO_LOG=1`，想换地方就用 `config.ini` 的 `[paths] log_dir` 或环境变量 `ANIME_JP_SUB_LOG_DIR`。

**`.tmp_*` 目录是什么？**处理某一集时在片子旁边建的中间目录（放音频和字幕），正常结束会自动删掉；中途 Ctrl-C 掉的残留可以手动删。

**怎么彻底卸载？**删掉项目目录就行——`.venv\`、`models\`、`tools\` 。`config.ini`、日志、中间文件都不会污染你的其他目录。

---

## 已知限制

- **只支持 Windows、只处理 mkv**。
- **中文轨和音频不同步的地方，日语也不会凭空补上**：官方中文轨偶尔会错位，脚本按实测的语音时间归属，所以可能出现「有中文、没日语」的片段。
- **断句靠语音识别 + 换气停顿**：说得又快又没有停顿的地方（战斗喊叫、一口气念完的台词），断句可能和你的语感不一致。
- **识别错的地方注音一定跟着错**（战斗喊叫、快语速段落尤其明显）。
- **人名读音可能错**，靠 `furigana_dict.txt` 修（见上文）。
- **N 卡用户注意**：程序会用 CUDA，但 ctranslate2 还需要 CUDA 运行库里的`cublas64_12.dll` / `cublasLt64_12.dll`。只装了显卡驱动没装 CUDA 运行库时，程序会自动回退 CPU 并提示你。想用 GPU：从任何带 CUDA 的 Python 环境里找到这两个 dll，复制到`.venv\Lib\site-packages\ctranslate2\` 下即可。
- **没有 N 卡也能用**，只是慢（CPU 转录 1 集大约十几分钟到几十分钟，看 CPU）。
- **会覆盖原文件、不备份**：磁盘紧张或做种中的片子，请先暂停/复制一份再跑。处理过程中一旦自检不过就**不会替换**原文件。

---

## 挑片源的小技巧

能省掉整条流水线：**部分片源自带日语字幕轨**，跑之前用 `scan` 看一眼值不值得处理。

- **优先挑自带中文内封字幕的**：这样成品里日语 + 中文两条都在，随时能对照。标题里带「简繁」「SC/TC」「简繁日」的通常都有。只有日文/英文字幕的片源也能跑（日语断句不看中文轨），只是成品里没有中文轨可切。
- `.mkv` 大多是内封或原盘；`.mp4` 常见内嵌硬字幕（提不出来）。
- 标题写「**简繁日内封字幕**」的通常内封了日文轨，**可以直接用**；写「**简日内嵌**」的不能直接用。

---

## 项目目录里都有什么

| 文件 / 目录                             | 作用                                                   |
| --------------------------------------- | ------------------------------------------------------ |
| `setup.bat`                             | 首次安装：双击一下，自动建 `.venv` + 装依赖 + 自检     |
| `run_jp_sub.py` / `run_jp_sub.bat`      | 入口（`.bat` 是启动器，处理它所在的文件夹）            |
| `src\anime_jp_sub\`                     | 程序本体                                               |
| `src\anime_jp_sub\fonts\`               | 自带的注音字体 M PLUS 1 Code（OFL 许可，会内附进 mkv） |
| `requirements.txt`                      | Python 依赖清单                                        |
| `config.example.ini`                    | 配置样例，复制成 `config.ini` 生效                     |
| `models\`                               | 你自己下的 whisper 模型（约 3GB，git 忽略）            |
| `tools\`                                | 自动下载的 ffmpeg / MKVToolNix（git 忽略）             |
| `.venv\`                                | Python 虚拟环境（git 忽略）                            |
| `anime_jp_sub.log`、`furigana_dict.txt` | 运行日志、番剧的人名读音词典（生成在番剧文件夹里）     |

---

## 许可

- 本项目代码：**MIT**
- 随项目分发的字体：**M PLUS 1 Code**（SIL Open Font License 1.1，可自由再分发）
- 外部程序（FFmpeg、MKVToolNix）与 whisper 模型**不随仓库分发**，由你自行安装/下载，各自的许可见 `THIRD_PARTY_NOTICES.md`

---

## 开发者调优

这一节的东西在 `dev` 分支里。

```bat
run_jp_sub.bat review "D:\Anime\2026.7\某番"                 :: 生成"评审页"（已有就跳过，--force 重做）
run_jp_sub.bat process --keep-dump "D:\Anime\2026.7\某番"    :: 处理时留一份听写结果
run_jp_sub.bat score 标记.txt --baseline E11.jp.ass --dump E11.jp.dump.json -v
```

| 参数 | 作用 |
|---|---|
| `--force` | 只对 `review` 用：已有的评审页也重新生成 |
| `--keep-dump` | 只对 `process` 用：留一份 `.jp.dump.json`（听写 + VAD），给 `score` 打分用 |

### 评审页：逐条挑断句

断句是对是错机器测不出来，只能靠耳朵。`review` 会在每集旁边生成两个文件（**不改动 mkv，删掉即还原**）：

| 文件 | 用途 |
|---|---|
| `<集名>.review.html` | 双击用浏览器打开：每条字幕一行，标「并入上一条 / 并入下一条 / 拆分 / 这里漏字幕 / 备注」，标完点导出，得到一段纯文本 |
| `<集名>.review.txt` | 同样的清单，纯文本版（不想开浏览器时用） |

页面会顺手把**可疑点**标出来，都是"切分"类的毛病（识别错字不在内）：

| 标记 | 意思 |
|---|---|
| ⚠跨停顿 | 这一条字幕的显示区间盖住了一整段长静音——很可能是"两句被并成一句" |
| ⚠行首助詞 | 这一行从助詞开头（断点很可能该往前挪） |
| ⚠行尾起句 | 行尾是「いや / あの / え」这种只会起句的词（应该归下一行） |
| ⚠偏长 | 一条显示超过 8 秒或 24 字 |


### 打分

`score` 拿人工标记给切分算法打分。三样输入缺一不可：标记文本、**人当时审的那份产物**（`--baseline`，标记的行号属于它）、那一集的听写结果（`--dump`）。口径钉死：**合并**标记 = 那个词缝上不该有切点，**拆分**标记 = 该有切点，允许差一个词缝；`-v` 会把没通过的逐条列出来（带时间码，方便回听）。
