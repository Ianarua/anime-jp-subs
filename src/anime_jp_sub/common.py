#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.py —— anime_jp_sub 与 furigana 共用的底座：工具路径、子进程、时间戳。

工具路径（ffmpeg / ffprobe / mkvmerge / mkvpropedit）按下面的顺序找，先命中先用：
  1. 环境变量      ANIME_JP_SUB_FFMPEG / _FFPROBE / _MKVMERGE / _MKVPROPEDIT / _MODEL_DIR
  2. 配置文件      config.ini 的 [paths] 段（默认找脚本同目录，可用 ANIME_JP_SUB_CONFIG 指定）
  3. PATH          shutil.which 找得到就用（装过 ffmpeg/mkvtoolnix 的机器不用配）
  4. 项目 tools/   本脚本自动下载的兜底（见 ensure_tools）；只写项目目录，不碰系统

装过的永远优先：机器上已经有 ffmpeg / MKVToolNix 就用系统那份；一份都找不到的时候才会
问你一句"要不要自动下到项目里"（非交互运行不会偷偷下，见 ensure_tools）。下载只走官方源、
带 sha256 校验，装完还要真跑一次 --version 才算数。
"""

import configparser
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

CONFIG_NAME = "config.ini"

_ENV_NAMES = {
    "ffmpeg": "ANIME_JP_SUB_FFMPEG",
    "ffprobe": "ANIME_JP_SUB_FFPROBE",
    "mkvmerge": "ANIME_JP_SUB_MKVMERGE",
    "mkvpropedit": "ANIME_JP_SUB_MKVPROPEDIT",
}
# 在 PATH 里找的时候用的可执行文件名
_EXE_NAMES = {
    "ffmpeg": ("ffmpeg.exe", "ffmpeg"),
    "ffprobe": ("ffprobe.exe", "ffprobe"),
    "mkvmerge": ("mkvmerge.exe", "mkvmerge"),
    "mkvpropedit": ("mkvpropedit.exe", "mkvpropedit"),
}
# 四件套（顺序就是自检 / 报告的展示顺序）
TOOL_KEYS = ("ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit")
# "能不能跑"的验证命令：ffmpeg 那一族只认单横线，别写成 --version（会当参数错误）
_VERSION_ARGS = {
    "ffmpeg": ("-version",),
    "ffprobe": ("-version",),
    "mkvmerge": ("--version",),
    "mkvpropedit": ("--version",),
}
# 自动下载的单位：一个压缩包里装一组
_BUNDLES = {
    "ffmpeg": ("ffmpeg", "ffprobe"),
    "mkvtoolnix": ("mkvmerge", "mkvpropedit"),
}
# 项目 tools/ 里每个程序固定在哪儿（下载器按同一个布局放）
_TOOL_RELPATHS = {
    "ffmpeg": ("ffmpeg", "bin", "ffmpeg.exe"),
    "ffprobe": ("ffmpeg", "bin", "ffprobe.exe"),
    "mkvmerge": ("mkvtoolnix", "mkvmerge.exe"),
    "mkvpropedit": ("mkvtoolnix", "mkvpropedit.exe"),
}
_TOOL_CACHE = {}        # key -> (路径, 来源说明)，只缓存命中的
_TOOL_OK = {}           # 路径 -> 能不能跑（同一个 exe 不反复试）


def _config_paths(folder=None):
    """配置文件的查找顺序（后面的覆盖前面的）：
       1. 全局 config.ini：ANIME_JP_SUB_CONFIG 指定的文件 → **项目根**下的 config.ini
          → 包目录下的 config.ini（拆包前的老位置，兼容用）
       2. 番剧文件夹里的 config.ini（只影响那一个番剧，用来单独调参数）

    ⚠ 项目根那一项是必须的：拆成 src/ 布局后 `Path(__file__).parent` 指的是
    `src/anime_jp_sub/`，新人把 config.example.ini 复制到项目根（按 README 说的做）
    会**读不到**（踩过）。
    """
    paths = []
    env = os.environ.get("ANIME_JP_SUB_CONFIG")
    if env:
        paths.append(Path(env))
    else:
        root = project_root()
        if root:
            paths.append(root / CONFIG_NAME)
        paths.append(Path(__file__).resolve().parent / CONFIG_NAME)
    if folder:
        paths.append(Path(folder) / CONFIG_NAME)
    return paths


def load_config(folder=None):
    """读配置，返回 {section: {key: value}}。不存在就返回空 dict（全走代码里的默认值）。"""
    out = {}
    for p in _config_paths(folder):
        if not p.is_file():
            continue
        cfg = configparser.ConfigParser()
        try:
            cfg.read(p, encoding="utf-8-sig")
        except Exception as e:                      # noqa: BLE001
            print(f"  [warn] 读配置 {p} 失败：{e}（忽略这个文件）")
            continue
        for sec in cfg.sections():
            out.setdefault(sec, {}).update({k: v.strip() for k, v in cfg.items(sec)})
    return out


_CONFIG_CACHE = {}


def config(folder=None):
    """带缓存的 load_config（按 folder 分别缓存）。"""
    key = str(folder or "")
    if key not in _CONFIG_CACHE:
        _CONFIG_CACHE[key] = load_config(folder)
    return _CONFIG_CACHE[key]


def setting(section, key, default=None, folder=None):
    """取一个配置项：没配就返回 default。"""
    return config(folder).get(section, {}).get(key, default)


def _tool_runs(path, key):
    """这个 exe 真能跑起来吗（跑一次版本命令，结果缓存）。
    PATH 里可能有个坏的 / 半截的 ffmpeg，先验证再采用，别到抽音频才发现。"""
    if path in _TOOL_OK:
        return _TOOL_OK[path]
    ok = False
    try:
        code, out = run([path] + list(_VERSION_ARGS.get(key, ("--version",))), timeout=30)
        ok = code == 0 and bool(out.strip())
    except Exception:                               # noqa: BLE001
        ok = False
    _TOOL_OK[path] = ok
    return ok


def tool_version(path, key="ffmpeg"):
    """取版本号第一行（给 doctor 显示用）；跑不起来返回 ""。"""
    try:
        code, out = run([path] + list(_VERSION_ARGS.get(key, ("--version",))), timeout=30)
    except Exception:                               # noqa: BLE001
        return ""
    if code != 0:
        return ""
    for line in out.splitlines():
        if line.strip():
            return line.strip()
    return ""


def find_tool_info(key):
    """按 环境变量 → config.ini → PATH → 项目 tools/ 找一个可执行文件。
    返回 (路径, 来源说明)；找不到返回 ("", "")。结果按 key 缓存（命中的才缓存）。"""
    if key in _TOOL_CACHE:
        return _TOOL_CACHE[key]
    cand = os.environ.get(_ENV_NAMES[key]) or config().get("paths", {}).get(key)
    if cand:
        p = Path(cand)
        if p.is_file() and _tool_runs(str(p), key):
            hit = (str(p), f"环境变量/config.ini（{_ENV_NAMES[key]}）")
            _TOOL_CACHE[key] = hit
            return hit
        print(f"  [warn] 配置里的 {key} 不能用（路径不存在或跑不起来）: {cand}")
    for exe in _EXE_NAMES[key]:
        found = shutil.which(exe)
        if found and _tool_runs(found, key):
            hit = (found, "系统 PATH")
            _TOOL_CACHE[key] = hit
            return hit
    bundled = tools_dir().joinpath(*_TOOL_RELPATHS[key])
    if bundled.is_file() and _tool_runs(str(bundled), key):
        hit = (str(bundled), "项目 tools/（自动下载的）")
        _TOOL_CACHE[key] = hit
        return hit
    return ("", "")


def find_tool(key, required=True):
    """找一个可执行文件，返回路径（找不到且 required 时报错）。"""
    path, _src = find_tool_info(key)
    if path:
        return path
    if required:
        raise RuntimeError(
            f"找不到 {key}。装好它（放进 PATH）、或设环境变量 {_ENV_NAMES[key]}、"
            f"或在 {CONFIG_NAME} 的 [paths] 里写路径；"
            f"也可以让它自动下到项目 tools/ 里：加 --download-tools 参数（见 README）。")
    return ""


def missing_tools(keys=TOOL_KEYS):
    """哪些程序还没就位（返回 key 列表）。"""
    return [k for k in keys if not find_tool(k, required=False)]


# ===== 外部工具自动下载（只写项目自己的 tools/，不碰系统） =====
# 只下官方源，不打包进仓库（体积 + 许可）。下完 sha256 校验 + 真跑一次版本命令才算数。
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
MKVTOOLNIX_INDEX = "https://mkvtoolnix.download/windows/releases/"
_UA = {"User-Agent": f"anime-jp-sub (windows; python {platform.python_version()})"}
_MKV_CLI = ("mkvmerge.exe", "mkvpropedit.exe", "mkvextract.exe", "mkvinfo.exe")


def tools_dir():
    """自动下载的工具放哪儿：环境变量 ANIME_JP_SUB_TOOLS_DIR → config.ini 的
    [paths] tools_dir → 项目根下 tools/。pip 安装（没有项目根）时退回
    LocalAppData/anime-jp-sub/tools。"""
    env = os.environ.get("ANIME_JP_SUB_TOOLS_DIR")
    if env:
        return Path(env)
    cfg = config().get("paths", {}).get("tools_dir")
    if cfg:
        return Path(cfg)
    root = project_root()
    if root:
        return root / "tools"
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "anime-jp-sub" / "tools"


def _http_read(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA),
                                timeout=timeout) as r:
        return r.read()


def _check_sha256(path, sha256_url):
    """拿 <url>.sha256 校验文件（两个官方源都有），返回 (通过?, 说明)。
    校验文件拿不到就跳过——但不能假装校验过，说明里会写清楚。"""
    try:
        txt = _http_read(sha256_url, timeout=60).decode("utf-8", "replace")
    except Exception as e:                          # noqa: BLE001
        return True, f"（拿不到官方 sha256，跳过校验：{e}）"
    m = re.search(r"\b([0-9a-fA-F]{64})\b", txt)
    if not m:
        return True, "（官方 sha256 文件格式不认识，跳过校验）"
    want = m.group(1).lower()
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got != want:
        return False, (f"sha256 对不上（文件下坏了）：期望 {want[:16]}…，实际 {got[:16]}…")
    return True, "sha256 校验通过"


def download_file(url, dest, sha256_url=None, label=""):
    """下载 url 到 dest（支持断点续传 + 进度显示 + sha256 校验）。返回 (成功?, 说明)。
    先写 dest.part，全部校验通过才改名成 dest——中途断了下次接着下。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    done = part.stat().st_size if part.exists() else 0
    headers = dict(_UA)
    if done:
        headers["Range"] = f"bytes={done}-"
        print(f"  {label}发现没下完的临时文件（{done / 1048576:.1f}MB），接着下...")
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r, part.open("ab" if done else "wb") as f:
            if done and getattr(r, "status", 200) != 206:   # 服务器不支持续传 → 重头来
                f.seek(0)
                f.truncate()
                done = 0
            total = int(r.headers.get("Content-Length") or 0) + done
            t0 = last = time.monotonic()
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last >= 0.5:
                    last = now
                    pct = f"{done * 100 / total:5.1f}%" if total else "  ?  "
                    mb = f"{done / 1048576:.1f}MB" + (f"/{total / 1048576:.1f}MB" if total else "")
                    sp = done / max(now - t0, 0.001) / 1048576
                    print(f"\r  {label}{pct} {mb}  {sp:.1f}MB/s", end="", flush=True)
        print()
    except Exception as e:                          # noqa: BLE001
        print()
        return False, f"下载失败：{e}（已下 {done / 1048576:.1f}MB，重跑能接着下）"
    if sha256_url:
        ok, msg = _check_sha256(part, sha256_url)
        print(f"  {label}{msg}")
        if not ok:
            part.unlink()
            return False, msg
    part.replace(dest)
    return True, "ok"


