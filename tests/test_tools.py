# -*- coding: utf-8 -*-
"""外部工具（ffmpeg / MKVToolNix）查找与自动下载的测试。

这些用例**不联网、不下载任何东西**：
  * 查找顺序用打桩的 _tool_runs 验证（不依赖机器上装没装）；
  * 下载 / 校验 / 解压用本地 file:// 和现造的 zip 验证；
  * "非交互绝不偷偷下 200MB" 是硬要求，专门有一条用例盯着。
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from anime_jp_sub import common  # noqa: E402


class FakeStdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


class TestToolLookup(unittest.TestCase):
    """查找顺序：环境变量 → config.ini → PATH → 项目 tools/。"""

    def setUp(self):
        common._TOOL_CACHE.clear()
        common._TOOL_OK.clear()
        common._CONFIG_CACHE.clear()

    def tearDown(self):
        common._TOOL_CACHE.clear()
        common._TOOL_OK.clear()
        common._CONFIG_CACHE.clear()

    def _layout(self, d):
        """造一套假的候选文件（内容是垃圾无所谓，_tool_runs 被打桩）。"""
        d = Path(d)
        env_exe = d / "env_ffmpeg.exe"
        env_exe.write_bytes(b"x")
        path_dir = d / "pathbin"
        path_dir.mkdir()
        (path_dir / "ffmpeg.exe").write_bytes(b"x")
        tools = d / "tools"
        (tools / "ffmpeg" / "bin").mkdir(parents=True)
        (tools / "ffmpeg" / "bin" / "ffmpeg.exe").write_bytes(b"x")
        return env_exe, path_dir / "ffmpeg.exe", tools

    def test_lookup_order(self):
        with tempfile.TemporaryDirectory() as d:
            env_exe, path_exe, tools = self._layout(d)
            env = {"ANIME_JP_SUB_FFMPEG": str(env_exe),
                   "PATH": str(Path(d) / "pathbin"),
                   "ANIME_JP_SUB_TOOLS_DIR": str(tools)}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(common, "_tool_runs", lambda p, k: True):
                # 1. 环境变量优先
                self.assertEqual(common.find_tool_info("ffmpeg")[0], str(env_exe))
                common._TOOL_CACHE.clear()
                # 2. 没设环境变量 → 系统 PATH
                os.environ.pop("ANIME_JP_SUB_FFMPEG")
                common._CONFIG_CACHE.clear()
                self.assertEqual(common.find_tool_info("ffmpeg")[0], str(path_exe))
                common._TOOL_CACHE.clear()
                # 3. PATH 里也没有 → 项目 tools/
                os.environ["PATH"] = ""
                self.assertEqual(common.find_tool_info("ffmpeg")[0],
                                 str(tools / "ffmpeg" / "bin" / "ffmpeg.exe"))
                self.assertIn("tools", common.find_tool_info("ffmpeg")[1])
                common._TOOL_CACHE.clear()
                # 4. 连 tools/ 都没有 → 找不到（开源版没有"作者本机默认路径"那一档）
                os.environ["ANIME_JP_SUB_TOOLS_DIR"] = str(Path(d) / "nothing")
                self.assertEqual(common.find_tool_info("ffmpeg"), ("", ""))

    def test_broken_candidate_is_skipped(self):
        """PATH 里那份跑不起来时不能采用（比如坏掉的 ffmpeg），要往后找。
        注：这里打桩 run() 而不是真去执行一个假的 .exe——沙箱/杀软会拦住这种文件。"""
        with mock.patch.object(common, "run", lambda cmd, **kw: (1, "不是有效的 Win32 程序")):
            self.assertFalse(common._tool_runs("X:/bin/a.exe", "ffmpeg"))
        with mock.patch.object(common, "run", lambda cmd, **kw: (0, "")):
            self.assertFalse(common._tool_runs("X:/bin/b.exe", "ffmpeg"))   # 没输出也不算
        with mock.patch.object(common, "run", lambda cmd, **kw: (0, "ffmpeg version 9.0.1")):
            self.assertTrue(common._tool_runs("X:/bin/c.exe", "ffmpeg"))
        with mock.patch.object(common, "run", side_effect=OSError("boom")):
            self.assertFalse(common._tool_runs("X:/bin/d.exe", "ffmpeg"))

    def test_version_args_match_each_tool(self):
        """回归：ffmpeg / ffprobe 只认单横线 -version，写成 --version 会被当参数错误。"""
        seen = []
        with mock.patch.object(common, "run",
                               lambda cmd, **kw: (seen.append(cmd), (0, "v"))[1]):
            for key in ("ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit"):
                common._tool_runs(f"X:/bin/{key}.exe", key)
        self.assertEqual([c[1] for c in seen], ["-version", "-version", "--version", "--version"])

    def test_missing_tool_message_mentions_download_tools(self):
        with mock.patch.object(common, "find_tool_info", lambda k: ("", "")):
            with self.assertRaises(RuntimeError) as cm:
                common.find_tool("ffmpeg")
            self.assertIn("--download-tools", str(cm.exception))

    def test_tools_dir_env_override(self):
        with mock.patch.dict(os.environ, {"ANIME_JP_SUB_TOOLS_DIR": r"X:\some\where"}):
            self.assertEqual(str(common.tools_dir()), r"X:\some\where")


class TestDownload(unittest.TestCase):

    def test_sha256_ok_and_mismatch(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.bin"
            f.write_bytes(b"hello")
            good = hashlib.sha256(b"hello").hexdigest()
            with mock.patch.object(common, "_http_read",
                                   lambda url, timeout=60: f"{good}  a.bin".encode()):
                ok, msg = common._check_sha256(f, "http://x/a.bin.sha256")
            self.assertTrue(ok)
            with mock.patch.object(common, "_http_read",
                                   lambda url, timeout=60: ("0" * 64).encode()):
                ok, msg = common._check_sha256(f, "http://x/a.bin.sha256")
            self.assertFalse(ok)
            self.assertIn("sha256", msg)

    def test_sha256_unavailable_is_skipped_but_said_out_loud(self):
        """拿不到官方校验文件时跳过校验——但必须在输出里说清楚，不能假装校验过了。"""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.bin"
            f.write_bytes(b"hello")
            def boom(url, timeout=60):
                raise OSError("no net")
            with mock.patch.object(common, "_http_read", boom):
                ok, msg = common._check_sha256(f, "http://x/a.bin.sha256")
            self.assertTrue(ok)
            self.assertIn("跳过校验", msg)

    def test_download_file_from_local_url(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "src.bin"
            src.write_bytes(b"abc123" * 1000)
            dest = d / "sub" / "out.bin"
            ok, msg = common.download_file(src.as_uri(), dest)
            self.assertTrue(ok, msg)
            self.assertEqual(dest.read_bytes(), src.read_bytes())
            self.assertFalse((d / "sub" / "out.bin.part").exists())

    def test_download_file_restarts_when_server_ignores_range(self):
        """断点续传的坑：服务器不认 Range 时会整份重发，绝不能把新内容接在旧 .part 后面。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "src.bin"
            src.write_bytes(b"FRESH" * 500)
            dest = d / "out.bin"
            part = d / "out.bin.part"
            part.write_bytes(b"OLDGARBAGE" * 100)
            ok, msg = common.download_file(src.as_uri(), dest)
            self.assertTrue(ok, msg)
            self.assertEqual(dest.read_bytes(), src.read_bytes())

    def test_zip_extract_takes_marker_folder_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            z = d / "pkg.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("ffmpeg-9.0.1-essentials_build/bin/ffmpeg.exe", b"FF")
                zf.writestr("ffmpeg-9.0.1-essentials_build/bin/ffprobe.exe", b"FP")
                zf.writestr("ffmpeg-9.0.1-essentials_build/doc/readme.txt", b"DOC")
                zf.writestr("mkvtoolnix/mkvmerge.exe", b"MK")
                zf.writestr("mkvtoolnix/qt6core.dll", b"DLL")
            n = common._zip_one_dir(z, "ffmpeg.exe", d / "out")
            self.assertEqual(n, 2)
            self.assertEqual((d / "out" / "ffmpeg.exe").read_bytes(), b"FF")
            self.assertFalse((d / "out" / "readme.txt").exists())
            # only：只解 mkvmerge.exe，别把几百 MB 的 Qt 库也搬过来
            n2 = common._zip_one_dir(z, "mkvmerge.exe", d / "out2", only={"mkvmerge.exe"})
            self.assertEqual(n2, 1)
            self.assertFalse((d / "out2" / "qt6core.dll").exists())

    def test_zip_extract_missing_marker(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            z = d / "pkg.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("whatever/readme.txt", b"x")
            self.assertEqual(common._zip_one_dir(z, "ffmpeg.exe", d / "out"), 0)

    def test_latest_mkvtoolnix_version_from_index_html(self):
        html = ('<tr><td></td><td><a href="./0.4.2/"><span class="name">0.4.2/</span></a>'
                '</td></tr><tr><td><a href="./96.0/"><span class="name">96.0/</span></a>'
                '</td></tr><tr><td><a href="./101.0/"><span class="name">101.0/</span></a>'
                '</td></tr><tr><td><a href="./old-runtime/">'
                '<span class="name">old-runtime/</span></a></td></tr>')
        with mock.patch.object(common, "_http_read",
                               lambda url, timeout=60: html.encode("utf-8")):
            self.assertEqual(common._latest_mkvtoolnix_version(), "101.0")

    def test_latest_mkvtoolnix_version_raises_on_unknown_page(self):
        with mock.patch.object(common, "_http_read", lambda url, timeout=60: b"<html>nope</html>"):
            with self.assertRaises(RuntimeError):
                common._latest_mkvtoolnix_version()


class TestEnsureTools(unittest.TestCase):

    def test_all_present_does_nothing(self):
        with mock.patch.object(common, "find_tool", lambda k, required=True: "C:/x.exe"):
            self.assertTrue(common.ensure_tools(("ffmpeg", "mkvmerge")))

    def test_non_interactive_never_downloads(self):
        """非交互（脚本 / 管道 / 定时任务）绝不能偷偷下 200MB。"""
        calls = []
        with mock.patch.object(common, "find_tool", lambda k, required=True: ""), \
             mock.patch.object(common, "_INSTALLERS",
                               {"ffmpeg": lambda *a, **kw: calls.append("ffmpeg") or (True, "ok"),
                                "mkvtoolnix": lambda *a, **kw: calls.append("mkv") or (True, "ok")}), \
             mock.patch.object(common.sys, "stdin", FakeStdin(False)):
            buf = io.StringIO()
            with mock.patch.object(common.sys, "stdout", buf):
                ok = common.ensure_tools(("ffmpeg", "mkvmerge"))
        self.assertFalse(ok)
        self.assertEqual(calls, [])
        self.assertIn("--download-tools", buf.getvalue())

    def test_interactive_answer_no_skips(self):
        calls = []
        with mock.patch.object(common, "find_tool", lambda k, required=True: ""), \
             mock.patch.object(common, "_INSTALLERS",
                               {"ffmpeg": lambda *a, **kw: calls.append("ffmpeg") or (True, "ok")}), \
             mock.patch.object(common.sys, "stdin", FakeStdin(True)), \
             mock.patch("builtins.input", lambda prompt="": "n"):
            buf = io.StringIO()
            with mock.patch.object(common.sys, "stdout", buf):
                ok = common.ensure_tools(("ffmpeg",))
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_auto_download_installs_then_rechecks(self):
        state = {"done": False}

        def fake_install(*a, **kw):
            state["done"] = True
            return True, "ok"

        with mock.patch.object(common, "find_tool",
                               lambda k, required=True: "X:/tools/ffmpeg.exe" if state["done"] else ""), \
             mock.patch.object(common, "_INSTALLERS",
                               {"ffmpeg": fake_install, "mkvtoolnix": fake_install}), \
             mock.patch.object(common, "_TOOL_CACHE", {}):
            buf = io.StringIO()
            with mock.patch.object(common.sys, "stdout", buf):
                ok = common.ensure_tools(("ffmpeg", "ffprobe"), auto_download=True)
        self.assertTrue(ok)
        self.assertTrue(state["done"])

    def test_auto_download_reports_failure(self):
        with mock.patch.object(common, "find_tool", lambda k, required=True: ""), \
             mock.patch.object(common, "_INSTALLERS",
                               {"ffmpeg": lambda *a, **kw: (False, "网络不通")}):
            buf = io.StringIO()
            with mock.patch.object(common.sys, "stdout", buf):
                ok = common.ensure_tools(("ffmpeg",), auto_download=True)
        self.assertFalse(ok)
        self.assertIn("网络不通", buf.getvalue())

    def test_install_failure_when_still_missing(self):
        """装完了还是找不到 → 必须报失败，不能报成功。"""
        with mock.patch.object(common, "find_tool", lambda k, required=True: ""), \
             mock.patch.object(common, "_INSTALLERS",
                               {"ffmpeg": lambda *a, **kw: (True, "ok")}):
            buf = io.StringIO()
            with mock.patch.object(common.sys, "stdout", buf):
                ok = common.ensure_tools(("ffmpeg",), auto_download=True)
        self.assertFalse(ok)
        self.assertIn("还是找不到", buf.getvalue())


class TestDoctor(unittest.TestCase):

    def test_doctor_does_not_crash(self):
        buf = io.StringIO()
        with mock.patch.object(common.sys, "stdout", buf):
            rc = common.doctor()
        self.assertIn(rc, (0, 1))
        self.assertIn("环境检查", buf.getvalue())
        self.assertIn("自动下载目录", buf.getvalue())


class TestRememberProjectPath(unittest.TestCase):
    """程序每次运行都把项目位置记到 %LOCALAPPDATA%\\anime-jp-sub\\home.txt，
    供"复制到别的文件夹的 .bat"找回项目。写不了要**安静地放弃**（锁定环境的机器，
    以前让 .bat 写会蹦出 cmd 自己的 Access is denied.，2>nul 也压不住）。"""

    def test_writes_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "localappdata"
            local.mkdir()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local)}), \
                 mock.patch.object(common, "project_root", lambda: Path(d) / "proj"):
                common.remember_project_path()
                f = local / "anime-jp-sub" / "home.txt"
                self.assertTrue(f.is_file())
                first = f.stat().st_mtime_ns
                common.remember_project_path()          # 内容没变 → 不再写盘
            self.assertEqual(f.read_text(encoding="utf-8").strip(),
                             str(Path(d) / "proj"))
            self.assertEqual(f.stat().st_mtime_ns, first)

    def test_unwritable_target_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(Path(d) / "nope" / "x")}), \
                 mock.patch.object(common, "project_root", lambda: Path(d) / "proj"), \
                 mock.patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
                common.remember_project_path()          # 不该抛

    def test_no_project_root_is_a_noop(self):
        with mock.patch.object(common, "project_root", lambda: None):
            common.remember_project_path()              # 不该抛、不该写任何东西


