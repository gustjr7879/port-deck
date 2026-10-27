@echo off
cd /d "%~dp0"
python -c "import psutil" 2>nul || python -m pip install -r requirements.txt
start "" http://127.0.0.1:7070
python server.py
