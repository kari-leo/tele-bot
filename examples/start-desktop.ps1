param(
  [string]$RuntimeRoot = 'D:\files_data\windborne\dsh-runtime',
  [string]$DesktopExe = 'D:\daily_app\Deepseek Harness\DeepSeek Harness.exe',
  [string]$PowerShellDirectory = 'D:\files_data\windborne\dsh-runtime\prerequisites\PowerShell-7.6.9',
  [string]$GitDirectory = 'D:\tools\Git\cmd',
  [string]$LogDirectory = ''
)

$ErrorActionPreference = 'Stop'
foreach ($required in @($DesktopExe, (Join-Path $PowerShellDirectory 'pwsh.exe'), (Join-Path $GitDirectory 'git.exe'))) {
  if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Required executable missing: $required" }
}
$existing = Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $DesktopExe }
if ($existing) { throw 'DeepSeek Harness is already running. Exit it before starting with this environment.' }
. (Join-Path $PSScriptRoot 'use-d-drive-runtime.ps1') -RuntimeRoot $RuntimeRoot
$env:PATH = $PowerShellDirectory + ';' + $GitDirectory + ';' + $env:PATH
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
$launch = @{ FilePath=$DesktopExe; WorkingDirectory=(Split-Path -Parent $DesktopExe); WindowStyle='Hidden'; PassThru=$true }
if ($LogDirectory) {
  if (-not [IO.Path]::IsPathFullyQualified($LogDirectory)) { throw 'LogDirectory must be absolute' }
  New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
  $launch.RedirectStandardOutput = Join-Path $LogDirectory 'stdout.log'
  $launch.RedirectStandardError = Join-Path $LogDirectory 'stderr.log'
}
$started = Start-Process @launch
Write-Output ('DeepSeek Harness started; PID: ' + $started.Id)
