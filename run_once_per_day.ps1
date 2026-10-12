$ErrorActionPreference = "Stop"
$root = "C:\Users\aiche\schnaeppchen"
$stamp = Join-Path $root "last_run.txt"
$today = Get-Date -Format "yyyy-MM-dd"
if (Test-Path $stamp) {
  $last = (Get-Content $stamp -Raw).Trim()
  if ($last -eq $today) { exit 0 }
}
# CPU-Affinitaet FFFC: Kern 0 (APIC 0/1) meiden, der wiederholt Machine-Check-Fehler wirft. Kindprozesse (Python, Chromium) erben die Maske.
try { [System.Diagnostics.Process]::GetCurrentProcess().ProcessorAffinity = [IntPtr]0xFFFC } catch { }
& "C:\Users\aiche\AppData\Local\Programs\Python\Python314\python.exe" (Join-Path $root "schnaeppchen.py")
if ($LASTEXITCODE -eq 0) {
  Set-Content -Path $stamp -Value $today -Encoding ascii
}
exit $LASTEXITCODE
