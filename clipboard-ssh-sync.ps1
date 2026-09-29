#Requires -Version 7.0
[CmdletBinding()]
param(
    [int]$PollMilliseconds = 120,
    [int64]$MaxTextBytes = 4MB,
    [int64]$MaxImageBytes = 32MB,
    [int64]$MaxFilesBytes = 256MB,
    [int]$MaxFileCount = 100,
    [switch]$NoBalloon,
    [switch]$Diagnostic
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ([Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') {
    throw 'clipboard-ssh-sync.ps1 must be started with pwsh.exe -STA'
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class ClipboardSequence
{
    [DllImport("user32.dll")]
    public static extern uint GetClipboardSequenceNumber();
}
'@

$configPath = Join-Path $PSScriptRoot 'config.psd1'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "找不到配置文件 $configPath；请复制 config.example.psd1 为 config.psd1 并填写 XXX 项"
}
$config = Import-PowerShellDataFile -LiteralPath $configPath
$sshExe = (Get-Command ssh.exe -ErrorAction Stop).Source
$scpExe = (Get-Command scp.exe -ErrorAction Stop).Source
$sshTarget = [string]$config.SshTarget
$sshPort = [string]$config.SshPort
$remoteBin = ([string]$config.RemoteBin).TrimEnd('/')
$remoteInbox = ([string]$config.RemoteInbox).TrimEnd('/')
$remote = @{
    Text      = "$remoteBin/clipboard-x11-text"
    Image     = "$remoteBin/clipboard-x11-image"
    Files     = "$remoteBin/clipboard-x11-files"
    Status    = "$remoteBin/clipboard-x11-status"
    Watch     = "$remoteBin/clipboard-x11-watch"
    ReadText  = "$remoteBin/clipboard-x11-read-text"
    ReadImage = "$remoteBin/clipboard-x11-read-image"
    ReadFiles = "$remoteBin/clipboard-x11-read-files"
    Ack       = "$remoteBin/clipboard-x11-ack"
}

$dataDir = Join-Path $env:LOCALAPPDATA 'ImeSshSync'
$downloadRoot = Join-Path $dataDir 'ClipboardInbox'
$logFile = Join-Path $dataDir 'clipboard-ssh-sync.log'
New-Item -ItemType Directory -Path $dataDir,$downloadRoot -Force | Out-Null

$createdNew = $false
$mutex = [Threading.Mutex]::new($true, 'Local\ImeSshSync.Clipboard', [ref]$createdNew)
if (-not $createdNew) { throw 'clipboard-ssh-sync is already running for this user' }

function Write-Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss.fff} {1}' -f (Get-Date), $Message
    Add-Content -LiteralPath $logFile -Value $line -Encoding utf8
    if ($Host.Name -eq 'ConsoleHost') { Write-Host $line }
}

$notifyIcon = $null
if (-not $NoBalloon) {
    $notifyIcon = [Windows.Forms.NotifyIcon]::new()
    $notifyIcon.Icon = [Drawing.SystemIcons]::Information
    $notifyIcon.Text = 'Windows <-> Linux clipboard'
    $notifyIcon.Visible = $true
}

function Show-ClipboardNotice([string]$Direction, [string]$Type) {
    if ($null -eq $notifyIcon) { return }
    $notifyIcon.BalloonTipTitle = "剪贴板已同步：$Direction"
    $notifyIcon.BalloonTipText = "类型：$Type"
    $notifyIcon.BalloonTipIcon = [Windows.Forms.ToolTipIcon]::Info
    $notifyIcon.ShowBalloonTip(1800)
}

function Get-Sha256([byte[]]$Bytes) {
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($Bytes)).ToLowerInvariant()
}

function New-ProcessInfo([string]$FileName, [string[]]$Arguments) {
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FileName
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    foreach ($argument in $Arguments) { [void]$psi.ArgumentList.Add($argument) }
    return $psi
}