class TestConsoleEncoding(unittest.TestCase):
    """中文 Windows 的控制台是 GBK，打印 `✓`/`✗`/`⚠` 这种符号会 UnicodeEncodeError，
    把整条命令打断（实测：用户在 PowerShell 里跑 `run_jp_sub.py doctor` 直接崩）。
    现在：输出里只用 GBK 编得出来的符号，并且给 stdout/stderr 加 errors=replace 兜底。"""

    def test_doctor_output_is_gbk_encodable(self):
        buf = io.StringIO()
        with mock.patch.object(common.sys, "stdout", buf):
            rc = common.doctor()
        self.assertIn(rc, (0, 1))
        buf.getvalue().encode("gbk")        # 编不出来就会抛 UnicodeEncodeError

    def test_setup_console_makes_stream_never_raise(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="gbk", errors="strict")
        with mock.patch.object(common.sys, "stdout", stream):
            common.setup_console()
            self.assertEqual(stream.errors, "replace")
            print("✓ 这个符号 GBK 编不出来，但不能再崩")     # 加了 replace 就不该抛
            stream.flush()
        text = raw.getvalue().decode("gbk")
        self.assertIn("这个符号", text)                     # 中文照常

    def test_no_unencodable_symbols_in_messages(self):
        """回归：以后别再往输出里塞 ✓ ✗ ⚠（在 GBK 控制台上会崩）。"""
        from anime_jp_sub import pipeline
        for mod in (common, pipeline):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            for ch in ("✓", "✗", "⚠"):
                # 允许出现在注释/文档字符串里（不会被打出来），但不允许出现在 print 或返回的提示里
                for line in src.splitlines():
                    code = line.split("#", 1)[0]
                    if ch in code and "print" in code:
                        self.fail(f"{mod.__name__} 的输出里有 {ch}：{line.strip()}")


