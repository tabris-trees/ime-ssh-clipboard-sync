[CmdletBinding()]
param()

$targets = Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and (
        $_.CommandLine -like '*ime-ssh-sync.ps1*' -or
        $_.CommandLine -like '*clipboard-ssh-sync.ps1*' -or
        $_.CommandLine -like '*clipboard-x11-watch*'
    )
}

if (-not $targets) {
    Write-Host 'ImeSshSync is not running.'
    exit 0
}

$targets | ForEach-Object {
    Write-Host "Stopping $($_.Name) pid=$($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

