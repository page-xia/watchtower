$token = [Environment]::GetEnvironmentVariable('WATCH_INGEST_TOKEN', 'User')
if (-not $token) { Write-Output 'TOKEN_NOT_FOUND'; exit 1 }
try {
    $response = Invoke-RestMethod -Uri 'https://watch.omnisource.xin/api/messages/evidence/prebuild' -Method Post -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 30
    $response | ConvertTo-Json -Compress
}
catch {
    Write-Output ("HTTP_ERROR: " + $_.Exception.Message)
    if ($_.ErrorDetails) { Write-Output $_.ErrorDetails.Message }
}
