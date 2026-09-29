[CmdletBinding()]
param()

$launcher = Join-Path $PSScriptRoot 'start-hidden.vbs'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "Missing launcher: $launcher" }

$startup = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)
$shortcutPath = Join-Path $startup 'ImeSshSync.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
$shortcut.Arguments = '"{0}"' -f $launcher
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = 'Windows IME and clipboard SSH sync'
$shortcut.Save()
Write-Host "Installed user logon startup shortcut: $shortcutPath"

