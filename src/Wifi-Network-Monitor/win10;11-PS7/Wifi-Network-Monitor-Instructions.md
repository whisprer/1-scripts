[Wifi-Network-Monitor-Instruction.md]


## 0. You should have 3 files incluing this - the other two are:

- WiFi-Network-Monitor.ps1
- Install-WiFi-Network-Monitor.ps1

Put both files in the same folder. Then open PowerShell 7 as Administrator.

Before creating the baseline, wake your phone, NAS, and servers so the scanner can see them.


## 1. Unblock the scripts
cd "C:\path\containing\the\scripts"

Unblock-File .\WiFi-Network-Monitor.ps1
Unblock-File .\Install-WiFi-Network-Monitor.ps1


## 2. Create a candidate baseline
.\WiFi-Network-Monitor.ps1 -CreateCandidateBaseline

It scans only 192.168.1.0/24 through the physical WiFi interface. ProtonVPN and WSL addresses are excluded.

Inspect the result:

notepad.exe "$env:ProgramData\WoflNet\WiFiMonitor\candidate-baseline.json"

Check that every listed IP, MAC and manufacturer is plausible. If a known device was missing, wake/reconnect it and rerun the candidate scan.


## 3. Approve the baseline

Only after inspecting it:

.\WiFi-Network-Monitor.ps1 -ApproveCandidateBaseline


## 4. Install the hourly task
.\Install-WiFi-Network-Monitor.ps1

The monitor then runs hourly as SYSTEM, including when you are not logged in.

### Logs

- Suspicious entries:

Get-Content "$env:ProgramData\WoflNet\WiFiMonitor\suspicious-devices.log" -Tail 50

- Ordinary scan history:

Get-Content "$env:ProgramData\WoflNet\WiFiMonitor\scan-history.log" -Tail 50

The suspicious log is only created after the first alert. It reports:

- An entirely new/unapproved MAC address as WARNING.
- An approved IP appearing with a different MAC as CRITICAL.
- IP address, MAC address, vendor, timestamp and reason.

Test the installed task manually:

Start-ScheduledTask -TaskName "WoflNet WiFi Security Monitor"

Start-Sleep -Seconds 30

Get-ScheduledTaskInfo -TaskName "WoflNet WiFi Security Monitor"

One limitation: sleeping phones often ignore discovery traffic. If your phone wasn’t awake during baseline creation, it may correctly appear as a new device when it wakes later; inspect it, then deliberately rebuild and reapprove the baseline.


---
