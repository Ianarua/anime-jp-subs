@echo off
chcp 65001 >nul
setlocal
set BS=\
set "HERE=%~dp0"
set "HOMEFILE=%LOCALAPPDATA%\anime-jp-sub\home.txt"

rem ===========================================================================
rem  ASCII-ONLY ON PURPOSE.
rem  cmd.exe mis-parses a batch file that switches the code page (chcp) and
rem  also contains non-ASCII text: it splits a line in the middle and runs the
rem  fragment as a command ("'?' is not recognized as an internal or external
rem  command"). Chinese messages are printed by the Python program instead.
rem  Also avoid the patterns  set "X=...\"  /  if "%X%"=="\"  - cmd treats the
rem  trailing  \"  as an escaped quote and fails with "syntax of the command is
rem  incorrect". Paths are therefore joined as  "%PROJ%\file"  (a doubled
rem  backslash is fine for Windows).
rem
rem  Three ways to use this .bat:
rem    1) keep it in the project folder (next to run_jp_sub.py)
rem    2) run it once from the project folder, then copy it into any anime
rem       folder and double-click it there
rem    3) set ANIME_JP_SUB_HOME to the project folder
rem  What gets processed: the folder this .bat sits in, or the first argument.
rem ===========================================================================

echo ==========================================
echo   Anime JP Subtitle Auto-Generator
echo ==========================================
echo.

rem ---- 1) is the program right next to this .bat? ----
set "PROJ=%HERE%"
if exist "%PROJ%\run_jp_sub.py" goto :found

rem ---- 2) ANIME_JP_SUB_HOME ----
set "PROJ=%ANIME_JP_SUB_HOME%"
if defined PROJ if exist "%PROJ%\run_jp_sub.py" goto :found

rem ---- 3) remembered path (written when it runs from the project folder) ----
set "PROJ="
if exist "%HOMEFILE%" set /p PROJ=<"%HOMEFILE%"
if defined PROJ if exist "%PROJ%\run_jp_sub.py" goto :found

echo [X] Cannot find the program (run_jp_sub.py).
echo.
echo     Two ways to make this .bat work:
echo       1) put it in the project folder (the folder that has run_jp_sub.py),
echo          run it there once, then copy it into any anime folder
echo       2) or set the environment variable ANIME_JP_SUB_HOME to the
echo          project folder
echo.
echo     Remembered path file: %HOMEFILE%
echo.
pause
exit /b 1

:found
rem No argument  -> process the folder this .bat sits in.
rem Any argument -> pass it through unchanged, so all of these work:
rem   run_jp_sub.bat "D:\Anime\2026.7"        (process that folder)
rem   run_jp_sub.bat scan "D:\Anime\2026.7"   (only report)
rem   run_jp_sub.bat doctor                   (environment check)
rem   run_jp_sub.bat doctor --download-tools
rem   run_jp_sub.bat --keep-srt
set "TARGET=%HERE%"
rem a path ending with a backslash would escape the closing quote - strip it
if not "%TARGET:~2%"=="%BS%" if "%TARGET:~-1%"=="%BS%" set "TARGET=%TARGET:~0,-1%"
echo Program : %PROJ%
if "%~1"=="" (echo Scan dir: %TARGET%) else (echo Options : %*)
echo.
if "%~1"=="" if /i "%PROJ%\"=="%HERE%" echo [i] Tip: you can also pass a folder, e.g. run_jp_sub.bat "D:\Anime\2026.7"

rem ---- pick a Python: project .venv first, then ANIME_JP_SUB_PYTHON, then
rem      py -3 / python - but only after checking the candidate really runs
rem      (a broken pyenv shim on PATH answers nothing; and if it is a .bat it
rem       would also swallow the rest of this script - hence call everywhere).
set "PYCMD="
if exist "%PROJ%\.venv\Scripts\python.exe" set "PYCMD="%PROJ%\.venv\Scripts\python.exe""
if defined PYCMD goto :run
if defined ANIME_JP_SUB_PYTHON set "PYCMD="%ANIME_JP_SUB_PYTHON%""
if defined PYCMD goto :no_venv
call :try_python py -3
if defined PYCMD goto :no_venv
call :try_python python
if defined PYCMD goto :no_venv
echo [X] Python not found (tried: py -3, python).
echo     Double-click setup.bat in the project folder once - it installs everything.
echo     Or install Python 3.10/3.11 (tick "Add Python to PATH"), or point
echo     ANIME_JP_SUB_PYTHON at a working python.exe.
goto :done

:try_python
rem  run the candidate in a child cmd: a broken shim (pyenv ships .bat shims that
rem  end with a plain  exit ) would otherwise terminate this script as well
cmd /c %* -V >nul 2>nul
if not errorlevel 1 set PYCMD=%*
exit /b 0

:no_venv
echo [i] Project .venv not found - using: %PYCMD%
echo     (first time here? double-click setup.bat in the project folder once -
echo      it creates .venv and installs the dependencies)
echo.

:run
if "%~1"=="" (
    call %PYCMD% "%PROJ%\run_jp_sub.py" "%TARGET%"
) else (
    call %PYCMD% "%PROJ%\run_jp_sub.py" %*
)

:done
echo.
echo Done. Press any key to close.
pause >nul
