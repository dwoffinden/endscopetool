# Endscope (sic)

## Optional setup

If your computer has multiple network interfaces (e.g. Wi-Fi _and_ ethernet),
it's possible to connect to the device's network without losing internet access.
After connecting at least once, configure NetworkManager:

```
nmcli connection modify softish-23840 \
  connection.autoconnect no \
  ipv4.never-default yes \
  ipv4.ignore-auto-dns yes \
  ipv4.route-table 100 \
  ipv4.routing-rules "priority 100 ipproto 17 dport 61502-61503 table 100, priority 101 ipproto 17 sport 61502-61503 table 100" \
  ipv6.method disabled

nmcli connection up softish-23840
```

This isolates the connection in its own routing table, and routes only the
required UDP ports so that it works even if your router also uses 192.168.1.1
