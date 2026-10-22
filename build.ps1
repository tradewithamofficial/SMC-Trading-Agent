# Run .\build.ps1 from the source folder; paths resolve relative to this script.
param([switch]$SkipDependencyInstall)
$ErrorActionPreference = 'Stop'
$buildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    throw 'Create the project environment first: python -m venv .venv'
}
$releaseFolder = Join-Path $PSScriptRoot 'release\SMCTradingAgent'
$robotIcon = Join-Path $PSScriptRoot 'assets\TradeWithAM_Robot.ico'
if (-not (Test-Path -LiteralPath $robotIcon)) { throw 'Missing assets\TradeWithAM_Robot.ico' }
if (Test-Path -LiteralPath (Join-Path $releaseFolder 'telegram_config.local.json')) {
    throw 'Move your private Telegram configuration out of the release folder before rebuilding.'
}
$examplePath = Join-Path $PSScriptRoot 'telegram_config.example.json'
if (Test-Path -LiteralPath $releaseFolder) {
    throw 'The release output folder already exists. Preserve/move it before rebuilding; local data will not be overwritten.'
}
foreach ($promptName in @('smc_setups.txt','smc_entry_conditions.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "reference_simple\$promptName"))) {
        throw "Missing public prompt: $promptName"
    }
}
$exampleConfig = Get-Content -LiteralPath $examplePath -Raw | ConvertFrom-Json
if ($exampleConfig.bot_token -ne '' -or $exampleConfig.chat_id -ne '' -or
    @($exampleConfig.PSObject.Properties).Count -ne 2) {
    throw 'telegram_config.example.json must contain only blank bot_token and chat_id fields.'
}

Push-Location -LiteralPath $PSScriptRoot
try {
    if (-not $SkipDependencyInstall) {
        & $buildPython -m pip install -r (Join-Path $PSScriptRoot 'requirements-build.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
    }
    & $buildPython -m PyInstaller --noconfirm --clean --onedir --windowed --name SMCTradingAgent `
        --icon $robotIcon `
        --version-file (Join-Path $PSScriptRoot 'windows-version.txt') `
        --add-data "$(Join-Path $PSScriptRoot 'assets\Futuristic AI Robot with Breakout Chart.png');assets" `
        --add-data "$robotIcon;assets" `
        --distpath (Join-Path $PSScriptRoot 'release') `
        --workpath (Join-Path $PSScriptRoot 'build') `
        --specpath (Join-Path $PSScriptRoot 'build') `
        (Join-Path $PSScriptRoot 'main.py')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed. See the build output above.' }
    $promptFolder = Join-Path $releaseFolder 'reference_simple'
    New-Item -ItemType Directory -Path $promptFolder | Out-Null
    foreach ($promptName in @('smc_setups.txt','smc_entry_conditions.txt')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot "reference_simple\$promptName") -Destination $promptFolder
    }
    Copy-Item -LiteralPath $examplePath -Destination (Join-Path $releaseFolder 'telegram_config.example.json')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'RELEASE-SETUP.txt') -Destination (Join-Path $releaseFolder 'README-SETUP.txt')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LICENSE') -Destination $releaseFolder
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'THIRD-PARTY-NOTICES.md') -Destination $releaseFolder
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'third_party_licenses') -Destination $releaseFolder -Recurse -Force
    Write-Host "Created: $(Join-Path $releaseFolder 'SMCTradingAgent.exe')"
    Write-Host 'Distribute the complete SMCTradingAgent folder, including _internal.'
} finally {
    Pop-Location
}
