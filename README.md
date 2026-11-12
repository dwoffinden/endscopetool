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
| `p` | Toggle partial-frame decoding |
| `w` | Save a snapshot (`snapshot_YYYYMMDD_HHMMSS_CC.jpg`) |
| `d` | Toggle debug logging |
| `h` / click | Toggle the help window |
| `q` / `Esc` | Quit |

## Command-line options

- `--debug`: log timeouts, retries, dropped incomplete frames, and frame stalls.
- `--trace`: log every UDP packet header and per-frame timing.
- `--min-completeness [RATIO]`: render incomplete frames whose contiguous prefix from part 0 is at least `RATIO` of the frame (`0`–`1`, `0.75` if given without a value), compositing the decoded top rows over the previous frame.
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

```sh
nmcli connection modify softish-23840 \
  connection.autoconnect no \
  ipv4.never-default yes \
  ipv4.ignore-auto-dns yes \
  ipv4.route-table 100 \
  ipv4.routing-rules "priority 100 ipproto 17 dport 61502-61503 table 100, priority 32768 to 192.168.1.0/24 table 100" \
  ipv6.method disabled

nmcli connection up softish-23840
```

This isolates the connection in its own routing table:

- Priority `100` (evaluated before the `main` table at `32766`) routes the endoscope's UDP ports (`61502`–`61503`) to table `100`, so it works even if your wired router also uses `192.168.1.1`.
- Priority `32768` (evaluated after `main`) acts as a fallback when you have no wired connection and _only_ `softish` Wi-Fi is connected.

To revert the connection back to NetworkManager defaults:

```sh
nmcli connection modify softish-23840 \
  connection.autoconnect yes \
  ipv4.never-default no \
  ipv4.ignore-auto-dns no \
  ipv4.route-table 0 \
  ipv4.routing-rules "" \
  ipv6.method auto
```

## Credits & License

Licensed under [MPL-2.0](LICENSE).

Based on the [original script by Raphael Wimmer](https://gist.github.com/RaphaelWimmer/5bcb286414e6cd38ed38724f9a6a6129) (CC0 / Public Domain, reverse-engineered from a packet capture), with contributions from [Aghei2](https://github.com/Aghei2), [jamaggs](https://gist.github.com/jamaggs), and [berkakinci](https://github.com/berkakinci).