function Invoke-ProcessBytes(
    [string]$FileName,
    [string[]]$Arguments,
    [AllowNull()][byte[]]$InputBytes,
    [int]$TimeoutSeconds = 30
) {
    $psi = New-ProcessInfo $FileName $Arguments
    # Every SSH/SCP call is non-interactive. Always close stdin so a remote helper
    # cannot keep the session alive waiting for inherited terminal input.
    $psi.RedirectStandardInput = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $psi
    [void]$process.Start()

    $stderrTask = $process.StandardError.ReadToEndAsync()
    $stdoutBuffer = [IO.MemoryStream]::new()
    $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($stdoutBuffer)

    if ($null -ne $InputBytes) {
        $process.StandardInput.BaseStream.Write($InputBytes, 0, $InputBytes.Length)
    }
    $process.StandardInput.BaseStream.Close()

    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $process.Kill($true) } catch {}
        throw "process timeout after ${TimeoutSeconds}s: $FileName"
    }
    $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult().Trim()
    $output = $stdoutBuffer.ToArray()
    $exitCode = $process.ExitCode
    $process.Dispose()
    $stdoutBuffer.Dispose()

    if ($exitCode -ne 0) {
        $text = if ($output.Length) { [Text.Encoding]::UTF8.GetString($output).Trim() } else { $stderr }
        throw "exit=$exitCode $text"
    }
    return [pscustomobject]@{ Bytes = $output; Stderr = $stderr }
}

function Get-SshArguments([string[]]$RemoteArguments) {
    $baseArguments = @(
        '-T', '-p', $sshPort,
        '-o', 'BatchMode=yes',
        '-o', 'ConnectTimeout=8',
        '-o', 'ServerAliveInterval=10',
        '-o', 'ServerAliveCountMax=2',
        $sshTarget
    )

    # Stable clipboard APIs need the X server on the Windows SSH client. A process
    # launched by Quicker has no DISPLAY, so derive the client IP from SSH_CONNECTION.
    if ($RemoteArguments.Count -gt 0 -and $RemoteArguments[0] -like "$remoteBin/clipboard-x11-*") {
        foreach ($argument in $RemoteArguments) {
            if ($argument -notmatch '^[A-Za-z0-9_./:-]+$') {
                throw "unsafe remote API argument: $argument"
            }
        }
        $remoteCommand = 'DISPLAY=${SSH_CONNECTION%% *}:0.0 ' + ($RemoteArguments -join ' ')
        return $baseArguments + @($remoteCommand)
    }

    return $baseArguments + $RemoteArguments
}

function Invoke-SshBytes([string[]]$RemoteArguments, [AllowNull()][byte[]]$InputBytes, [int]$TimeoutSeconds = 30) {
    return Invoke-ProcessBytes $sshExe (Get-SshArguments $RemoteArguments) $InputBytes $TimeoutSeconds
}

function Invoke-SshJson([string[]]$RemoteArguments, [AllowNull()][byte[]]$InputBytes, [int]$TimeoutSeconds = 30) {
    $result = Invoke-SshBytes $RemoteArguments $InputBytes $TimeoutSeconds
    $text = [Text.Encoding]::UTF8.GetString($result.Bytes).Trim()
    try { $json = $text | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "invalid remote JSON: $text" }
    if (-not $json.ok) { throw "remote error: $text" }
    return $json
}

function Convert-ClipboardImageToPng {
    $image = [Windows.Forms.Clipboard]::GetImage()
    if ($null -eq $image) { return $null }
    try {
        $stream = [IO.MemoryStream]::new()
        $image.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
        return $stream.ToArray()
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $image.Dispose()
    }
}

function Get-LocalClipboardPayload {
    if ([Windows.Forms.Clipboard]::ContainsFileDropList()) {
        $paths = @([Windows.Forms.Clipboard]::GetFileDropList() | ForEach-Object { [string]$_ })
        return [pscustomobject]@{ Type = 'files'; Paths = $paths }
    }
    if ([Windows.Forms.Clipboard]::ContainsImage()) {
        return [pscustomobject]@{ Type = 'image'; Bytes = [byte[]](Convert-ClipboardImageToPng) }
    }
    if ([Windows.Forms.Clipboard]::ContainsText([Windows.Forms.TextDataFormat]::UnicodeText)) {
        $text = [Windows.Forms.Clipboard]::GetText([Windows.Forms.TextDataFormat]::UnicodeText)
        return [pscustomobject]@{ Type = 'text'; Bytes = [Text.UTF8Encoding]::new($false, $true).GetBytes($text) }
    }
    return $null
}

