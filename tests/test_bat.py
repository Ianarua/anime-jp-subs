# -*- coding: utf-8 -*-
"""run_jp_sub.bat 的测试（Windows 专用，非 Windows 自动跳过）。

这里盯的是"新人第一次双击"这条路上踩过的坑：
  * 批处理里混中文 + `chcp 65001` → cmd.exe 会把某一行从中间截断、把碎片当命令执行，
    用户实测报的是 `'的' is not recognized as an internal or external command`。
    → 所以这个 .bat 必须**纯 ASCII**（中文提示交给 Python 打印）。
  * `set "X=...\"` / `if "%X%"=="\"` 这种"反斜杠紧贴引号"的写法会让 cmd 报
    `The syntax of the command is incorrect.` → 路径一律写成 `"%PROJ%\file"`。
  * 用户把 .bat 单独复制到番剧文件夹（旧版写死绝对路径所以能用；新版只认 `%~dp0`），
    必须能找到项目：同目录 → `ANIME_JP_SUB_HOME` → 记住的 `home.txt`。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BAT = ROOT / "run_jp_sub.bat"
SETUP = ROOT / "setup.bat"


def _run_bat(folder, env_extra=None, timeout=120):
    """跑 .bat（stdin 接空，等价于 `pause` 直接跳过）。返回 (退出码, 输出)。"""
    env = dict(os.environ)
    env.pop("ANIME_JP_SUB_HOME", None)
    env.update(env_extra or {})
    cp = subprocess.run(["cmd", "/c", "run_jp_sub.bat"], cwd=str(folder),
                        stdin=subprocess.DEVNULL, capture_output=True,
                        encoding="utf-8", errors="replace", env=env, timeout=timeout)
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


@unittest.skipUnless(os.name == "nt", "批处理测试只在 Windows 上跑")
class TestRunBat(unittest.TestCase):

    def test_bat_is_ascii_only_and_crlf(self):
        """回归：.bat 里出现非 ASCII 字符，cmd 就可能把某行截断当命令执行。"""
        for f in (BAT, SETUP):
            data = f.read_bytes()
            self.assertEqual([b for b in data if b > 127], [],
                             f"{f.name} 必须保持纯 ASCII")
            self.assertEqual(data.count(b"\r\n"), data.count(b"\n"),
                             f"{f.name} 必须全用 CRLF 行尾")

    def test_setup_bat_must_sit_in_the_project_folder(self):
        """setup.bat 被放到别处时要说清楚，而不是静默失败。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            shutil.copy2(SETUP, d / "setup.bat")
            cp = subprocess.run(["cmd", "/c", "setup.bat"], cwd=str(d),
                                stdin=subprocess.DEVNULL, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=120)
            out = (cp.stdout or "") + (cp.stderr or "")
            self.assertIn("not in the project folder", out)

    def test_setup_bat_reports_when_python_cannot_be_used(self):
        """Python 不可用（坏 shim / 没装）时要给提示 + ANIME_JP_SUB_PYTHON 这条出路。
        用一个假的 ANIME_JP_SUB_PYTHON（什么都做不了）来模拟。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "run_jp_sub.py").write_text('print("STUB")\n', encoding="utf-8")
            shutil.copy2(SETUP, d / "setup.bat")
            fake = d / "fakepy.bat"
            fake.write_text("@exit /b 0\r\n", encoding="ascii")
            cp = subprocess.run(["cmd", "/c", "setup.bat"], cwd=str(d),
                                stdin=subprocess.DEVNULL, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=120,
                                env={**os.environ, "ANIME_JP_SUB_PYTHON": str(fake)})
            out = (cp.stdout or "") + (cp.stderr or "")
            self.assertIn("Could not create .venv", out)

    def test_finds_program_next_to_itself(self):
        """放在项目目录里（旁边就是 run_jp_sub.py）→ 直接用。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            la = d / "localappdata"
            la.mkdir()
            shutil.copy2(BAT, d / "run_jp_sub.bat")
            (d / "run_jp_sub.py").write_text(
                "import sys\nprint('STUB ARGV', sys.argv[1:])\n", encoding="utf-8")
            rc, out = _run_bat(d, {"LOCALAPPDATA": str(la)})
            self.assertIn(f"Program : {d}", out)
            # 不带参数 → 处理 .bat 自己所在的文件夹
            if "STUB ARGV" in out:      # 机器上没装 python 时不会跑到这里
                self.assertIn(str(d), out)

    def test_remembers_project_for_copies_elsewhere(self):
        """复制到别的文件夹后，靠 home.txt 也能找到项目（用户的实际用法）。
        home.txt 是程序自己写的（common.remember_project_path），这里直接造出来。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            proj = d / "proj"
            anime = d / "anime"
            la = d / "localappdata"
            for p in (proj, anime, la):
                p.mkdir()
            shutil.copy2(BAT, proj / "run_jp_sub.bat")
            (proj / "run_jp_sub.py").write_text('print("STUB OK")\n', encoding="utf-8")
            (la / "anime-jp-sub").mkdir()
            (la / "anime-jp-sub" / "home.txt").write_text(str(proj), encoding="utf-8")
            shutil.copy2(BAT, anime / "run_jp_sub.bat")
            rc, out = _run_bat(anime, {"LOCALAPPDATA": str(la)})
            self.assertIn(f"Program : {proj}", out)         # 项目是靠 home.txt 找到的
            self.assertIn(f"Scan dir: {anime}", out)        # 处理的是 .bat 所在的文件夹

    def test_arguments_are_passed_through(self):
        """run_jp_sub.bat doctor / scan "D:\\..." —— 参数原样交给程序。
        以前只认"第一个参数=要处理的文件夹"，所以 doctor 会被当成路径，白跑一趟。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            anime = d / "anime"
            anime.mkdir()
            shutil.copy2(BAT, anime / "run_jp_sub.bat")
            proj = d / "proj"
            proj.mkdir()
            (proj / "run_jp_sub.py").write_text('print("STUB OK")\n', encoding="utf-8")
            env = {**os.environ, "LOCALAPPDATA": str(d / "la"),
                   "ANIME_JP_SUB_HOME": str(proj)}
            for arg in ("doctor", "version"):
                cp = subprocess.run(["cmd", "/c", "run_jp_sub.bat", arg], cwd=str(anime),
                                    stdin=subprocess.DEVNULL, capture_output=True,
                                    encoding="utf-8", errors="replace", env=env, timeout=120)
                out = (cp.stdout or "") + (cp.stderr or "")
                self.assertIn(f"Options : {arg}", out)

    def test_env_var_works_too(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            proj = d / "proj"
            anime = d / "anime"
            la = d / "localappdata"
            for p in (proj, anime, la):
                p.mkdir()
            shutil.copy2(BAT, proj / "run_jp_sub.bat")
            (proj / "run_jp_sub.py").write_text('print("STUB OK")\n', encoding="utf-8")
            shutil.copy2(BAT, anime / "run_jp_sub.bat")
            rc, out = _run_bat(anime, {"LOCALAPPDATA": str(la),
                                       "ANIME_JP_SUB_HOME": str(proj)})
            self.assertIn(f"Program : {proj}", out)
            self.assertIn(f"Scan dir: {anime}", out)

    def test_says_clearly_when_project_cannot_be_found(self):
        """找不到项目时要给出可照做的提示，而不是闪退或一堆乱码。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            la = d / "localappdata"
            anime = d / "anime"
            la.mkdir()
            anime.mkdir()
            shutil.copy2(BAT, anime / "run_jp_sub.bat")
            rc, out = _run_bat(anime, {"LOCALAPPDATA": str(la)})
            self.assertEqual(rc, 1)
            self.assertIn("Cannot find the program", out)
            self.assertIn("ANIME_JP_SUB_HOME", out)
            self.assertIn("home.txt", out)

    def test_target_argument_is_used(self):
        """带参数时处理参数指定的文件夹，而不是 .bat 所在的文件夹。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            proj = d / "proj"
            anime = d / "anime"
            other = d / "other"
            la = d / "localappdata"
            for p in (proj, anime, other, la):
                p.mkdir()
            shutil.copy2(BAT, proj / "run_jp_sub.bat")
            (proj / "run_jp_sub.py").write_text('print("STUB OK")\n', encoding="utf-8")
            shutil.copy2(BAT, anime / "run_jp_sub.bat")
            env = dict(os.environ)
            env["LOCALAPPDATA"] = str(la)
            env["ANIME_JP_SUB_HOME"] = str(proj)
            cp = subprocess.run(["cmd", "/c", "run_jp_sub.bat", str(other)],
                                cwd=str(anime), stdin=subprocess.DEVNULL,
                                capture_output=True, encoding="utf-8",
                                errors="replace", env=env, timeout=120)
            out = (cp.stdout or "") + (cp.stderr or "")
            # 带参数时参数原样传给程序（形如 Options : <文件夹>），不再扫 .bat 自己的目录
            self.assertIn(f"Options : {other}", out)
            self.assertNotIn(f"Scan dir: {anime}", out)


if __name__ == "__main__":
    unittest.main()
