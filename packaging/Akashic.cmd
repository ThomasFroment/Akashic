@echo off
rem Starts Akashic from this folder. No console window: pythonw.
start "" "%~dp0python\pythonw.exe" -m akashic.app %*