function Send-LocalTextOrImage($Payload) {
    $limit = if ($Payload.Type -eq 'text') { $MaxTextBytes } else { $MaxImageBytes }
    if ($Payload.Bytes.Length -gt $limit) { throw "$($Payload.Type) payload too large: $($Payload.Bytes.Length) > $limit" }
    $command = if ($Payload.Type -eq 'text') { $remote.Text } else { $remote.Image }
    $reply = Invoke-SshJson @($command) $Payload.Bytes 45
    Write-Log "upload type=$($Payload.Type) bytes=$($Payload.Bytes.Length) sha=$($reply.sha256) deduplicated=$([bool]$reply.deduplicated)"
    Show-ClipboardNotice 'Windows → Linux' $Payload.Type
    return [string]$reply.sha256
}

function Send-LocalFiles([string[]]$Paths) {
    $files = @($Paths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | ForEach-Object { Get-Item -LiteralPath $_ })
    if ($files.Count -eq 0) { throw 'clipboard file list contains no regular files' }
    if ($files.Count -gt $MaxFileCount) { throw "too many files: $($files.Count) > $MaxFileCount" }
    $totalBytes = [int64](($files | Measure-Object Length -Sum).Sum)
    if ($totalBytes -gt $MaxFilesBytes) { throw "files too large: $totalBytes > $MaxFilesBytes" }

    $transferId = [guid]::NewGuid().ToString()
    $remoteRoot = "$remoteInbox/$transferId"
    [void](Invoke-SshJson @('python3','-c','import json,os,sys; p=sys.argv[1]; os.makedirs(p,mode=0o700,exist_ok=False); print(json.dumps({"ok":True}))',$remoteRoot) $null 20)

    $remotePaths = [Collections.Generic.List[string]]::new()
    for ($i = 0; $i -lt $files.Count; $i++) {
        $slot = '{0:D4}' -f ($i + 1)
        $remoteDir = "$remoteRoot/$slot"
        [void](Invoke-SshJson @('python3','-c','import json,os,sys; os.mkdir(sys.argv[1],0o700); print(json.dumps({"ok":True}))',$remoteDir) $null 20)
        $remotePath = "$remoteDir/$($files[$i].Name)"
        $scpArgs = @('-P',$sshPort,'-q','-o','BatchMode=yes','-o','ConnectTimeout=8','--',$files[$i].FullName,"${sshTarget}:$remotePath")
        [void](Invoke-ProcessBytes $scpExe $scpArgs $null 180)
        $remotePaths.Add($remotePath)
    }

    $manifest = @{ operation = 'copy'; paths = @($remotePaths) } | ConvertTo-Json -Compress -Depth 4
    $manifestBytes = [Text.UTF8Encoding]::new($false).GetBytes($manifest)
    $reply = Invoke-SshJson @($remote.Files) $manifestBytes 45
    Write-Log "upload type=files count=$($files.Count) bytes=$totalBytes sha=$($reply.sha256) deduplicated=$([bool]$reply.deduplicated)"
    Show-ClipboardNotice 'Windows → Linux' 'files'
    return [string]$reply.sha256
}

function Set-ClipboardWithRetry([scriptblock]$Action) {
    $lastError = $null
    for ($i = 0; $i -lt 8; $i++) {
        try { & $Action; return }
        catch { $lastError = $_; Start-Sleep -Milliseconds 80 }
    }
    throw $lastError
}

function Ack-RemoteGeneration([long]$Generation) {
    try { [void](Invoke-SshJson @($remote.Ack,[string]$Generation) $null 20) }
    catch { Write-Log "ack-failed generation=$Generation error=$($_.Exception.Message)" }
}

