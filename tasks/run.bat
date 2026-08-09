@echo off
REM Skywatch supervisor - keeps exactly one daemon process alive.
REM
REM Task Scheduler runs this every 5 minutes via run-hidden.vbs. It is a no-op
REM when the daemon is already up, and restarts it after a reboot, a crash, or
REM the machine waking from sleep.
REM
REM ASCII only in this file. cmd.exe parses .bat using the system codepage and a
REM single non-ASCII byte (em-dash, ellipsis) corrupts the parser.

setlocal
set PROJECT_DIR=%~dp0..
set PYTHON=C:\WPy64-31180\python-3.11.8.amd64\python.exe
set PYTHONIOENCODING=utf-8

cd /d "%PROJECT_DIR%"
"%PYTHON%" -m skywatch.supervisor
exit /b %ERRORLEVEL%
