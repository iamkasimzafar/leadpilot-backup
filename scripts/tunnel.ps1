# Opens an SSH tunnel to the LeadPilot MySQL server and holds it open.
#
# The database only listens on the server's own loopback, so local development
# reaches it through this tunnel. Leave this running in its own terminal, then
# the app connects to 127.0.0.1:3306 exactly as .env already specifies.
#
# Usage: .\scripts\tunnel.ps1 [-SshUser ubuntu] [-SshHost 43.157.81.74] [-LocalPort 3306]
#
# You will be prompted for the SSH password unless key auth is set up. To stop
# being prompted, install your public key once:
#   type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh ubuntu@43.157.81.74 `
#     "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"

param(
    [string]$SshHost = '43.157.81.74',
    [string]$SshUser = 'ubuntu',
    [int]$SshPort = 22,
    [int]$LocalPort = 3306,
    [string]$RemoteHost = '127.0.0.1',
    [int]$RemotePort = 3306
)

$ErrorActionPreference = 'Stop'

$inUse = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    Write-Warning "Local port $LocalPort is already in use. Close the existing tunnel or pass -LocalPort <other>."
    exit 1
}

Write-Host "Tunnelling 127.0.0.1:$LocalPort -> $RemoteHost`:$RemotePort via $SshUser@$SshHost" -ForegroundColor Cyan
Write-Host "Leave this window open. Press Ctrl+C to close the tunnel." -ForegroundColor DarkGray

# -N: no remote command, just forward. -o ExitOnForwardFailure: fail loudly if the
# local port cannot be bound, rather than appearing to succeed.
ssh -N `
    -L "${LocalPort}:${RemoteHost}:${RemotePort}" `
    -p $SshPort `
    -o ExitOnForwardFailure=yes `
    -o ServerAliveInterval=30 `
    -o ServerAliveCountMax=3 `
    "$SshUser@$SshHost"
