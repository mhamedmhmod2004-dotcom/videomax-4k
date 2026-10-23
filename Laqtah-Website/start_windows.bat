@echo off
cd /d "%~dp0"
echo Starting LAQTAH...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Could not install dependencies. Make sure Python and Internet work.
    pause
    exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 echo WARNING: Install FFmpeg and add it to PATH for HD video/audio merging.
where deno >nul 2>nul
if errorlevel 1 echo WARNING: Install Deno 2.3+ for full YouTube support.
echo Open http://127.0.0.1:8000
start "" "http://127.0.0.1:8000"
python -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
