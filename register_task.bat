@echo off
REM Registers the SignalBot scheduled task (runs autostart_bot.bat at logon)
REM and launches it immediately.
schtasks /create /tn SignalBot /tr "cmd /c C:\Users\DAMITH~1\SIGNAL~1\autostart_bot.bat" /sc onlogon /f
if %errorlevel%==0 (
  echo TASK_CREATED
  schtasks /run /tn SignalBot
) else (
  echo TASK_FAILED %errorlevel%
)