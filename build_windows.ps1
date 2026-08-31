$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
python -m pip install -e '.[build]'
python -m PyInstaller --noconfirm --clean --windowed --name GameKO --paths src `
    --icon "src/gameko/assets/AINFORGE.ico" `
    --version-file "windows_version_info.txt" `
    --collect-data UnityPy `
    --add-data "src/gameko/assets/NotoSansCJKkr-Regular.otf;gameko/assets" `
    --add-data "src/gameko/assets/NotoSansKR-OFL.txt;gameko/assets" `
    --add-data "src/gameko/assets/gameko_notosanscjkkr_sdf_u6000_3_23f1;gameko/assets" `
    --add-data "src/gameko/assets/AINFORGE.png;gameko/assets" `
    launcher.py
Copy-Item -LiteralPath "$projectRoot\README.md" -Destination "$projectRoot\dist\GameKO\README.md" -Force
Copy-Item -LiteralPath "$projectRoot\THIRD_PARTY.md" -Destination "$projectRoot\dist\GameKO\THIRD_PARTY.md" -Force
Copy-Item -LiteralPath "$projectRoot\src\gameko\assets\NotoSansKR-OFL.txt" -Destination "$projectRoot\dist\GameKO\NotoSansKR-OFL.txt" -Force
Copy-Item -LiteralPath "$projectRoot\src\gameko\assets\AINFORGE.png" -Destination "$projectRoot\dist\GameKO\AINFORGE.png" -Force
$smoke = Start-Process -FilePath "$projectRoot\dist\GameKO\GameKO.exe" -ArgumentList '--smoke-test' -WorkingDirectory "$projectRoot\dist\GameKO" -WindowStyle Hidden -PassThru
if (-not $smoke.WaitForExit(15000)) {
    Stop-Process -Id $smoke.Id -Force
    throw 'GameKO smoke test timed out (possible startup error dialog)'
}
if ($smoke.ExitCode -ne 0) { throw "GameKO smoke test failed with exit code $($smoke.ExitCode)" }
$unitySmokeRoot = "$projectRoot\build\frozen-unity-smoke"
if (Test-Path -LiteralPath $unitySmokeRoot) {
    Remove-Item -LiteralPath $unitySmokeRoot -Recurse -Force
}
New-Item -ItemType Directory -Force "$unitySmokeRoot\Smoke_Data\StreamingAssets" | Out-Null
Copy-Item -LiteralPath "$projectRoot\tests\fixtures\unity_dialogue.bundle" -Destination "$unitySmokeRoot\Smoke_Data\StreamingAssets\dialogue.bundle" -Force
Copy-Item -LiteralPath "$projectRoot\src\gameko\assets\gameko_notosanscjkkr_sdf_u6000_3_23f1" -Destination "$unitySmokeRoot\Smoke_Data\StreamingAssets\font.bundle" -Force
$unitySmoke = Start-Process -FilePath "$projectRoot\dist\GameKO\GameKO.exe" -ArgumentList @('--unity-static-smoke-test', $unitySmokeRoot) -WorkingDirectory "$projectRoot\dist\GameKO" -WindowStyle Hidden -PassThru
if (-not $unitySmoke.WaitForExit(30000)) {
    Stop-Process -Id $unitySmoke.Id -Force
    throw 'GameKO Unity static smoke test timed out'
}
if ($unitySmoke.ExitCode -ne 0) { throw "GameKO Unity static smoke test failed with exit code $($unitySmoke.ExitCode)" }
Compress-Archive -Path "$projectRoot\dist\GameKO" -DestinationPath "$projectRoot\dist\GameKO-Windows.zip" -Force
Write-Host "완료: $projectRoot\dist\GameKO-Windows.zip"
