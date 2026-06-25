@echo off
REM =============================================================================
REM  MASTER_CHATBOT - Production launcher (Windows)
REM  Builds the React UI once (if needed), then starts the FastAPI server which
REM  serves BOTH the API and the built React app at http://127.0.0.1:8000
REM =============================================================================
setlocal
cd /d "%~dp0"

if not exist "frontend\dist\index.html" (
  echo [build] React app not built yet - building it now...
  pushd "frontend"
  call npm install
  if errorlevel 1 (
    echo [build] npm install failed.
    popd
    exit /b 1
  )
  call npm run build
  if errorlevel 1 (
    echo [build] npm run build failed.
    popd
    exit /b 1
  )
  popd
)

echo.
echo ============================================================
echo   MASTER_CHATBOT  -  http://127.0.0.1:8000
echo   FastAPI + React (built) served from this one process.
echo   Ctrl-C to stop.
echo ============================================================
echo.
cd backend
python server.py
