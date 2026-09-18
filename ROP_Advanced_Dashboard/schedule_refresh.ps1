param(
    [Parameter(Mandatory=$true)][string]$InventoryFile,
    [string]$SnapshotDate = (Get-Date -Format "yyyy-MM-dd"),
    [string]$ProjectFolder = $PSScriptRoot
)

$Python = "python"
$Script = Join-Path $ProjectFolder "refresh_inventory.py"
& $Python $Script --inventory $InventoryFile --snapshot-date $SnapshotDate --project $ProjectFolder
if ($LASTEXITCODE -ne 0) {
    throw "ROP dashboard refresh failed with exit code $LASTEXITCODE"
}
