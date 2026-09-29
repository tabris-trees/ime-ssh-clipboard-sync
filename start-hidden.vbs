Option Explicit
Dim shell, folder, imeScript, clipboardScript, command
Set shell = CreateObject("WScript.Shell")
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
imeScript = folder & "\ime-ssh-sync.ps1"
clipboardScript = folder & "\clipboard-ssh-sync.ps1"
command = "pwsh.exe -NoLogo -NoProfile -STA -WindowStyle Hidden -File """ & imeScript & """"
shell.Run command, 0, False
command = "pwsh.exe -NoLogo -NoProfile -STA -WindowStyle Hidden -File """ & clipboardScript & """"
shell.Run command, 0, False
