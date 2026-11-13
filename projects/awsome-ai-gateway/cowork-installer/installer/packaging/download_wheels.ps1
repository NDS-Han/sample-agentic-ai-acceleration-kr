<#
.SYNOPSIS
    Pre-downloads all build dependencies as wheels for an air-gapped build.

.DESCRIPTION
    Run this on an INTERNET-CONNECTED Windows x64 machine with the same
    Python minor version as the build machine. It fills a wheel directory
    with the project's runtime dependencies plus pip/PyInstaller.

    Copy the resulting directory (plus this repository) to the offline build
    machine, then run:

        powershell -File packaging\build.ps1 -WheelDir C:\path\to\wheels

.PARAMETER OutDir
    Where to place the wheels. Default: .\wheels
#>
[CmdletBinding()]
param(
    [string]$OutDir = "wheels"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ProjectDir = Join-Path $PSScriptRoot "entrypoints\gateway-cli-v2"
Set-Location $RepoRoot

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# Wheels must match the build machine: Windows x64 + same Python minor
# version. Running this script with that interpreter guarantees it.
# The project itself lives in the nested gateway-cli-v2 directory.
# 재현 가능한 휠 캐시: 런타임 의존성은 requirements.lock.txt(poetry.lock 에서
# 생성, 버전+sha256 해시 핀)로, 프로젝트 자체는 --no-deps 로 따로 내린다.
# `$ProjectDir` 을 그대로 주면 pip 이 pyproject 의 >= 스펙으로 **최신판을 재해석**
# 해 lock 을 무시한다 — 해시 검증과 모순되므로 분리한다 (R33).
python -m pip download --dest $OutDir `
    --require-hashes -r "$ProjectDir\requirements.lock.txt"
if ($LASTEXITCODE -ne 0) { throw "pip download (locked deps) failed" }
python -m pip download --dest $OutDir --no-deps $ProjectDir
if ($LASTEXITCODE -ne 0) { throw "pip download (project) failed" }
python -m pip download --dest $OutDir "pip" "setuptools" "wheel"
if ($LASTEXITCODE -ne 0) { throw "pip download (bootstrap tools) failed" }

Write-Host "`nWheel cache ready: $OutDir"
Write-Host "Copy it to the build machine and pass -WheelDir to build.ps1."
