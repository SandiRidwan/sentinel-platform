<#
  SENTINEL — Windows task entry point.
  Mirrors the Makefile one-for-one by calling the same runner.
  Usage:  .\tasks.ps1 doctor
          .\tasks.ps1 quality
#>
param(
  [Parameter(Position = 0)]
  [string]$Command = "help",
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Rest
)

$ErrorActionPreference = "Stop"

# Put the tools winget installed on PATH for this process.
$machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
$user    = [System.Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = "$machine;$user"

# GnuWin32 make is not added to PATH by its installer; expose it anyway for parity.
$gnuMake = "C:\Program Files (x86)\GnuWin32\bin"
if ((Test-Path $gnuMake) -and ($env:Path -notlike "*$gnuMake*")) {
  $env:Path = "$env:Path;$gnuMake"
}

Set-Location -LiteralPath $PSScriptRoot

$py = "python"
if (Test-Path ".\.venv\Scripts\python.exe") {
  $py = ".\.venv\Scripts\python.exe"
}

& $py -m tools.tasks.runner $Command @Rest
exit $LASTEXITCODE