def _zip_one_dir(zip_path, marker, dest_dir, only=None):
    """从 zip 里找出 marker（如 ffmpeg.exe）所在的目录，把该目录的文件解到 dest_dir。
    返回解出来的文件数（0 = 没找到 marker）。only 给一组小写文件名时只解这些。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        hit = next((i for i in infos
                    if PurePosixPath(i.filename).name.lower() == marker.lower()), None)
        if hit is None:
            return 0
        folder = PurePosixPath(hit.filename).parent
        for i in infos:
            p = PurePosixPath(i.filename)
            if p.parent != folder:
                continue
            if only is not None and p.name.lower() not in only:
                continue
            with z.open(i) as src, (dest_dir / p.name).open("wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
    return n


def _latest_mkvtoolnix_version():
    """官方 releases 目录页里挑版本号最大的一个（页面是 Caddy 的文件列表）。"""
    html = _http_read(MKVTOOLNIX_INDEX, timeout=60).decode("utf-8", "replace")
    names = re.findall(r'<span class="name">([^<]+)/</span>', html)
    vers = [n for n in names if re.fullmatch(r"\d+(\.\d+)*", n)]
    if not vers:
        raise RuntimeError("没从官方目录页里读到版本号（页面结构变了？）")
    return max(vers, key=lambda s: [int(x) for x in s.split(".")])


def install_ffmpeg(tools_root=None):
    """下 ffmpeg（官方 essentials zip，约 110MB）解到 tools/ffmpeg/bin。返回 (成功?, 说明)。"""
    root = Path(tools_root or tools_dir())
    dl = root / "_download"
    zip_path = dl / "ffmpeg-release-essentials.zip"
    print("  下载 ffmpeg（官方 gyan.dev，约 110MB）...")
    ok, msg = download_file(FFMPEG_ZIP_URL, zip_path,
                            sha256_url=FFMPEG_ZIP_URL + ".sha256", label="ffmpeg ")
    if not ok:
        return False, msg
    bin_dir = root / "ffmpeg" / "bin"
    try:
        n = _zip_one_dir(zip_path, "ffmpeg.exe", bin_dir)
    except Exception as e:                          # noqa: BLE001
        return False, f"解压失败：{e}"
    if not n:
        return False, "压缩包里没找到 ffmpeg.exe（官方包结构变了？）"
    zip_path.unlink()
    print(f"  解出 {n} 个文件到 {bin_dir}")
    return True, "ok"


def install_mkvtoolnix(tools_root=None):
    """下 MKVToolNix（官方 64 位 zip，约 88MB）解出 mkvmerge/mkvpropedit。返回 (成功?, 说明)。"""
    root = Path(tools_root or tools_dir())
    dl = root / "_download"
    try:
        ver = _latest_mkvtoolnix_version()
        bits = "32" if platform.machine().lower() in ("x86", "i386", "i686") else "64"
        base = f"{MKVTOOLNIX_INDEX}{ver}/mkvtoolnix-{bits}-bit-{ver}"
        url = base + ".zip"
        print(f"  下载 MKVToolNix {ver}（官方，约 88MB）...")
        ok, msg = download_file(url, dl / f"mkvtoolnix-{ver}.zip",
                                sha256_url=url + ".sha256", label="mkvtoolnix ")
    except Exception as e:                          # noqa: BLE001
        return False, f"找官方最新版失败：{e}"
    if not ok:
        return False, msg
    dest = root / "mkvtoolnix"
    try:
        n = _zip_one_dir(dl / f"mkvtoolnix-{ver}.zip", "mkvmerge.exe", dest,
                         only={x.lower() for x in _MKV_CLI})
    except Exception as e:                          # noqa: BLE001
        return False, f"解压失败：{e}"
    if not n:
        return False, "压缩包里没找到 mkvmerge.exe（官方包结构变了？）"
    (dl / f"mkvtoolnix-{ver}.zip").unlink()
    print(f"  解出 {n} 个文件到 {dest}")
    return True, "ok"


_INSTALLERS = {"ffmpeg": install_ffmpeg, "mkvtoolnix": install_mkvtoolnix}
_BUNDLE_DESC = {"ffmpeg": "ffmpeg + ffprobe（约 110MB）",
                "mkvtoolnix": "mkvmerge + mkvpropedit（约 88MB）"}


def ensure_tools(keys=TOOL_KEYS, auto_download=False):
    """确认要用的外部程序就位，缺的可以自动下到项目 tools/ 里。返回 True = 齐了。
    - auto_download=True（命令行 --download-tools）：直接下，不问
    - 交互式（双击 .bat 跑）：问一句 [y/N]，把体积说清楚
    - 非交互（脚本 / 管道调用）：只提示，绝不偷偷下 200MB"""
    need = missing_tools(keys)
    if not need:
        return True
    bundles = [b for b in ("ffmpeg", "mkvtoolnix")
               if any(k in need for k in _BUNDLES[b])]
    print(f"\n缺少外部程序：{'、'.join(need)}")
    for b in bundles:
        print(f"  （可以自动下到项目 tools/ 里：{_BUNDLE_DESC[b]}，不装进系统）")
    if auto_download:
        print("  开始自动下载（--download-tools 不再询问）...")
    elif sys.stdin.isatty():
        try:
            ans = input("  现在自动下载吗？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if not ans.startswith("y"):
            print("  跳过。也可以自己装好放进 PATH，或写进 config.ini，见 README。")
            return False
    else:
        print("  非交互运行，不自动下载。加 --download-tools，或自己装好放进 PATH。")
        return False
    for b in bundles:
        ok, msg = _INSTALLERS[b]()
        if not ok:
            print(f"  [X] {b} 装失败：{msg}")
            return False
    for k in need:                                  # 装完把缓存清掉重新找一遍
        _TOOL_CACHE.pop(k, None)
    still = missing_tools(keys)
    if still:
        print(f"  [X] 装完了还是找不到：{'、'.join(still)}")
        return False
    print("  外部程序就位 √")
    return True


def project_root():
    """代码所在项目的根目录（往上找 pyproject.toml）。
    pip 安装的情况下找不到，返回 None——那时候模型路径必须靠配置指定。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def find_model_dir():
    """whisper 模型目录：环境变量 → config.ini → 项目根下的 models/。
    ⚠ 不能按 common.py 的位置算——它在 src/anime_jp_sub/ 里，会算到包里面去（踩过）。"""
    cand = (os.environ.get("ANIME_JP_SUB_MODEL_DIR")
            or config().get("paths", {}).get("model_dir"))
    if cand:
        return str(Path(cand))
    return str((project_root() or Path.cwd()) / "models")


