@echo off
title Capital Ledger - Free Local Demo
echo Starting Capital Ledger on this computer. No AWS account is used.
echo Open http://127.0.0.1:8787 after the server starts.
echo Keep this window open. Press Ctrl+C to stop.
echo.
python "%~dp0start-local.py" %*
if errorlevel 1 pause
