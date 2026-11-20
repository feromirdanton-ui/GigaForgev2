# Create the GigaForge shortcuts (Desktop + Start Menu) with the app icon.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1 `
#       -Root "C:\path\to\GigaForge-main" [-PythonW "pythonw.exe"] [-Uninstall]
#
# Kept as a .ps1 (not inline in a .bat) so the quoting is real PowerShell rather
# than caret-escaped batch — the inline version is where shortcut installers
# usually break.
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$PythonW = "pythonw",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$icon = Join-Path $Root "assets\gigaforge.ico"
if (-not (Test-Path $icon)) {
    $maker = Join-Path $Root "scripts\make_icon.py"
    if (Test-Path $maker) {
        Write-Host "   [..] icon missing - generating it"
        & python $maker | Out-Null
    }
}

$targets = @(
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs")
)

if ($Uninstall) {
    foreach ($dir in $targets) {
        $lnk = Join-Path $dir "GigaForge.lnk"
        if (Test-Path $lnk) {
            Remove-Item $lnk -Force
            Write-Host "   [--] removed $lnk"
        }
    }
    Write-Host "Done."
    exit 0
}

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in $targets) {
    if (-not (Test-Path $dir)) { continue }
    $lnk = Join-Path $dir "GigaForge.lnk"
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $PythonW
    $s.Arguments = "-m gigaforge.desktop"
    $s.WorkingDirectory = $Root
    if (Test-Path $icon) { $s.IconLocation = $icon } else { $s.IconLocation = "$env:SystemRoot\System32\shell32.dll,13" }
    $s.Description = "GigaForge - agentic orchestration harness"
    $s.WindowStyle = 1
    $s.Save()
    Write-Host "   [OK] $lnk"
}

Write-Host "Done. Double-click `"GigaForge`" on the Desktop."