def __getattr__(name):
    """让 common.FFMPEG / common.MKVPROPEDIT / common.MODEL_DIR 在**用到时**才解析路径。
    这样只用到 ffprobe 的模块（比如 furigana 单独跑）不会因为没装 mkvmerge 就 import 失败。"""
    if name in ("FFMPEG", "FFPROBE", "MKVMERGE", "MKVPROPEDIT"):
        return find_tool(name.lower())
    if name == "MODEL_DIR":
        return find_model_dir()
    raise AttributeError(f"module 'common' has no attribute {name!r}")


def remember_project_path():
    """把项目位置记到 %LOCALAPPDATA%\\anime-jp-sub\\home.txt。

    为什么：`run_jp_sub.bat` 被复制到番剧文件夹后，得靠这个文件找回项目
    （查找顺序 同目录 → ANIME_JP_SUB_HOME → 这个文件）。
    为什么放在 Python 里写、而不是让 .bat 写：cmd 在**重定向失败**时会自己打印
    `Access is denied.`，`2>nul` 也压不住（实测）；Python 这边 catch 一下就安静了。
    写不了（锁定环境的机器）就直接放弃：绝不报错、绝不打扰用户。
    """
    try:
        root = project_root()
        local = os.environ.get("LOCALAPPDATA")
        if root is None or not local:
            return
        d = Path(local) / "anime-jp-sub"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "home.txt"
        want = str(root)
        if f.is_file() and f.read_text(encoding="utf-8", errors="replace").strip() == want:
            return                                  # 内容没变就别反复写盘
        f.write_text(want + "\n", encoding="utf-8")
    except Exception:                               # noqa: BLE001
        pass


