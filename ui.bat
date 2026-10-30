@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo [ui] UncensoredAI: vybor modeli s Hugging Face + arenda GPU na Clore.ai
echo Otkroetsya http://127.0.0.1:8765 - zakroyte okno ili Ctrl+C chtoby ostanovit.
python app.py %*
pause
