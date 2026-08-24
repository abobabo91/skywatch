@echo off
REM Skywatch supervisor - keeps exactly one daemon process alive.
REM
REM Task Scheduler runs this every 5 minutes via run-hidden.vbs. It is a no-op
REM when the daemon is already up, and restarts it after a reboot, a crash, or
REM the machine waking from sleep.
REM
REM The interpreter is SKYWATCH_PYTHON if you set it, otherwise whatever
REM `python` resolves to on PATH. Set it when you keep Skywatch's dependencies
REM in a specific interpreter or venv rather than the default one.
REM
REM ASCII only in this file. cmd.exe parses .bat using the system codepage and a
REM single non-ASCII byte (em-dash, ellipsis) corrupts the parser.

setlocal
set PROJECT_DIR=%~dp0..
set PYTHON=%SKYWATCH_PYTHON%
if "%PYTHON%"=="" set PYTHON=python
set PYTHONIOENCODING=utf-8

cd /d "%PROJECT_DIR%"
"%PYTHON%" -m skywatch.supervisor
exit /b %ERRORLEVEL%
