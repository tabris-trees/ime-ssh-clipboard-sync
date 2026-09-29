[CmdletBinding()]
param()

$startup = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)
$shortcutPath = Join-Path $startup 'ImeSshSync.lnk'
if (Test-Path -LiteralPath $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath -Force
    Write-Host "Removed: $shortcutPath"
}
else {
    Write-Host 'Startup shortcut is not installed.'
}