function Receive-RemoteEvent($Event) {
    $generation = [long]$Event.generation
    $type = [string]$Event.type
    if ($Event.origin -eq 'windows') { return }
    $remoteKey = "${type}:$($Event.sha256)"
    if ($remoteKey -eq $script:lastReceivedKey) {
        Ack-RemoteGeneration $generation
        Write-Log "download-suppressed generation=$generation key=$remoteKey"
        return
    }

    if ($type -eq 'text') {
        $result = Invoke-SshBytes @($remote.ReadText,[string]$generation) $null 30
        if ($result.Bytes.Length -gt $MaxTextBytes) { throw "remote text too large: $($result.Bytes.Length)" }
        $text = [Text.UTF8Encoding]::new($false, $true).GetString($result.Bytes)
        Set-ClipboardWithRetry { [Windows.Forms.Clipboard]::SetText($text, [Windows.Forms.TextDataFormat]::UnicodeText) }
    }
    elseif ($type -eq 'image') {
        $result = Invoke-SshBytes @($remote.ReadImage,[string]$generation) $null 45
        if ($result.Bytes.Length -gt $MaxImageBytes) { throw "remote image too large: $($result.Bytes.Length)" }
        $stream = [IO.MemoryStream]::new($result.Bytes, $false)
        $source = [Drawing.Image]::FromStream($stream, $true, $true)
        $bitmap = [Drawing.Bitmap]::new($source)
        try { Set-ClipboardWithRetry { [Windows.Forms.Clipboard]::SetImage($bitmap) } }
        finally { $bitmap.Dispose(); $source.Dispose(); $stream.Dispose() }
    }
    elseif ($type -eq 'files') {
        $reply = Invoke-SshBytes @($remote.ReadFiles,[string]$generation) $null 30
        $manifestText = [Text.Encoding]::UTF8.GetString($reply.Bytes)
        $manifest = $manifestText | ConvertFrom-Json -ErrorAction Stop
        $entries = @($manifest.files)
        if ($entries.Count -eq 0 -or $entries.Count -gt $MaxFileCount) { throw "invalid remote file count: $($entries.Count)" }
        $totalBytes = [int64](($entries | Measure-Object size -Sum).Sum)
        if ($totalBytes -gt $MaxFilesBytes) { throw "remote files too large: $totalBytes" }
        $localGenerationDir = Join-Path $downloadRoot ([string]$generation)
        New-Item -ItemType Directory -Path $localGenerationDir -Force | Out-Null
        $localPaths = [Collections.Specialized.StringCollection]::new()
        for ($i = 0; $i -lt $entries.Count; $i++) {
            $remotePath = [string]$entries[$i].path
            if (-not $remotePath.StartsWith('/')) { throw "remote file path is not absolute: $remotePath" }
            $name = ($remotePath -split '/')[-1]
            if ([string]::IsNullOrWhiteSpace($name)) { throw 'remote file has empty name' }
            $slotDir = Join-Path $localGenerationDir ('{0:D4}' -f ($i + 1))
            New-Item -ItemType Directory -Path $slotDir -Force | Out-Null
            $localPath = Join-Path $slotDir $name
            $scpArgs = @('-P',$sshPort,'-q','-o','BatchMode=yes','-o','ConnectTimeout=8','--',"${sshTarget}:$remotePath",$localPath)
            [void](Invoke-ProcessBytes $scpExe $scpArgs $null 180)
            [void]$localPaths.Add($localPath)
        }
        Set-ClipboardWithRetry { [Windows.Forms.Clipboard]::SetFileDropList($localPaths) }
    }
    else { throw "unsupported remote clipboard type: $type" }

    $script:lastClipboardSequence = [ClipboardSequence]::GetClipboardSequenceNumber()
    $script:lastUploadedKey = "${type}:$($Event.sha256)"
    $script:lastReceivedKey = $remoteKey
    Ack-RemoteGeneration $generation
    Write-Log "download type=$type generation=$generation bytes=$($Event.size) sha=$($Event.sha256)"
    Show-ClipboardNotice 'Linux → Windows' $type
}

function Start-RemoteWatcher {
    $psi = New-ProcessInfo $sshExe (Get-SshArguments @($remote.Watch))
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $psi
    [void]$process.Start()
    return [pscustomobject]@{
        Process = $process
        LineTask = $process.StandardOutput.ReadLineAsync()
        ErrorTask = $process.StandardError.ReadToEndAsync()
    }
}

