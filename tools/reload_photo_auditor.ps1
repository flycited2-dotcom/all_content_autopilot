param([string[]]$RecheckIds=@())
$ErrorActionPreference='Stop'
foreach ($auditId in $RecheckIds) {
 if ($auditId -notmatch '^[a-f0-9]{32}$') { throw 'Invalid audit id' }
}
$projectRoot=Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
 $auditors=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '-m photo_auditor run$' })
 if ($auditors.Count -ne 1) { throw 'Expected one audit service' }
 try {
  Stop-ScheduledTask -TaskName 'AgentsMY Photo Auditor'
  for ($reloadTick=0; $reloadTick -lt 25; $reloadTick++) {
   $oldAudit=Get-CimInstance Win32_Process -Filter "ProcessId=$($auditors[0].ProcessId)" -ErrorAction SilentlyContinue
   if (-not $oldAudit) { break }
   Start-Sleep -Seconds 1
  }
  if ($oldAudit) { throw 'The old read-only inspector is still running' }
  @'
import json,sqlite3,sys
from datetime import datetime
from pathlib import Path
from photo_auditor.images import digest,image_bytes
from photo_auditor.store import AuditStore
from listener.pending import write_json
root=Path.cwd();store=AuditStore(root/'runs/photo-auditor')
folder=root/'runs/2026-10-27/audit-reloads'/datetime.now().strftime('%H%M%S-%f')
folder.mkdir(parents=True)
with sqlite3.connect(store.path) as db, sqlite3.connect(folder/'audit.sqlite3') as saved:
 db.row_factory=sqlite3.Row;db.backup(saved)
 rows=[dict(r) for r in db.execute("SELECT id,product_id,sha256,lease FROM audits WHERE status='checking'")]
 for row in rows:
  if digest(image_bytes((store.root/row['id']/'generated.bin').read_bytes()))!=row['sha256']:
   raise RuntimeError('Changed snapshot; preserve its inspection')
  db.execute("UPDATE audits SET status='queued',lease=NULL,tries=max(0,tries-1),retry_at=0 WHERE id=? AND status='checking' AND sha256=? AND lease=?",(row['id'],row['sha256'],row['lease']))
rechecked=[]
for audit_id in sys.argv[1:]:
 if not store.recheck(audit_id):
  raise RuntimeError('The requested saved audit cannot be rechecked')
 store.consumer_waiting(audit_id)
 rechecked.append(audit_id)
write_json(folder/'result.json',{'released':rows,'rechecked':rechecked,'image_generations_interrupted':False})
print(json.dumps({'released_read_only_inspections':[r['product_id'] for r in rows],'same_audits_rechecked':rechecked,'image_generations_interrupted':False}))
'@ | python - @RecheckIds
  if ($LASTEXITCODE -ne 0) { throw 'Read-only inspection reload did not complete' }
 }
 finally { Start-ScheduledTask -TaskName 'AgentsMY Photo Auditor' }
}
finally { Pop-Location }
