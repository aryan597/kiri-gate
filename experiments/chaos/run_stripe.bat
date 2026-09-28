@echo off
cd /d "%~dp0"
python -m pip install -q stripe langgraph langchain-openai
python test_stripe_repro.py
echo.
if "%STRIPE_API_KEY%"=="" set /p STRIPE_API_KEY=Paste your Stripe TEST secret key (sk_test_...):
echo.
echo === 1. Real Stripe test mode, scripted agent (no LLM) ===
python stripe_repro.py --scripted
echo.
echo === 2. Real Stripe test mode, local Qwen (keep LM Studio loaded) ===
python stripe_repro.py --model qwen/qwen3.5-9b --runs 5
echo.
echo Results: results\stripe\*\REPORT.md   Check them in the Stripe dashboard: Payments, test mode, description "kiri chaos test".
pause
