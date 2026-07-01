# 构建 Windows 可执行（one-folder）。在项目根目录运行：
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
Set-Location $root

function Remove-BuildArtifact {
    param([Parameter(Mandatory=$true)][string]$RelativePath)
    $target = Join-Path $root $RelativePath
    if (-not (Test-Path $target)) { return }
    $resolved = (Resolve-Path $target).Path
    $rootPrefix = $root.TrimEnd('\') + '\'
    if (-not ($resolved.StartsWith($rootPrefix))) {
        throw "Refusing to delete outside project root: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

Write-Host "[1/3] Install/check dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pyinstaller edge-tts | Out-Host
# 可选：打包 Piper 本机神经语音（显著增大 exe）。设 BUNDLE_PIPER=1 启用。
if ($env:BUNDLE_PIPER -in @("1","true","yes","on","TRUE","YES","ON")) {
    Write-Host "[1/3] BUNDLE_PIPER set -> installing piper-tts for bundling..." -ForegroundColor Cyan
    python -m pip install --upgrade piper-tts | Out-Host
}

Write-Host "[2/3] Clean previous build artifacts..." -ForegroundColor Cyan
$functionalPrompts = Join-Path $root "debater_prompts\functional_prompts"
if (-not (Test-Path $functionalPrompts)) {
    throw "Missing functional prompt directory: $functionalPrompts"
}
if ((Get-ChildItem -Path $functionalPrompts -Filter "*.md" -File).Count -lt 7) {
    throw "Not enough functional prompt files; expected at least 7 markdown files."
}
$methodology = Join-Path $root "methodology"
if (-not (Test-Path -LiteralPath $methodology -PathType Container)) {
    throw "Missing required methodology directory: $methodology"
}
$methodologyFiles = @(Get-ChildItem -LiteralPath $methodology -Filter "*.md" -File)
if ($methodologyFiles.Count -lt 2 -or @($methodologyFiles | Where-Object Length -eq 0).Count -gt 0) {
    throw "Methodology directory must contain at least two non-empty markdown files."
}
Remove-BuildArtifact "build\AI-Debate-Live"
Remove-BuildArtifact "dist\AI-Debate-Live"

Write-Host "[3/3] Build with PyInstaller..." -ForegroundColor Cyan
python -m PyInstaller packaging\ai_debate_live.spec --noconfirm --distpath dist --workpath build | Out-Host

Write-Host ""
Write-Host "Done -> dist\AI-Debate-Live\AI-Debate-Live.exe" -ForegroundColor Green
Write-Host "Double-click to run. Prerequisite: opencode is installed and logged in." -ForegroundColor Yellow
