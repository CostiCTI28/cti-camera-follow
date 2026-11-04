# Instaleaza plugin-ul OBS "Audio Monitor" (exeldro) in %ProgramData%\obs-studio\plugins
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$url  = 'https://obsproject.com/forum/resources/audio-monitor.1186/version/7298/download?file=119396'
$dst  = Join-Path $env:ProgramData 'obs-studio\plugins'
$dest = Join-Path $dst 'audio-monitor'

if (Test-Path (Join-Path $dest 'bin\64bit\audio-monitor.dll')) {
    Write-Host '      Audio Monitor deja instalat.'
    exit 0
}

$tmp = Join-Path $env:TEMP ('am_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $tmp | Out-Null
try {
    Invoke-WebRequest -Uri $url -OutFile "$tmp\am.zip" -UserAgent 'Mozilla/5.0' -UseBasicParsing
    Expand-Archive "$tmp\am.zip" "$tmp\x" -Force

    $dll = Get-ChildItem "$tmp\x" -Recurse -Filter 'audio-monitor.dll' | Select-Object -First 1
    if (-not $dll) { throw 'audio-monitor.dll nu exista in arhiva' }

    New-Item -ItemType Directory -Force $dst | Out-Null
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }

    if ($dll.Directory.Name -eq '64bit' -and $dll.Directory.Parent.Name -eq 'bin') {
        # layout nou: audio-monitor\bin\64bit + audio-monitor\data
        Copy-Item $dll.Directory.Parent.Parent.FullName $dest -Recurse -Force
    } else {
        # layout vechi: obs-plugins\64bit + data\obs-plugins\audio-monitor
        New-Item -ItemType Directory -Force "$dest\bin\64bit" | Out-Null
        Copy-Item $dll.FullName "$dest\bin\64bit\" -Force
        $data = Get-ChildItem "$tmp\x" -Recurse -Directory -Filter 'audio-monitor' |
                Where-Object { Test-Path (Join-Path $_.FullName 'locale') } | Select-Object -First 1
        if ($data) { Copy-Item $data.FullName "$dest\data" -Recurse -Force }
    }
    Write-Host '      Audio Monitor instalat.'
}
catch {
    Write-Host "      EROARE Audio Monitor: $($_.Exception.Message)"
    Write-Host '      Instalare manuala: https://obsproject.com/forum/resources/audio-monitor.1186/'
    exit 1
}
finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
