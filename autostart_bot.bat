@echo off
REM Windowless autostart for the Signal Bot (runs at every logon).
REM Logs everything to serve.log next to this file.
cd /d "%~dp0"
"C:\Users\DAMITH MADHUSHANKHA\AppData\Local\Programs\Python\Python314\pythonw.exe" serve.py >> serve.log 2>&1