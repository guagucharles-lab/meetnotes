#Requires -Version 5.1
# Meetnotes: poisto. Muistiinpanot (oletuksena %USERPROFILE%\MeetingNotes) jaavat talteen.
$ErrorActionPreference = 'Continue'

$AppDir = Join-Path $env:LOCALAPPDATA 'meetnotes'
$Venv   = Join-Path $AppDir 'venv'
$Py     = Join-Path $Venv 'Scripts\python.exe'

Write-Host 'Meetnotes - poisto' -ForegroundColor White

if (Test-Path $Py) {
    Write-Host 'Pysaytetaan taustapalvelu (keskeneraisen kokouksen muistiinpanot kirjoitetaan ensin)...'
    & $Py -m meetnotes.cli stop
    & $Py -m meetnotes.cli autostart off
}

if (Test-Path $Py) {
    & $Py -c "import keyring; keyring.delete_password('meetnotes', 'anthropic_api_key')" 2>$null
}

foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $lnk = Join-Path $dir 'Meetnotes.lnk'
    if (Test-Path $lnk) { Remove-Item -Force $lnk }
}

# Only ever remove %LOCALAPPDATA%\meetnotes (program, settings, log), never the notes.
if ($env:LOCALAPPDATA -and (Split-Path $AppDir -Leaf) -eq 'meetnotes' -and (Test-Path $AppDir)) {
    Remove-Item -Recurse -Force $AppDir
}
$cfg = Join-Path $env:USERPROFILE '.config\meetnotes'
if ($env:USERPROFILE -and (Test-Path $cfg)) { Remove-Item -Recurse -Force $cfg }

Write-Host ''
Write-Host 'Meetnotes poistettu.' -ForegroundColor Green
Write-Host ('Muistiinpanosi ovat yha tallessa: ' + (Join-Path $env:USERPROFILE 'MeetingNotes'))
