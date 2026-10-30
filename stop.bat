@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo Zakaz Clore budet otmenen: server osvoboditsya, skachannaya model udalitsya. Ctrl+C dlya otmeny.
pause
python stop.py %*
pause
