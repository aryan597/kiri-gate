@echo off
setlocal
cd /d "%~dp0"
echo.
echo  Publishes kiri-gate to GitHub and creates the v0.1.0 release.
echo.
where git >nul 2>nul || (echo Install Git: https://git-scm.com/download/win & pause & exit /b 1)
where gh  >nul 2>nul || (echo Install GitHub CLI:  winget install --id GitHub.cli   then run this again & pause & exit /b 1)
gh auth status >nul 2>nul || gh auth login

echo Running tests...
python -m unittest discover -s tests -t . || (echo Tests failed. Not publishing. & pause & exit /b 1)

for /f %%U in ('gh api user -q .login') do set GHUSER=%%U
echo GitHub user: %GHUSER%
powershell -NoProfile -Command "foreach($f in 'README.md','CHANGELOG.md'){ (Get-Content $f -Raw).Replace('<your-username>','%GHUSER%') | Set-Content $f -NoNewline -Encoding utf8 }"

choice /c PR /m "Make the repo [P]ublic or [R]ivate"
if errorlevel 2 (set VIS=--private) else (set VIS=--public)

if not exist .git git init -b main
git add -A
git commit -m "kiri-gate v0.1.0" || echo Nothing new to commit.

gh repo view kiri-gate >nul 2>nul
if errorlevel 1 (
  gh repo create kiri-gate %VIS% --source . --remote origin --push --description "A gate in front of your AI agent's tools: acts on what can be undone, asks about what can't."
  gh repo edit --add-topic ai-agents --add-topic ai-safety --add-topic llm --add-topic mcp --add-topic human-in-the-loop
) else (
  git push -u origin main
)

gh release view v0.1.0 >nul 2>nul
if errorlevel 1 (
  gh release create v0.1.0 --title "v0.1.0: the core gate" --notes-file CHANGELOG.md
) else (
  echo Release v0.1.0 already exists.
)
echo.
gh repo view --web
pause
