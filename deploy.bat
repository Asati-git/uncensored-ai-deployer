@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo [deploy] Arenda GPU na Clore.ai i razvertyvanie modeli iz .env (MODEL_REPO)...
echo Flagi: --model owner/name  --ctx 65536  --max-price 0.5 (USD/chas)  --order-type spot  --dry-run  --force
python deploy.py %*
echo.
echo Exit code: %ERRORLEVEL%
pause
