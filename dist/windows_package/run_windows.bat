@echo off
setlocal
cd /d "%~dp0"

if not exist "logs" mkdir logs
if not exist "parms.ini" (
    echo ERROR: parms.ini not found in %~dp0
    echo Please place your parms.ini file in this folder before starting.
    pause
    exit /b 1
)

echo [1/3] Loading Docker image multistream-rsi:9.8...
docker load -i "multistream-rsi-9.8-windows-amd64.tar"

echo [2/3] Removing any previous container instance...
docker rm -f multistream-rsi >nul 2>&1

echo [3/3] Starting multistream-rsi container on ports 5000 and 5001...
docker run -d ^
  --name multistream-rsi ^
  --restart unless-stopped ^
  -p 5000:5000 ^
  -p 5001:5001 ^
  -v "%cd%\parms.ini:/home/jboa/app/src/parms.ini:ro" ^
  -v "%cd%\logs:/home/jboa/app/logs" ^
  multistream-rsi:9.8

echo.
echo Container started! Dashboard available at http://localhost:5001
docker ps --filter "name=multistream-rsi"
pause
