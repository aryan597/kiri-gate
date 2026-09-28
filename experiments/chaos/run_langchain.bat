@echo off
cd /d "%~dp0"
echo Installing LangChain (once)...
python -m pip install -q langchain langchain-openai langchain-anthropic
echo.
echo === 1. No-LLM proof (middleware alone) ===
python test_langchain_repro.py
python langchain_repro.py --scripted
echo.
echo === 2. Local Qwen via LM Studio (keep the model loaded) ===
python chaos.py --model qwen/qwen3.5-9b --runs 5 --gate
python langchain_repro.py --model qwen/qwen3.5-9b --runs 5 --conditions model,default,excluded,keyed
echo.
set /p ANTHROPIC_API_KEY=Paste ANTHROPIC_API_KEY for Claude Haiku (Enter to skip):
if "%ANTHROPIC_API_KEY%"=="" goto end
python langchain_repro.py --model claude-haiku-4-5-20251001 --runs 3 --conditions model,default,excluded,keyed
:end
echo.
echo Done. Results: results\*\COMBINED.md
pause
