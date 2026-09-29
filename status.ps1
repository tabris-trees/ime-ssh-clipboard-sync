[CmdletBinding()]
param()

$configPath = Join-Path $PSScriptRoot 'config.psd1'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "找不到配置文件 $configPath；请复制 config.example.psd1 为 config.psd1 并填写 XXX 项"
}
$config = Import-PowerShellDataFile -LiteralPath $configPath
$sshTarget = [string]$config.SshTarget
$sshPort = [string]$config.SshPort
$remoteBin = ([string]$config.RemoteBin).TrimEnd('/')

$processes = Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and (
        $_.CommandLine -like '*ime-ssh-sync.ps1*' -or
        $_.CommandLine -like '*clipboard-ssh-sync.ps1*' -or
        $_.CommandLine -like '*clipboard-x11-watch*'
    )
}

if ($processes) {
    $processes | Select-Object ProcessId,Name,@{Name='Role';Expression={
        if ($_.CommandLine -like '*ime-ssh-sync.ps1*') { 'IME' }
        elseif ($_.CommandLine -like '*clipboard-x11-watch*') { 'SSH watch' }
        else { 'Clipboard' }
    }} | Format-Table -AutoSize
}
else {
    Write-Host 'No local ImeSshSync processes are running.'
}

Write-Host "`nRemote clipboard status:"
& ssh.exe -T -p $sshPort -o BatchMode=yes -o ConnectTimeout=8 $sshTarget ('DISPLAY=${SSH_CONNECTION%% *}:0.0 ' + "$remoteBin/clipboard-x11-status")

Write-Host "`nRecent IME log:"
Get-Content -LiteralPath "$env:LOCALAPPDATA\ImeSshSync\ime-ssh-sync.log" -Tail 5 -ErrorAction SilentlyContinue
Write-Host "`nRecent clipboard log:"
Get-Content -LiteralPath "$env:LOCALAPPDATA\ImeSshSync\clipboard-ssh-sync.log" -Tail 5 -ErrorAction SilentlyContinue

