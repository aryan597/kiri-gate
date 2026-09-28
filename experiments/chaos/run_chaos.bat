@echo off
cd /d "%~dp0"
echo.
echo  Side-effect recovery: does the agent send twice when a write's outcome is unknown?
echo  1) Local Qwen (LM Studio on). 2) Claude Haiku (API key, well under $0.50).
echo.
python chaos.py --model qwen/qwen3.5-9b --runs 5 --gate
if "%ANTHROPIC_API_KEY%"=="" set /p ANTHROPIC_API_KEY=Paste Anthropic API key (or press Enter to skip Claude): 
if not "%ANTHROPIC_API_KEY%"=="" python chaos.py --model claude-haiku-4-5-20251001 --runs 5 --gate
pause
