@echo off
cd /d "%~dp0"
call npm run build || goto :error
call npm run server
:error
