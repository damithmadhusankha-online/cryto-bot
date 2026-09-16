@echo off
REM Launch the Signal Bot product runner (serve.py) for the scheduled task.
REM Credentials are loaded automatically from .env by config.py.
cd /d "%~dp0"
python serve.py >> "serve.log" 2>&1