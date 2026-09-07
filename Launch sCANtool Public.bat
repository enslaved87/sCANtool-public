@echo off
cd /d "%~dp0"
python scantool_public.py
if errorlevel 1 pause