$watcher = $null
$watchRetryAt = [datetime]::MinValue
$watchFailures = 0
$lastClipboardSequence = [ClipboardSequence]::GetClipboardSequenceNumber()
$lastUploadedKey = $null
$lastReceivedKey = $null
Write-Log "started target=${sshTarget}:${sshPort} local-sequence=$lastClipboardSequence"

try {
    while ($true) {
        if ($null -eq $watcher -and [datetime]::UtcNow -ge $watchRetryAt) {
            try {
                $watcher = Start-RemoteWatcher
                $watchFailures = 0
                Write-Log 'watch connected'
            }
            catch {
                $watchFailures++
                $delay = [Math]::Min(30, [Math]::Pow(2, [Math]::Min($watchFailures, 5)))
                $watchRetryAt = [datetime]::UtcNow.AddSeconds($delay)
                Write-Log "watch-connect-failed retry=${delay}s error=$($_.Exception.Message)"
            }
        }

        if ($null -ne $watcher -and $watcher.LineTask.IsCompleted) {
            try {
                $line = $watcher.LineTask.GetAwaiter().GetResult()
                if ($null -eq $line) { throw 'watch stream closed' }
                $watcher.LineTask = $watcher.Process.StandardOutput.ReadLineAsync()
                if (-not [string]::IsNullOrWhiteSpace($line)) {
                    try {
                        $event = $line | ConvertFrom-Json -ErrorAction Stop
                        if ($event.ok -and $event.event -eq 'clipboard') {
                            Write-Log "watch-event generation=$($event.generation) type=$($event.type) origin=$($event.origin) bytes=$($event.size) sha=$($event.sha256)"
                            Receive-RemoteEvent $event
                        }
                    }
                    catch { Write-Log "watch-event-failed error=$($_.Exception.Message)" }
                }
            }
            catch {
                $stderr = if ($watcher.ErrorTask.IsCompleted) { $watcher.ErrorTask.GetAwaiter().GetResult().Trim() } else { '' }
                try { if (-not $watcher.Process.HasExited) { $watcher.Process.Kill($true) } } catch {}
                $watcher.Process.Dispose()
                $watcher = $null
                $watchFailures++
                $delay = [Math]::Min(30, [Math]::Pow(2, [Math]::Min($watchFailures, 5)))
                $watchRetryAt = [datetime]::UtcNow.AddSeconds($delay)
                Write-Log "watch-disconnected retry=${delay}s error=$($_.Exception.Message) stderr=$stderr"
            }
        }

        $sequence = [ClipboardSequence]::GetClipboardSequenceNumber()
        if ($sequence -ne 0 -and $sequence -ne $lastClipboardSequence) {
            try {
                $payload = Get-LocalClipboardPayload
                if ($null -ne $payload) {
                    if ($payload.Type -eq 'files') {
                        $keyBytes = [Text.Encoding]::UTF8.GetBytes((@($payload.Paths) -join "`0"))
                        $localKey = "files:$(Get-Sha256 $keyBytes)"
                    }
                    else {
                        $localKey = "$($payload.Type):$(Get-Sha256 $payload.Bytes)"
                    }

                    if ($localKey -ne $lastUploadedKey) {
                        if ($payload.Type -eq 'files') { $remoteHash = Send-LocalFiles $payload.Paths }
                        else { $remoteHash = Send-LocalTextOrImage $payload }
                        $lastUploadedKey = $localKey
                    }
                    elseif ($Diagnostic) { Write-Log "local-suppressed key=$localKey" }
                }
                $lastClipboardSequence = $sequence
            }
            catch {
                Write-Log "local-upload-failed sequence=$sequence error=$($_.Exception.Message)"
                Start-Sleep -Milliseconds 500
            }
        }

        [Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds $PollMilliseconds
    }
}
finally {
    if ($null -ne $watcher) {
        try { if (-not $watcher.Process.HasExited) { $watcher.Process.Kill($true) } } catch {}
        $watcher.Process.Dispose()
    }
    if ($null -ne $notifyIcon) { $notifyIcon.Visible = $false; $notifyIcon.Dispose() }
    if ($createdNew) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
    Write-Log 'stopped'
}
