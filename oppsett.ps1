# Oppsett av IPR Kurs sandkasse. Kjøres én gang per PC (dobbeltklikk oppsett.bat).
# Lager et eget Python-miljø i mappen .venv, installerer pakker, lager demodata og kjører testene.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Skriv($tekst, $farge = "White") { Write-Host $tekst -ForegroundColor $farge }

Skriv ""
Skriv "== IPR Kurs - oppsett av sandkasse ==" Cyan
Skriv "Mappe: $PSScriptRoot"
Skriv ""

$iSynkMappe = $PSScriptRoot -match '\\Dropbox\\'
foreach ($synk in @($env:OneDrive, $env:OneDriveCommercial, $env:OneDriveConsumer)) {
    if ($synk -and $PSScriptRoot.StartsWith($synk, [StringComparison]::OrdinalIgnoreCase)) { $iSynkMappe = $true }
}
if ($iSynkMappe) {
    Skriv "ADVARSEL: Mappen ligger i OneDrive eller Dropbox." Yellow
    Skriv "Synkronisering kan gi rare feil. Anbefalt: flytt mappen til f.eks. C:\IPR\ipr-kurs" Yellow
    $svar = Read-Host "Vil du fortsette likevel? (j/n)"
    if ($svar -ne "j") { exit 1 }
}

# 1. Finn Python 3.10 eller nyere
Skriv "1/4  Ser etter Python ..." Cyan
$kandidater = @(
    @("py", "-3.13"), @("py", "-3.12"), @("py", "-3.14"), @("py", "-3.11"), @("py", "-3"), @("python")
)
$pyExe = $null; $pyArgs = @()
foreach ($k in $kandidater) {
    $exe = $k[0]; $argumenter = @($k | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $versjon = & $exe @argumenter -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $versjon -and ([version]$versjon -ge [version]"3.10")) {
        $pyExe = $exe; $pyArgs = $argumenter
        Skriv "     Fant Python $versjon" Green
        break
    }
}
if (-not $pyExe) {
    Skriv ""
    Skriv "Fant ikke Python 3.10 eller nyere." Red
    Skriv "Installer Python 3.12 fra https://www.python.org/downloads/" Yellow
    Skriv "VIKTIG: huk av 'Add python.exe to PATH' i installasjonen." Yellow
    Skriv "Kjør deretter oppsett.bat på nytt. (Mangler du rettigheter, spør IT.)" Yellow
    exit 1
}

# 2. Eget Python-miljø i .venv
Skriv "2/4  Lager Python-miljø (.venv) ..." Cyan
$vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Skriv "Klarte ikke å lage .venv" Red; exit 1 }
}

# 3. Pakker
Skriv "3/4  Installerer pakker (kan ta et par minutter) ..." Cyan
& $vpy -m pip install --quiet --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Skriv "Installasjon av pakker feilet. Sjekk internettforbindelsen og prøv igjen." Red
    exit 1
}

# 4. Demodata og test
Skriv "4/4  Lager demodata og kjører testene ..." Cyan
& $vpy -m kurs.seed_demo | Out-Null
if ($LASTEXITCODE -ne 0) { Skriv "Klarte ikke å lage demodata." Red; exit 1 }
& $vpy -m pytest -q -p no:cacheprovider tests
if ($LASTEXITCODE -ne 0) { Skriv "Noen tester feilet - se over. Sandkassen kan likevel startes." Yellow }

Skriv ""
Skriv "Ferdig! Sandkassen er klar." Green
Skriv "Start den med å dobbeltklikke start.bat" Green
Skriv ""
