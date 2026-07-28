\[Wifi-Network-Monitor-Instructions.md]





\## 0. you should also have the following files: 

Wifi-Network-Monitor.sh





\## 1. Script esplained

the following bits do crucial things all of 'em:



\### Identifying the Devices

MAC addresses aren’t exactly human-friendly. But here’s a trick — the first three pairs of characters in a MAC address are the OUI (Organizationally Unique Identifier). They tell you the manufacturer of the network card.



You can look up any MAC prefix on macvendors.com or use the command line:

`curl -s "https://api.macvendors.com/b4:7c:9c"`



Which might return something like:



`Samsung Electronics Co.,Ltd`



Now that mystery device at 192.168.1.11 is suddenly a lot less mysterious.



The first section of script auto runs that and sticks the info in column 3.


Your real Wi-Fi address will probably resemble 192.168.x.x or 10.x.x.x. Phones may appear in arp.exe -a only after your device has communicated with it; sleeping phones also often disappear from ARP caches.


\### Arp

`arp -a`



ARP stands for Address Resolution Protocol. Your computer constantly uses it to map IP addresses to physical (MAC) addresses on the local network. The -a flag dumps the full ARP cache — basically a list of every device your machine has "seen" recently on the network.



```

$ arp -a

? (192.168.1.1) at c4:e9:84:aa:11:02 \[ether] on wlan0

? (192.168.1.5) at a8:51:ab:cd:22:ff \[ether] on wlan0

? (192.168.1.9) at <incomplete> on wlan0

grep -v "incomplete"

```



Some entries show up as <incomplete> — these are ghost entries for devices that didn't respond. We filter them out with -v (which means exclude).



\### Awk

`awk '{print $2, $4}'`



awk is a text-processing powerhouse. Here we're telling it: from each line, give me only column 2 (the IP) and column 4 (the MAC address). Everything else — the at, the \[ether], the interface name — gets dropped.



\### Wrapping the IP in brackets

`tr -d '()'`



The IP address comes wrapped in parentheses like (192.168.1.1). This strips them out for a clean output.





\### Full next section

```

\#!/bin/bash							<- beware doing the stuff in this script in win - you'll gt CRLF linendings not LF and Linux won't like 								the script - you'll likely see err with `/bin/bash^M`.

echo "================================================"

echo "  Devices currently on your network"

echo "  $(date)"

echo "================================================"

printf "%-18s %-20s\\n" "IP Address" "MAC Address"

echo "------------------------------------------------"

arp -a | grep -v "incomplete" | awk '{print $2, $4}' | tr -d '()' | \\

while read ip mac; do

&#x20; printf "%-18s %-20s\\n" "$ip" "$mac"

done

echo "================================================"

echo "Total: $(arp -a | grep -v incomplete | wc -l) device(s) found"

```



\### Run It Automatically Every Few Minutes

Want to keep a passive eye on your network? Drop this into a cron job:



`crontab -e`

Add this line to run the scan every 5 minutes and log the results:



`\\\*/5 \\\* \\\* \\\* \\\* /path/to/wifi-scan.sh >> /var/log/wifi-monitor.log 2>\\\&1`

Now you’ll have a timestamped record of every device that’s ever shown up on your network.





\## 2. Make it executable and run it:

```

chmod +x wifi-scan.sh

./wifi-scan.sh

```



### Example output:

```

================================================

&#x20; Devices currently on your network

&#x20; Sat May 9 11:42:03 WAT 2026

================================================

IP Address         MAC Address		Vendor ID

\------------------------------------------------

192.168.1.1        c4:e9:84:aa:11:02	MicrosoftCorporation

192.168.1.5        a8:51:ab:cd:22:ff	Samsung Electronics Ltd.

192.168.1.11       b4:7c:9c:de:44:ab	Siemens Nixdorf Ltd.

================================================

Total: 3 device(s) found

```





\## 3. Limitations to Know

A few honest caveats:



* WSL2 is lurking behind a virtual gateway i.e. if you see 172.30.128.1 with a 00:15:5d <your device's> MAC is the WSL/Hyper-V virtual gateway. The script is reading WSL’s virtual ARP table, not your device's physical Wi-Fi network, nothing else will be listed there.
* ARP cache isn’t live. It shows devices your machine has recently communicated with, not necessarily every device connected right now. For a more active scan, look into nmap -sn 192.168.1.0/24 — though that requires installing nmap.
* This only works on your local network. You need to be connected to the same WiFi you’re scanning.
* Devices with randomized MACs (most modern phones do this by default) will appear with a different MAC each session, making them harder to track.
* On macOS, the output of arp -a is slightly different in formatting, but the same one-liner works.





\---



