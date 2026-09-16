@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
echo.
echo  Dette sletter alle demodata (kurs, deltakere, e-poster i utboksen)
echo  og lager nye. Stopp IPR Paameldingssystem forst hvis den kjorer (lukk start-vinduet).
echo.
pause
".venv\Scripts\python.exe" -m kurs.seed_demo
echo.
echo  Ferdig. Start sandkassen igjen med start.bat
pause
