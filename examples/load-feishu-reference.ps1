# Dot-source this helper to import selected .env values into the current process only.
# It never executes the file, prints values, writes credentials or changes the source.
param([Parameter(Mandatory = $true)][string]$Path)
$referenceKeys = @(
  'FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_VERIFICATION_TOKEN', 'FEISHU_ENCRYPT_KEY',
  'FEISHU_ADMIN_USER_IDS', 'FEISHU_API_BASE_URL', 'FEISHU_WEBHOOK_HOST',
  'FEISHU_WEBHOOK_PORT', 'FEISHU_WEBHOOK_PATH'
)
foreach ($referenceLine in [System.IO.File]::ReadAllLines((Resolve-Path -LiteralPath $Path).Path)) {
  if ($referenceLine -match '^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$') {
    $referenceName = $Matches[1]
    $referenceValue = $Matches[2]
    if ($referenceKeys -notcontains $referenceName) { continue }
    if ($referenceValue.Length -ge 2 -and (
      ($referenceValue.StartsWith('"') -and $referenceValue.EndsWith('"')) -or
      ($referenceValue.StartsWith("'") -and $referenceValue.EndsWith("'")))) {
      $referenceValue = $referenceValue.Substring(1, $referenceValue.Length - 2)
    }
    [System.Environment]::SetEnvironmentVariable($referenceName, $referenceValue, 'Process')
  }
}
Remove-Variable referenceKeys, referenceLine, referenceName, referenceValue -ErrorAction SilentlyContinue
