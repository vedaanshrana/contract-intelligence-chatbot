@echo off
REM =============================================================================
REM  MASTER_CHATBOT - Legacy Streamlit UI launcher
REM  Use this only if you want the old Streamlit interface instead of the new
REM  React UI. The Streamlit app talks to the same agents directly (no FastAPI).
REM =============================================================================
setlocal
cd /d "%~dp0backend"
python -m streamlit run chatbot.py
