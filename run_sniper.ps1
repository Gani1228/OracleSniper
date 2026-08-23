# Supervisor: keeps Sniper.py running until it wins an instance.
# Restarts on silent exits, but gives up if the script keeps dying instantly
# (that means a config error, not a transient failure).
$dir = "C:\Users\Dell\Documents\GitHub\OracleSniper"
Set-Location $dir

function Write-SupervisorLog($message) {
    "$(Get-Date -f 'yyyy-MM-dd HH:mm:ss') | supervisor: $message" |
        Out-File -FilePath "$dir\supervisor.log" -Append -Encoding utf8
}

# Only ever run one sniper. OCI rate-limits per tenancy, so a second copy
# would throttle the first rather than double the attempt rate.
$existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*Sniper.py*" }
if ($existing) {
    Write-SupervisorLog "a sniper is already running (pid $($existing.ProcessId)), exiting"
    exit 0
}

if (Test-Path "$dir\instance_success.txt") {
    Write-SupervisorLog "instance_success.txt exists, nothing to do"
    exit 0
}

Write-SupervisorLog "starting"
$fastFailures = 0
while (-not (Test-Path "$dir\instance_success.txt")) {
    $started = Get-Date
    python Sniper.py
    if (Test-Path "$dir\instance_success.txt") { break }
    $ranFor = (Get-Date) - $started
    if ($ranFor.TotalSeconds -lt 30) {
        $fastFailures++
        if ($fastFailures -ge 5) {
            Write-SupervisorLog "5 instant exits in a row, giving up (check .env and ~/.oci/config)"
            break
        }
    } else {
        $fastFailures = 0
    }
    Write-SupervisorLog "sniper exited after $([int]$ranFor.TotalSeconds)s, restarting in 60s"
    Start-Sleep -Seconds 60
}
if (Test-Path "$dir\instance_success.txt") { Write-SupervisorLog "instance won, supervisor exiting" }
