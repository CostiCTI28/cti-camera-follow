# Instaleaza OBS Studio si VB-Cable fara winget (descarcare directa). Foloseste winget doar daca exista.
param([Parameter(Mandatory = $true)][ValidateSet('obs', 'vbcable', 'python', 'ffmpeg')][string]$What)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$hasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)
$tmp = Join-Path $env:TEMP ('cti_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $tmp | Out-Null

try {
    if ($What -eq 'ffmpeg') {
        $dstDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'go2rtc'
        $dst = Join-Path $dstDir 'ffmpeg.exe'
        if (Test-Path $dst) { Write-Host '      ffmpeg deja prezent.'; exit 0 }
        Write-Host '      Descarc ffmpeg (~200 MB, dureaza putin)...'
        Invoke-WebRequest -Uri 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile "$tmp\ff.zip" -UseBasicParsing
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead("$tmp\ff.zip")
        try {
            $e = $zip.Entries | Where-Object { $_.FullName -like '*/bin/ffmpeg.exe' } | Select-Object -First 1
            if (-not $e) { throw 'ffmpeg.exe lipseste din arhiva' }
            New-Item -ItemType Directory -Force $dstDir | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dst, $true)
        } finally { $zip.Dispose() }
        Write-Host '      ffmpeg instalat.'
    }

    if ($What -eq 'python') {
        Write-Host '      Descarc Python 3.12...'
        Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile "$tmp\py.exe" -UseBasicParsing
        Start-Process "$tmp\py.exe" -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1' -Wait
        Write-Host '      Python instalat.'
    }

    if ($What -eq 'obs') {
        $exe = Join-Path $env:ProgramFiles 'obs-studio\bin\64bit\obs64.exe'
        if (Test-Path $exe) { Write-Host '      OBS deja instalat.'; exit 0 }
        if ($hasWinget) {
            winget install -e --id OBSProject.OBSStudio --accept-package-agreements --accept-source-agreements
        } else {
            Write-Host '      Caut ultima versiune OBS...'
            $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/obsproject/obs-studio/releases/latest' -Headers @{ 'User-Agent' = 'cti-setup' }
            $asset = $rel.assets | Where-Object { $_.name -match 'Windows.*Installer\.exe$' -and $_.name -notmatch 'arm64' } | Select-Object -First 1
            if (-not $asset) { throw 'nu gasesc installer-ul Windows in ultima versiune OBS' }
            Write-Host "      Descarc $($asset.name)..."
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile "$tmp\obs.exe" -UseBasicParsing
            Write-Host '      Instalez OBS (silentios)...'
            Start-Process "$tmp\obs.exe" -ArgumentList '/S' -Wait
        }
        if (-not (Test-Path $exe)) { throw 'OBS nu apare instalat dupa setup' }
        Write-Host '      OBS instalat.'
    }

    if ($What -eq 'vbcable') {
        $present = Get-CimInstance Win32_SoundDevice | Where-Object { $_.Name -match 'VB-Audio' }
        if ($present) { Write-Host '      VB-Cable deja instalat.'; exit 0 }
        Write-Host '      Descarc VB-Cable...'
        Invoke-WebRequest -Uri 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip' -OutFile "$tmp\vb.zip" -UseBasicParsing
        Expand-Archive "$tmp\vb.zip" "$tmp\vb" -Force
        $setup = Get-ChildItem "$tmp\vb" -Recurse -Filter 'VBCABLE_Setup_x64.exe' | Select-Object -First 1
        if (-not $setup) { throw 'VBCABLE_Setup_x64.exe lipseste din arhiva' }
        Write-Host '      Instalez VB-Cable. Confirma fereastra Windows pentru driver!'
        Start-Process $setup.FullName -ArgumentList '-i', '-h' -WorkingDirectory $setup.DirectoryName -Wait
        Write-Host '      VB-Cable instalat. REPORNESTE laptopul la final.'
    }
}
catch {
    Write-Host "      EROARE ($What): $($_.Exception.Message)"
    if ($What -eq 'ffmpeg') { Write-Host '      Preview-ul merge doar pe camerele cu RTSP standard. Manual: pui ffmpeg.exe in folderul go2rtc.' }
    elseif ($What -eq 'obs') { Write-Host '      Instalare manuala: https://obsproject.com/download' }
    else { Write-Host '      Instalare manuala: https://vb-audio.com/Cable/' }
    exit 1
}
finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
