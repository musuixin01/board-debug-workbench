@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  set "PROJECT_PYTHON=.venv\Scripts\python.exe"
) else if exist "..\..\.venv\Scripts\python.exe" (
  set "PROJECT_PYTHON=..\..\.venv\Scripts\python.exe"
) else (
  echo Python environment not found. Please follow README.md.
  pause
  exit /b 1
)
echo Open http://127.0.0.1:18763/ in your browser.
echo Keep this window open while using the workbench.
"%PROJECT_PYTHON%" -m uvicorn app:app --host 127.0.0.1 --port 18763
pause