class TestGlobalConfigLocation(unittest.TestCase):
    """拆包成 src/ 之后很容易踩的坑：全局 config.ini 被找成 src/anime_jp_sub/config.ini，
    而新人按 README 是把 config.example.ini 复制到**项目根**的，于是"我明明配了却没生效"。"""

    def test_config_ini_in_project_root_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "config.ini").write_text("[paths]\nffmpeg = C:\\fake\\ffmpeg.exe\n",
                                             encoding="utf-8")
            saved_root = common.project_root
            try:
                common.project_root = lambda: root
                common._CONFIG_CACHE.clear()
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("ANIME_JP_SUB_CONFIG", None)
                    self.assertIn(root / "config.ini", common._config_paths())
                    self.assertEqual(common.setting("paths", "ffmpeg"), r"C:\fake\ffmpeg.exe")
                    common._CONFIG_CACHE.clear()
                    self.assertEqual(str(common.tools_dir()), str(root / "tools"))
            finally:
                common.project_root = saved_root
                common._CONFIG_CACHE.clear()

    def test_env_var_still_wins(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "my.ini"
            f.write_text("[paths]\nffmpeg = C:\\env\\ffmpeg.exe\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"ANIME_JP_SUB_CONFIG": str(f)}):
                common._CONFIG_CACHE.clear()
                try:
                    self.assertEqual(common.setting("paths", "ffmpeg"), r"C:\env\ffmpeg.exe")
                finally:
                    common._CONFIG_CACHE.clear()


if __name__ == "__main__":
    unittest.main()
