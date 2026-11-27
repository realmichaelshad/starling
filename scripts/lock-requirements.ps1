<#
.SYNOPSIS
Regenerate backend/requirements.lock: exact versions with SHA-256 hashes for linux/amd64, CPython 3.11.

.DESCRIPTION
pip-compile runs inside the same base image as deploy/Dockerfile (Docker Desktop required), so the
resolution matches the production image whatever machine runs this script. Review the diff of
backend/requirements.lock and run the test image before committing it.

.PARAMETER Upgrade
Move every package to the newest version backend/requirements.in allows (default: keep existing pins).

.PARAMETER PythonImage
Base image; use the same tag or digest as deploy/Dockerfile. Default: $env:PYTHON_IMAGE, else python:3.11-slim.

.PARAMETER PipToolsSpec
pip-tools requirement installed in the container. Default: pip-tools>=7.4.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\lock-requirements.ps1
#>
[CmdletBinding()]
param(
    [switch]$Upgrade,
    [string]$PythonImage = $(if ($env:PYTHON_IMAGE) { $env:PYTHON_IMAGE } else { 'python:3.11-slim' }),
    [string]$PipToolsSpec = 'pip-tools>=7.4'
)
$ErrorActionPreference = 'Stop'

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'docker is required (Docker Desktop)'
}

$backend = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'
$upgradeFlag = if ($Upgrade) { '--upgrade' } else { '' }
# Expanded by the container shell. No double quotes inside: Windows PowerShell 5.1 mangles them
# when passing arguments to native programs.
$inner = 'python -m venv /tmp/tools && /tmp/tools/bin/pip install --quiet $PIP_TOOLS_SPEC && /tmp/tools/bin/pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras $UPGRADE --output-file requirements.lock requirements.in'

docker run --rm --platform linux/amd64 `
    -e HOME=/tmp -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_NO_CACHE_DIR=1 `
    -e "PIP_TOOLS_SPEC=$PipToolsSpec" -e "UPGRADE=$upgradeFlag" `
    -v "${backend}:/work" -w /work `
    $PythonImage sh -c $inner
if ($LASTEXITCODE -ne 0) {
    throw "pip-compile failed (exit code $LASTEXITCODE)"
}
Write-Host "wrote $(Join-Path $backend 'requirements.lock')"
