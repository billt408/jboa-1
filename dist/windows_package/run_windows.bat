@echo off
setlocal
cd /d "%~dp0"

set "CTR_BIN="
where podman >nul 2>&1 && set "CTR_BIN=podman"
if not defined CTR_BIN (
    where docker >nul 2>&1 && set "CTR_BIN=docker"
)
if not defined CTR_BIN (
    echo ERROR: Neither podman nor docker was found in PATH.
    pause
    exit /b 1
)

if not exist "logs" mkdir logs
if not exist "parms.ini" (
    echo ERROR: parms.ini not found in %~dp0
    echo Please place your parms.ini file in this folder before starting.
    pause
    exit /b 1
)

echo [1/3] Loading image multistream-rsi:9.8 using %CTR_BIN%...
%CTR_BIN% load -i "multistream-rsi-9.8-windows-amd64.tar"

echo [2/3] Removing any previous container instance...
%CTR_BIN% rm -f multistream-rsi >nul 2>&1

echo [3/3] Starting multistream-rsi container on ports 5000 and 5001...
%CTR_BIN% run -d ^
  --name multistream-rsi ^
  --restart unless-stopped ^
  -p 5000:5000 ^
  -p 5001:5001 ^
  -e INI_PATH=/home/jboa/app/src/parms.ini ^
  -e LOG_ROOT=/home/jboa/app/logs ^
  -v "%cd%\parms.ini:/home/jboa/app/src/parms.ini:ro" ^
  -v "%cd%\logs:/home/jboa/app/logs" ^
  multistream-rsi:9.8 ^
  --ini /home/jboa/app/src/parms.ini

echo.
echo Container started! Dashboard available at https://localhost:5001 (or http://localhost:5001)
%CTR_BIN% ps --filter "name=multistream-rsi"
pause
