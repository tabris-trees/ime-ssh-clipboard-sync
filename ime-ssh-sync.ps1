[CmdletBinding()]
param(
    [switch]$NoBalloon,
    [switch]$Once,
    [ValidateSet('CN', 'EN')]
    [string]$InitialState = 'CN'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$windowsFormsAssemblies = @(
    [System.Windows.Forms.Form].Assembly.Location
    [System.Windows.Forms.NativeWindow].Assembly.Location
    [System.Windows.Forms.Message].Assembly.Location
) | Select-Object -Unique
Add-Type -ReferencedAssemblies $windowsFormsAssemblies -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Windows.Forms;

public sealed class RemoteImeHotKeyWindow : NativeWindow, IDisposable
{
    private const int WM_HOTKEY = 0x0312;
    private const int HOTKEY_ID = 0x4953;
    private const uint MOD_CONTROL = 0x0002;
    private const uint MOD_SHIFT = 0x0004;
    private const uint MOD_NOREPEAT = 0x4000;
    private const uint VK_S = 0x53;
    private bool registered;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool RegisterHotKey(IntPtr hWnd, int id, uint modifiers, uint virtualKey);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool UnregisterHotKey(IntPtr hWnd, int id);

    public event EventHandler HotKeyPressed;

    public RemoteImeHotKeyWindow()
    {
        CreateHandle(new CreateParams());
        registered = RegisterHotKey(Handle, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_S);
        if (!registered)
        {
            int error = Marshal.GetLastWin32Error();
            DestroyHandle();
            throw new InvalidOperationException("无法注册 Ctrl+Shift+S；该快捷键可能已被其他程序占用。Win32 error=" + error);
        }
    }

    protected override void WndProc(ref Message message)
    {
        if (message.Msg == WM_HOTKEY && message.WParam.ToInt32() == HOTKEY_ID)
            HotKeyPressed?.Invoke(this, EventArgs.Empty);
        base.WndProc(ref message);
    }

    public void Dispose()
    {
        if (registered)
        {
            UnregisterHotKey(Handle, HOTKEY_ID);
            registered = false;
        }
        DestroyHandle();
    }
}
'@

$configPath = Join-Path $PSScriptRoot 'config.psd1'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "找不到配置文件 $configPath；请复制 config.example.psd1 为 config.psd1 并填写 XXX 项"
}
$config = Import-PowerShellDataFile -LiteralPath $configPath
$sshExe = (Get-Command ssh.exe -ErrorAction Stop).Source
$sshTarget = [string]$config.SshTarget
$sshPort = [string]$config.SshPort
$remoteBin = ([string]$config.RemoteBin).TrimEnd('/')
$remoteDisplay = [string]$config.RemoteDisplay
if ($remoteDisplay -and $remoteDisplay -notmatch '^[A-Za-z0-9_.-]*:[0-9]+(\.[0-9]+)?$') {
    throw 'RemoteDisplay must be an X11 DISPLAY such as localhost:10.0'
}
$remoteScripts = @{
    CN = "$remoteBin/ibus-x11-cn"
    EN = "$remoteBin/ibus-x11-en"
}

$logDir = Join-Path $env:LOCALAPPDATA 'ImeSshSync'
$logFile = Join-Path $logDir 'ime-ssh-sync.log'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

$createdNew = $false
$mutex = [Threading.Mutex]::new($true, 'Local\ImeSshSync.Ime', [ref]$createdNew)
if (-not $createdNew) { throw 'ime-ssh-sync is already running for this user' }

function Write-Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss.fff} {1}' -f (Get-Date), $Message
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    if ($Host.Name -eq 'ConsoleHost') { Write-Host $line }
}

$notifyIcon = $null
if (-not $NoBalloon) {
    $notifyIcon = [System.Windows.Forms.NotifyIcon]::new()
    $notifyIcon.Icon = [System.Drawing.SystemIcons]::Information
    $notifyIcon.Text = 'Remote IBus toggle'
    $notifyIcon.Visible = $true
}

function Show-State([string]$State, [string]$RemoteState) {
    if ($null -eq $notifyIcon) { return }
    $displayState = if ($State -eq 'CN') { '中文' } else { '英文' }
    $notifyIcon.BalloonTipTitle = "远端输入法已切换：$displayState"
    $notifyIcon.BalloonTipText = "远端输入引擎：$RemoteState"
    $notifyIcon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
    $notifyIcon.ShowBalloonTip(2500)
}

function Invoke-RemoteState([string]$State) {
    # The server selects the matching X11/IBus session from the SSH source.
    # RemoteDisplay is only an explicit override for ambiguous multi-session cases.
    $remoteCommand = $remoteScripts[$State]
    if ($remoteDisplay) { $remoteCommand += " --display $remoteDisplay" }
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $sshExe
    foreach ($argument in @('-T', '-p', $sshPort, '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=5',
            '-o', 'ServerAliveCountMax=1', $sshTarget, $remoteCommand)) {
        [void]$psi.ArgumentList.Add($argument)
    }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    $psi.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $psi
    [void]$process.Start()
    $process.StandardInput.Close()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(12000)) {
        try { $process.Kill($true) } catch {}
        throw 'SSH IME switch timed out after 12 seconds'
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult().Trim()
    $stderr = $stderrTask.GetAwaiter().GetResult().Trim()

    if ($process.ExitCode -ne 0) {
        throw "ssh exit=$($process.ExitCode): $stderr"
    }
    if ([string]::IsNullOrWhiteSpace($stdout)) { $stdout = '(无输出)' }
    return $stdout
}

$runtime = @{ NextState = $InitialState; Busy = $false }
Write-Log "started mode=remote-toggle hotkey=Ctrl+Shift+S next=$InitialState once=$([bool]$Once) target=${sshTarget}:${sshPort}"

function Switch-RemoteIme {
    if ($runtime.Busy) {
        Write-Log 'hotkey ignored=busy'
        return
    }

    $runtime.Busy = $true
    $state = $runtime.NextState
    try {
        $remoteState = $null
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                $remoteState = Invoke-RemoteState $state
                break
            }
            catch {
                Write-Log "hotkey retry state=$state attempt=$attempt error=$($_.Exception.Message)"
                if ($attempt -eq 3) { throw }
                Start-Sleep -Milliseconds 1500
            }
        }
        $runtime.NextState = if ($state -eq 'CN') { 'EN' } else { 'CN' }
        Write-Log "hotkey switched=$state remote=$remoteState next=$($runtime.NextState)"
        Show-State $state $remoteState
    }
    catch {
        Write-Log "hotkey switch-failed state=$state error=$($_.Exception.Message)"
    }
    finally {
        $runtime.Busy = $false
    }
}

$hotKeyWindow = $null
try {
    if ($Once) {
        Switch-RemoteIme
    }
    else {
        $hotKeyWindow = [RemoteImeHotKeyWindow]::new()
        $hotKeyWindow.add_HotKeyPressed({ Switch-RemoteIme })
        Write-Log 'hotkey-registered Ctrl+Shift+S'
        [System.Windows.Forms.Application]::Run()
    }
}
finally {
    if ($null -ne $hotKeyWindow) { $hotKeyWindow.Dispose() }
    if ($null -ne $notifyIcon) {
        $notifyIcon.Visible = $false
        $notifyIcon.Dispose()
    }
    if ($createdNew) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
    Write-Log 'stopped'
}
