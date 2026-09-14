# Windows x64 entry point. Native Windows acceptance is still required.
[CmdletBinding()]
param(
    [ValidateSet('build', 'verify', 'install', 'uninstall', 'package')]
    [string]$Action = 'install',
    [string]$MsysRoot = "$env:LOCALAPPDATA\mcpinkscape-msys64",
    [string]$WorkDir = "$PSScriptRoot\build\native",
    [string]$Prefix = "$env:LOCALAPPDATA\Programs\mcpinkscape",
    [ValidateRange(1, 128)] [int]$Jobs = 4
)
$ErrorActionPreference = 'Stop'
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
    throw 'The initial Windows build target is x64.'
}
$lock = Get-Content -Raw "$PSScriptRoot\packaging\sources.json" | ConvertFrom-Json
$bash = Join-Path $MsysRoot 'usr\bin\bash.exe'
if (-not (Test-Path $bash)) {
    if ($Action -in @('verify', 'uninstall')) {
        throw 'Verification/removal requires the existing build toolchain; pass its location with -MsysRoot.'
    }
    if (Test-Path $MsysRoot) { throw "Refusing to bootstrap into existing incomplete directory: $MsysRoot" }
    $installer = Join-Path ([IO.Path]::GetTempPath()) 'mcpinkscape-msys2-20260611.exe'
    Invoke-WebRequest -UseBasicParsing -Uri $lock.msys2.url -OutFile $installer
    if ((Get-FileHash -Algorithm SHA256 $installer).Hash.ToLowerInvariant() -ne $lock.msys2.sha256) {
        throw 'MSYS2 installer checksum mismatch.'
    }
    & $installer in --confirm-command --accept-messages --root $MsysRoot
    if ($LASTEXITCODE -ne 0) { throw "MSYS2 installation failed: $LASTEXITCODE" }
    if (-not (Test-Path $bash)) { throw 'MSYS2 did not install its shell.' }
}
# Values are passed through the environment, never interpolated into shell code.
$env:MSYSTEM = 'UCRT64'
$env:CHERE_INVOKING = '1'
$env:MCP_BUILD_ROOT = $PSScriptRoot
$env:MCP_BUILD_WORK = [IO.Path]::GetFullPath($WorkDir)
$env:MCP_BUILD_PREFIX = [IO.Path]::GetFullPath($Prefix)
$env:MCP_BUILD_ACTION = $Action
$env:MCP_BUILD_JOBS = "$Jobs"
& $bash -lc 'exec bash "$(cygpath -u "$MCP_BUILD_ROOT")/packaging/windows-build.sh"'
if ($LASTEXITCODE -ne 0) { throw "Native application build failed: $LASTEXITCODE" }
