@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Sandkassen er ikke satt opp enda. Dobbeltklikk oppsett.bat forst.
  pause
  exit /b 1
)

rem Sjekk om noe allerede lytter paa 127.0.0.1:5000 (typisk en gammel sandkasse som ikke ble lukket).
rem Vi starter ALDRI en server nummer to i det stille, og vi dreper aldri prosesser automatisk.
".venv\Scripts\python.exe" -m kurs.portsjekk 5000
if errorlevel 1 (
  echo.
  echo  Sandkassen ble IKKE startet.
  echo.
  pause
  exit /b 1
)

echo.
echo  IPR Paameldingssystem starter paa http://127.0.0.1:5000
echo  Admin: http://127.0.0.1:5000/admin   (brukernavn: admin, passord: demo)
echo.
echo  La dette vinduet vaere aapent mens du bruker sandkassen.
echo  Lukk vinduet (eller trykk Ctrl+C) for aa stoppe.
echo.
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:5000"
".venv\Scripts\python.exe" kjor.py
pause