def setup_console():
    """让控制台输出不会因为"编不出来的字符"把整条命令打断。

    实测踩过：中文 Windows 的控制台是 GBK，`doctor` 打印 `√`/`×` 直接
    `UnicodeEncodeError` 崩掉（用户在 PowerShell 里跑第一条命令就这样）。
    这里只加 `errors="replace"`：编不出来的字符退化成 `?`，其余照常——**不改编码**，
    改了反而会满屏乱码（控制台仍按 GBK 解释我们写出去的字节）。
    另外：README 里的示例命令要让 PowerShell 也能用（相对路径前面要 `.\`）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:                           # noqa: BLE001
            pass


def run(cmd, timeout=600, capture=True):
    """执行子进程（列表传参，不拼 shell 字符串）。返回 (returncode, stdout+stderr)。

    capture=False 时把子进程的输出直接透传到控制台——下模型（3GB）这种事必须让用户
    看得见进度条，不能闷在那里半小时像个死机。"""
    if not capture:
        cp = subprocess.run(cmd, timeout=timeout)
        return cp.returncode, ""
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=timeout)
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


# ===== 时间戳 =====
SRT_TS_RE = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def fmt_srt_ts(sec):
    """秒 -> SRT 时间戳 HH:MM:SS,mmm"""
    h, rem = divmod(int(sec * 1000), 3600000)
    m, rest = divmod(rem, 60000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_ass_ts(sec):
    """秒 -> ASS 时间戳 H:MM:SS.cc"""
    h, rem = divmod(int(round(sec * 100)), 360000)
    m, rest = divmod(rem, 6000)
    s, cs = divmod(rest, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def parse_srt_timeline(text):
    """从 SRT 文本解析 [(start, end), ...]（秒）。"""
    out = []
    for line in text.splitlines():
        m = SRT_TS_RE.search(line)
        if m:
            g = [int(x) for x in m.groups()]
            out.append((g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0,
                        g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0))
    return out


def load_srt(path):
    """读 SRT 文件，返回 [(start, end, text), ...]（时间单位秒）。"""
    out, cur = [], None
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        m = SRT_TS_RE.search(line)
        if m:
            g = [int(x) for x in m.groups()]
            cur = [g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0,
                   g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0, []]
            out.append(cur)
        elif cur is not None and line.strip() and not line.strip().isdigit():
            cur[2].append(line.strip())
    return [(s, e, " ".join(t)) for s, e, t in out]


def log_path(target):
    """本次运行的日志写到哪：默认放在被扫描的目录（番剧文件夹）下，叫 anime_jp_sub.log。
    想换地方就设 config.ini 的 [paths] log_dir 或环境变量 ANIME_JP_SUB_LOG_DIR。"""
    p = Path(target)
    base = p if p.is_dir() else p.parent
    d = (os.environ.get("ANIME_JP_SUB_LOG_DIR")
         or config().get("paths", {}).get("log_dir"))
    return Path(d) / "anime_jp_sub.log" if d else base / "anime_jp_sub.log"


class Tee:
    """把 print 的输出同时写到控制台和日志文件（控制台照旧看得见）。"""

    def __init__(self, path, console=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = self.path.open("a", encoding="utf-8")
        self.console = console or sys.stdout

    def write(self, s):
        self.console.write(s)
        self.f.write(s)

    def flush(self):
        self.console.flush()
        try:
            self.f.flush()
        except Exception:                           # noqa: BLE001
            pass

    def close(self):
        try:
            self.f.close()
        except Exception:                           # noqa: BLE001
            pass


def doctor(download_tools=False):
    """检查外部依赖是否就位（给命令行用）；download_tools=True 时顺手把缺的装上。"""
    setup_console()
    print("环境检查：")
    ok = True
    if download_tools:
        ensure_tools(auto_download=True)
    for key in TOOL_KEYS:
        p, src = find_tool_info(key)
        if not p:
            ok = False
            print(f"  × {key:12s} 没找到"
                  f"（放进 PATH / 写进 config.ini / 或加 --download-tools 自动下）")
            continue
        lines = [x for x in tool_version(p, key).splitlines() if x.strip()]
        ver = lines[0][:60] if lines else "?"
        print(f"  √ {key:12s} {p}\n      来源 {src}｜{ver}")
    print(f"  自动下载目录: {tools_dir()}（装这里不影响系统，删掉即可还原）")
    missing_model = _model_problem()
    if missing_model is None:
        print(f"  √ 模型        就位（{find_model_dir()}）")
    else:
        ok = False
        print("  × 模型        没就位：")
        for line in missing_model.splitlines():
            print("    " + line.strip())
    for mod in ("faster_whisper", "janome", "PIL"):
        try:
            __import__(mod)
            print(f"  √ python 包 {mod}")
        except Exception as e:                      # noqa: BLE001
            ok = False
            print(f"  × python 包 {mod}: {e}")
    return 0 if ok else 1


def _model_problem():
    """模型就位吗？就位返回 None，否则返回给用户看的说明。
    真正的检查在 pipeline.check_model()（认那 5 个文件），这里函数内 import 是为了
    不造成 common ↔ pipeline 的循环导入；万一环境不正常就退化成"看目录在不在"。
    """
    try:
        from . import pipeline
        return pipeline.check_model()
    except Exception:                               # noqa: BLE001
        md = Path(find_model_dir())
        return None if md.is_dir() else f"模型目录不存在：{md}"


if __name__ == "__main__":
    sys.exit(doctor())
