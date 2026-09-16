@echo off
REM Launch the Signal Bot in product mode (signal loop + Telegram commands
REM + daily digest). Credentials are loaded automatically from .env by
REM config.py - no environment variables needed.
cd /d "%~dp0"
python serve.py
pause