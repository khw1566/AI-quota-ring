# AI quota-ring installer
#   .\install.ps1            register in Windows startup and launch now
#   .\install.ps1 -Uninstall remove from startup and stop the widget
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "AI quota-ring.lnk"

function Stop-Widget {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -like "*$dir*widget.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

if ($Uninstall) {
    Stop-Widget
    if (Test-Path $lnk) { Remove-Item $lnk }
    Write-Host "Removed from startup."
    return
}

# Find pythonw: prefer the py launcher (pyw), else pythonw.exe next to python
$pyw = (Get-Command pyw -ErrorAction SilentlyContinue).Source
if (-not $pyw -or $pyw -like "*WindowsApps*") {
    $py = & python -c "import sys; print(sys.executable)" 2>$null
    if ($py) { $pyw = Join-Path (Split-Path $py) "pythonw.exe" }
}
if (-not $pyw -or -not (Test-Path $pyw)) { throw "Python 3.10+ (with tkinter) is required." }

$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = "`"$dir\widget.py`""
$s.WorkingDirectory = $dir
$s.Description = "AI quota-ring taskbar widget"
$s.Save()

Stop-Widget
Start-Process -FilePath $pyw -ArgumentList "`"$dir\widget.py`"" -WorkingDirectory $dir
Write-Host "Installed: $lnk"
