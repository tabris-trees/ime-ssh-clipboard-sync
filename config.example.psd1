@{
    # Copy this file to config.psd1, then replace the XXX placeholders.
    SshTarget  = 'XXX@XXX'
    SshPort    = 'XXX'

    RemoteBin   = '/home/XXX/software/bin'
    RemoteInbox = '/home/XXX/.local/share/windows-clipboard-sync/inbox'

    # Usually leave empty so the server selects the X11 session from the SSH source.
    # Set an explicit value such as 'localhost:10.0' only if the server reports ambiguity.
    RemoteDisplay = ''
}
