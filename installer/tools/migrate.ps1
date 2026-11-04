# Migrare de la versiunea cu scripturi (.bat + Python) la aplicatia instalata.
# Ruleaza ca utilizatorul logat (fara drepturi de Administrator).
$ErrorActionPreference = 'SilentlyContinue'
$data = Join-Path $env:ProgramData 'CTI Camera Follow'
New-Item -ItemType Directory -Force $data | Out-Null

# 1. config + jurnal: doar daca noua instalare nu are inca unul
$newCfg = Join-Path $data 'config.json'
if (-not (Test-Path $newCfg)) {
    $old = @('C:\CTI\CameraFollow', "$env:USERPROFILE\Downloads\cti-camera-follow", "$env:USERPROFILE\Desktop\cti-camera-follow") |
        ForEach-Object { Join-Path $_ 'config.json' } | Where-Object { Test-Path $_ } |
        Sort-Object { (Get-Item $_).LastWriteTime } -Descending | Select-Object -First 1
    if ($old) { Copy-Item $old $newCfg -Force }
}

# 2. pornirea automata veche (START.bat)
Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'CTI_CameraFollow.bat') -Force

# 3. scurtaturile vechi ale utilizatorului (cele noi sunt pentru toti utilizatorii)
$ws = New-Object -ComObject WScript.Shell
$desk = [Environment]::GetFolderPath('Desktop')
Get-ChildItem $desk -Filter 'CTI Camera Follow*' | ForEach-Object {
    if ($_.Extension -eq '.url') { Remove-Item $_.FullName -Force; return }
    if ($_.Extension -eq '.lnk') {
        $t = $ws.CreateShortcut($_.FullName).TargetPath
        if ($t -match '\.bat$' -or $t -match 'explorer\.exe$') { Remove-Item $_.FullName -Force }
    }
}
Remove-Item (Join-Path ([Environment]::GetFolderPath('Programs')) 'CTI Camera Follow') -Recurse -Force

# 4. instalarile vechi nu mai pornesc din greseala
foreach ($d in 'C:\CTI\CameraFollow', "$env:USERPROFILE\Downloads\cti-camera-follow") {
    foreach ($f in 'START.bat', 'SETUP.bat', 'REPARA.bat') {
        $p = Join-Path $d $f
        if (Test-Path $p) { Rename-Item $p "$f.vechi" -Force }
    }
}
exit 0
