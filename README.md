# Endscope (sic)

Desktop viewer for the Vitcoco Wi-Fi endoscope / ear-cleaning camera, replacing the `endscopetool` Android app.

## Usage

1. Turn on the device and connect to its `softish-XXXX` Wi-Fi network.
1. Run the viewer:

```sh
nix run
# or directly:
python endscopetool.py
```

Click either window (or press `h`) to toggle the on-screen help overlay.

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| `1` / `2` / `3` / `4` | Lock rotation to 0° / 90° / 180° / 270° |
| `r` | Unlock rotation (use camera's gravity sensor) |
| `+` / `-` | Increase / decrease LED brightness by 10% |
| `f` | Toggle full frame / circular crop |
| `w` | Save a snapshot (`snapshot_YYYYMMDD_HHMMSS_CC.jpg`) |
| `d` | Toggle debug logging |
| `h` / click | Toggle the help window |
| `q` / `Esc` | Quit |

## Command-line options

- `--debug`: log timeouts, retries, dropped incomplete frames, and frame stalls.
- `--trace`: log every UDP packet header and per-frame timing.
- `--fake`: run against a built-in simulated camera instead of real hardware.
  - `--fake-loss P`: drop each datagram with probability `P` (`0`–`1`) in each direction.
  - `--fake-latency MS`: delay each datagram by `MS` milliseconds.
  - `--fake-jitter MS`: add up to `MS` milliseconds of random extra delay.
- `--headless`: run without creating OpenCV windows (exits non-zero if no frames are rendered).
- `--duration SEC`: exit cleanly after `SEC` seconds.

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

## Credits & License

Licensed under [MPL-2.0](LICENSE).

Based on the [original script by Raphael Wimmer](https://gist.github.com/RaphaelWimmer/5bcb286414e6cd38ed38724f9a6a6129) (CC0 / Public Domain, reverse-engineered from a packet capture), with contributions from [Aghei2](https://github.com/Aghei2), [jamaggs](https://gist.github.com/jamaggs), and [berkakinci](https://github.com/berkakinci).
