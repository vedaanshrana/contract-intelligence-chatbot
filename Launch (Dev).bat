@echo off
REM =============================================================================
REM  MASTER_CHATBOT - Dev launcher (Windows)
REM  Starts the FastAPI backend on :8000 (auto-reload) AND the Vite dev server
REM  on :5173 in a second console window. /api/* is proxied from :5173 -> :8000
REM  by vite.config.ts, so open http://127.0.0.1:5173 to use the UI.
REM =============================================================================
setlocal
cd /d "%~dp0"

REM ── Backend (this window) ────────────────────────────────────────────────────
start "MASTER_CHATBOT - Backend (uvicorn :8000)" cmd /k "cd /d %~dp0backend && python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000"

REM ── Frontend (Vite dev server in a second window) ────────────────────────────
if not exist "frontend\node_modules" (
  echo [setup] Installing frontend dependencies...
  pushd "frontend"
  call npm install
  popd
)
start "MASTER_CHATBOT - Frontend (Vite :5173)" cmd /k "cd /d %~dp0frontend && npm run dev"

echo.
echo Two windows launched.
echo   Backend : http://127.0.0.1:8000/api/health
echo   Frontend: http://127.0.0.1:5173  (use this URL in your browser)
echo Close either window (or Ctrl-C inside it) to stop that side.
