@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"

rem ===========================================================================
rem  One-time setup: create .venv, install the Python dependencies, self-check.
rem  Just double-click it (it must sit in the project folder).
rem
rem  ASCII-ONLY ON PURPOSE (same reason as run_jp_sub.bat): cmd.exe mis-parses a
rem  batch file that switches the code page and also contains non-ASCII text.
rem  Chinese messages are printed by the Python program instead.
rem ===========================================================================

echo ==========================================
echo   anime-jp-sub - one-time setup
echo ==========================================
echo.

if not exist "%HERE%run_jp_sub.py" (
    echo [X] This .bat is not in the project folder.
    echo     Put setup.bat next to run_jp_sub.py and double-click it there.
    goto :done
)

rem ---- find a Python that actually runs ----
rem  Candidates are *verified* (they must answer -V) instead of trusting  where :
rem  a broken shim on PATH (pyenv etc.) answers nothing and, if it is a .bat,
rem  would also swallow the rest of this script - hence  call  everywhere.
set "PYCMD="
if defined ANIME_JP_SUB_PYTHON set "PYCMD="%ANIME_JP_SUB_PYTHON%""
if defined PYCMD goto :have_py
call :try_python py -3
if defined PYCMD goto :have_py
call :try_python python
if defined PYCMD goto :have_py
echo [X] Python not found (tried: py -3, python).
echo     Install Python 3.10 or 3.11 from https://www.python.org/downloads/windows/
echo     and tick "Add Python to PATH" during installation, then run this file again.
echo     Already have a working python.exe somewhere else? Set ANIME_JP_SUB_PYTHON
echo     to its full path and run this file again:
echo         setx ANIME_JP_SUB_PYTHON "C:\Python311\python.exe"
goto :done

:try_python
rem  run the candidate in a child cmd: a broken shim (pyenv ships .bat shims that
rem  end with a plain  exit ) would otherwise terminate this script as well
cmd /c %* -V >nul 2>nul
if not errorlevel 1 set PYCMD=%*
exit /b 0

:have_py
echo [1/3] Python
call %PYCMD% -V
echo.

echo [2/3] Virtual environment (.venv)
if exist "%HERE%.venv\Scripts\python.exe" (
    echo       .venv already exists - skipping.
) else (
    call %PYCMD% -m venv "%HERE%.venv"
    if not exist "%HERE%.venv\Scripts\python.exe" (
        echo [X] Could not create .venv - see the messages above.
        goto :done
    )
    echo       created.
)
echo.

echo [3/3] Python dependencies (downloads a few hundred MB, be patient)
call "%HERE%.venv\Scripts\python.exe" -m pip install -r "%HERE%requirements.txt"
if errorlevel 1 (
    echo.
    echo [X] pip failed. If it is a network problem, try a mirror:
    echo     "%HERE%.venv\Scripts\python.exe" -m pip install -r "%HERE%requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
    goto :done
)

echo.
echo ==========================================
echo   Environment check
echo ==========================================
call "%HERE%.venv\Scripts\python.exe" "%HERE%run_jp_sub.py" doctor

echo.
echo Next steps:
echo   1) put the whisper model into  %HERE%models\large-v3\
echo      (5 files: model.bin config.json tokenizer.json preprocessor_config.json vocabulary.json)
echo      - see README "Install step 4" for the download links
echo   2) double-click run_jp_sub.bat inside an anime folder
echo      (or run:  run_jp_sub.bat "D:\Anime\2026.7")
echo.

:done
echo.
echo Press any key to close.
pause >nul
