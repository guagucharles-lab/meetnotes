#Requires -Version 5.1
# Meetnotes: asennus Windowsiin.
# Kaynnista tuplaklikkaamalla "Asenna Meetnotes.cmd" (ei vaadi jarjestelmanvalvojaa).
#
# 1. Etsii Python 3.11+ tai asentaa Python 3.12:n wingetilla (vain sinulle)
# 2. Varmistaa Edge WebView2:n (sovellusikkuna; valmiina Windows 11:ssa)
# 3. Asentaa Meetnotesin omaan ymparistoon kansioon %LOCALAPPDATA%\meetnotes
# 4. Luo Meetnotes-kuvakkeen tyopoydalle ja Kaynnista-valikkoon
# 5. Kytkee kaynnistyksen kirjautuessa ja avaa sovelluksen

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Source = (Resolve-Path (Join-Path $Here '..')).Path
$AppDir = Join-Path $env:LOCALAPPDATA 'meetnotes'
$Venv   = Join-Path $AppDir 'venv'
$Py     = Join-Path $Venv 'Scripts\python.exe'
# Launch through Python's own signed pythonw.exe, not pip's unsigned launcher
# exes, so Smart App Control has nothing unsigned to block.
$PyW    = Join-Path $Venv 'Scripts\pythonw.exe'
$Icon   = Join-Path $AppDir 'meetnotes.ico'

function Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Fail([string]$Text) {
    Write-Host ''
    Write-Host "Asennus keskeytyi: $Text" -ForegroundColor Red
    exit 1
}

function Find-Python {
    # Returns @(exe, args...) for a Python 3.11+ interpreter, or $null.
    $candidates = New-Object System.Collections.ArrayList
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in '3.13', '3.12', '3.11') { [void]$candidates.Add(@('py', "-$v")) }
    }
    foreach ($v in 'Python313', 'Python312', 'Python311') {
        $exe = Join-Path $env:LOCALAPPDATA "Programs\Python\$v\python.exe"
        if (Test-Path $exe) { [void]$candidates.Add(@($exe)) }
    }
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath -and $onPath.Source -notlike '*WindowsApps*') { [void]$candidates.Add(@($onPath.Source)) }

    foreach ($c in $candidates) {
        $exe = $c[0]
        $rest = @($c | Select-Object -Skip 1)
        try {
            $ver = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -and ([version]$ver -ge [version]'3.11')) { return ,$c }
        } catch { }
    }
    return $null
}

Write-Host 'Meetnotes - asennus' -ForegroundColor White
Write-Host "Lahde: $Source"
if (-not (Test-Path (Join-Path $Source 'pyproject.toml'))) {
    Fail 'pyproject.toml puuttuu. Pura koko meetnotes-kansio ja aja skripti sen windows-kansiosta.'
}
# Files from a downloaded zip carry the internet mark; clear it for this folder.
Get-ChildItem -Path $Source -Recurse -File | Unblock-File -ErrorAction SilentlyContinue

# Upgrade in place: stop the running service first so files aren't locked.
if (Test-Path $Py) {
    Step 'Pysaytetaan aiempi versio'
    & $Py -m meetnotes.cli stop | Out-Null
}

Step 'Python'
$python = Find-Python
if (-not $python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail 'Python 3.11 tai uudempi puuttuu, eika wingetia loydy. Asenna Python osoitteesta https://www.python.org/downloads/ ja aja tama uudelleen.'
    }
    Write-Host 'Asennetaan Python 3.12 (vain sinulle, ei jarjestelmanvalvojaa)...'
    winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $python = Find-Python
    if (-not $python) { Fail 'Python asentui, mutta sita ei loydy. Kaynnista kone uudelleen ja aja asennus uudestaan.' }
}
$pyExe = $python[0]
$pyArgs = @($python | Select-Object -Skip 1)
Write-Host ("Kaytetaan: " + ($python -join ' '))

Step 'Sovellusikkunan moottori (Edge WebView2)'
$webview2 = @(
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    'HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
) | Where-Object { Test-Path $_ }
if ($webview2) {
    Write-Host 'Valmiina.'
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id Microsoft.EdgeWebView2Runtime -e --silent --accept-package-agreements --accept-source-agreements | Out-Host
} else {
    Write-Host 'WebView2 puuttuu. Sovellus aukeaa silloin selaimeen.' -ForegroundColor Yellow
}

Step 'Asennetaan Meetnotes (ensimmaisella kerralla muutama minuutti)'
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
if (-not (Test-Path $Py)) {
    & $pyExe @pyArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail 'Python-ympariston luonti epaonnistui.' }
}
& $Py -m pip install --upgrade --quiet pip
& $Py -m pip install --upgrade --quiet "${Source}[app]"
if ($LASTEXITCODE -ne 0) { Fail 'Pakettien asennus epaonnistui. Tarkista nettiyhteys ja aja uudelleen.' }
Copy-Item -Force (Join-Path $Source 'meetnotes\web\meetnotes.ico') $Icon

Step 'Asetukset'
& $Py -m meetnotes.winsetup config

Step 'Puheentunnistusmalli'
& $Py -m meetnotes.winsetup model
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Mallin lataus ei onnistunut nyt. Se ladataan ensimmaisen kokouksen alussa.' -ForegroundColor Yellow
}

Step 'Kuvakkeet tyopoydalle ja Kaynnista-valikkoon'
$shell = New-Object -ComObject WScript.Shell
$places = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
foreach ($dir in $places) {
    $lnk = $shell.CreateShortcut((Join-Path $dir 'Meetnotes.lnk'))
    $lnk.TargetPath = $PyW
    $lnk.Arguments = '-m meetnotes.desktop'
    $lnk.WorkingDirectory = $AppDir
    $lnk.IconLocation = "$Icon,0"
    $lnk.Description = 'Meetnotes - yksityiset kokousmuistiinpanot'
    $lnk.Save()
}

Step 'Kaynnistys kirjautuessa'
& $Py -m meetnotes.cli autostart on

Step 'Avataan Meetnotes'
Start-Process -FilePath $PyW -ArgumentList '-m', 'meetnotes.desktop' -WorkingDirectory $AppDir

Write-Host ''
Write-Host 'Valmis. Meetnotes-kuvake on tyopoydalla.' -ForegroundColor Green
Write-Host 'Lisaa sovelluksessa Asetukset-sivulla Claude API -avain.'
Write-Host "Komentorivi: $Py -m meetnotes.cli <komento>"
