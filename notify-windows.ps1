# Optional, Windows only: collect, rebuild index.html, and pop a notification when something changed.
# Point Windows Task Scheduler at this script to run it on a schedule.
$ErrorActionPreference = 'Continue'
$log = Join-Path $PSScriptRoot 'run.log'
if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) { Remove-Item $log -Confirm:$false }

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" | Out-File $log -Append -Encoding utf8
python -I (Join-Path $PSScriptRoot 'collect.py') 2>&1 | Out-File $log -Append -Encoding utf8

$changes = @(Get-Content (Join-Path $PSScriptRoot 'changes.txt') -Encoding utf8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
if ($changes.Count -eq 0) { return }

$title = "Branch Flow: $($changes.Count) change(s)"
$body = ($changes | Select-Object -First 4) -join "`n"
if ($changes.Count -gt 4) { $body += "`n..." }

[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$esc = { param($s) [Security.SecurityElement]::Escape($s) }
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$page = (Join-Path $PSScriptRoot 'index.html') -replace '\\', '/'
$xml.LoadXml("<toast activationType=`"protocol`" launch=`"file:///$page`"><visual><binding template=`"ToastGeneric`"><text>$(& $esc $title)</text><text>$(& $esc $body)</text></binding></visual></toast>")
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
