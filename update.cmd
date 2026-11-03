@echo off
rem Double-click to update Tatanka Charts: the newest version, the server rebuilt, the NinjaTrader add-on copied in.
rem One line on purpose: the update may change this file while it runs, and cmd reads a line at a time.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\update.ps1" & pause & exit /b
