param([string]$RuntimeRoot = 'D:\files_data\windborne\dsh-runtime')

# Dot-source in the launch shell. This changes only the current process environment.
$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathFullyQualified($RuntimeRoot)) { throw 'RuntimeRoot must be absolute' }
foreach ($child in @('tmp','cache\npm','cache\pnpm\store')) {
  New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeRoot $child) | Out-Null
}
$env:TELEBOT_RUNTIME_ROOT = $RuntimeRoot
$env:TELEBOT_VERIFY_TEMP = Join-Path $RuntimeRoot 'tmp'
$env:TEMP = $env:TELEBOT_VERIFY_TEMP
$env:TMP = $env:TELEBOT_VERIFY_TEMP
$env:npm_config_cache = Join-Path $RuntimeRoot 'cache\npm'
$env:npm_config_store_dir = Join-Path $RuntimeRoot 'cache\pnpm\store'
$env:PNPM_CONFIG_STORE_DIR = $env:npm_config_store_dir

# Use the verified project version when provisioned on D:, without changing the
# user's global package manager or triggering its network version switcher.
$telebotPnpmBin = Join-Path $RuntimeRoot 'prerequisites\pnpm-11.28.5\node_modules\.bin'
if ((Test-Path -LiteralPath (Join-Path $telebotPnpmBin 'pnpm.cmd')) -and (($env:PATH -split ';') -notcontains $telebotPnpmBin)) {
  $env:PATH = $telebotPnpmBin + ';' + $env:PATH
}

# Leave filesystem permissions to the existing account and official sandbox.
# Preparing cache paths must not change a persistent directory ACL.
Write-Output ('This shell uses D-drive runtime/cache/temp: ' + $RuntimeRoot)
