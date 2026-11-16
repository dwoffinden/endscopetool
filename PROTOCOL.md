# Wire Protocol

`endscopetool` implements the UDP protocol spoken by `softish-XXXX` (`protocol=2`) Wi-Fi endoscope / otoscope cameras (sold under brands such as Vitcoco, Kekoy, Frohsen, Wohlmen, etc.) and their white-label Android companion app (`EndscopeTool`, `com.atomath.camera.neutral`).

The camera acts as an open IPv4 Wi-Fi access point at `192.168.1.1`.

## Supported protocol (`softish-XXXX`, `protocol=2`)

### Control & telemetry (`192.168.1.1:61502` UDP)

Commands are LF-terminated ASCII strings; replies are single-line URL query strings (some firmwares send a fixed-size C buffer with uninitialised bytes / `0xaa` padding after the `\n`):

| Command | Reply | Notes |
| --- | --- | --- |
| `type=1001\n` | `type=2001&data=<int>\n` | Battery status (`0`–`100` = charge %, `101` / `>100` = charging). Polling periodically also acts as a keepalive. |
| `type=1002\n` | `type=2002&...\n` | Device metadata (see fields below). |
| `type=1003&value=<0..100>\n` | `type=1003&value=<int>\n` | Sets LED brightness (`0`–`100`). |
| `type=1004\n` | Binary `LOGO` chunks | Optional: when `type=2002` has `logo=1`, streams a custom OEM brand image from camera flash (`reply[0:4] == b"LOGO"`, `reply[4]` = total chunks, `reply[5]` = 1-based chunk index, `reply[8:]` = image data). Not used by `endscopetool`. |

Example `type=2002` fields:

- `protocol`: protocol version (`2` for this framing; `1` on older models).
- `angle`: static CMOS sensor mounting angle in degrees (e.g. `270` or `90`).
- `w`, `h`, `fps`: nominal stream dimensions and frame rate (e.g. `640`×`480` @ `20` fps or `1280`×`720` @ `15` fps).
- `company`, `dn`, `hardware`, `firmware`, `ssid`, `logo`: OEM brand, model (`Y8`, `CY*`), hardware/firmware revisions, AP SSID, and flash-logo flag (`0` or `1`).

### Video stream (`192.168.1.1:61503` UDP)

- **Start stream**: `\x20\x36\x00\x02` (`0x20 0x36` followed by big-endian `uint16` protocol version `2`).
- **Stop stream**: `\x20\x37`.

Each incoming video UDP datagram consists of an **8-byte header** followed by a slice of a baseline sequential JPEG frame (no restart markers):

| Byte(s) | Type | Field | Description |
| --- | --- | --- | --- |
| `0` | `uint8` | `frame_id` | Rolling 8-bit frame counter (`0`–`255`). |
| `1` | `uint8` | `frame_end` | `1` on the final chunk of a frame, `0` on earlier chunks. |
| `2` | `uint8` | `part` | `0`-based chunk index (`0 .. part_end - 1`). |
| `3` | `uint8` | `part_end` | Total chunks in this frame (populated when `frame_end == 1`). |
| `4..5` | `int16_be` | `sensor_angle` | Signed big-endian onboard gravity-sensor roll angle in degrees. Rotate clockwise by `angle - sensor_angle` (or counter-clockwise by `sensor_angle - angle`) to keep the horizon level. |
| `6..7` | `2 bytes` | *(unused)* | `0x00 0x00` (ignored). |
| `8..` | `bytes` | `pic_data` | Raw JPEG slice. |

## Other variants in the wild (not implemented here)

The official white-label `com.atomath.camera.neutral` app (and third-party open-source Android viewers such as [rbeilvert/otoscope](https://github.com/rbeilvert/otoscope)) also speak other protocols for different Wi-Fi camera chipsets:

- **Legacy `softish` `protocol=1` (`192.168.1.1:61502` & `61503`)**: Started with 2-byte `\x20\x36` (no protocol suffix). In the 8-byte video header, byte `1` carries the *previous* frame's chunk count instead of `frame_end`, and byte `3` is unused.
- **`"TX"` / EarVision / JEGOAT family (`192.168.1.1:61500` & `61501`)**:
  - **Control (`61500` UDP)**: Binary `\x10\x01` poll returns UTF-8 JSON (`{"ssid": ..., "version": ..., "battery_charging": ..., "battery_percentage": ...}`); `\x10\x02\x00<level>` sets LED brightness (`0`–`100`).
  - **Video (`61501` UDP)**: `\x20\x01` starts and `\x20\x02` stops streaming. Video packets have a **24-byte header** (`uint32_le` frame ID at `0..3`, `uint8` total chunks at `10`, `uint8` chunk index at `11`, `int16_be` gravity roll angle at `12..13`, JPEG slice at `24..`).
- **Other chipset families** (documented in [rbeilvert/otoscope](https://github.com/rbeilvert/otoscope)): UDP ports `10005`/`10006` (i4season / Soulear / Beken / inskam) and UDP `8031`/`8032` (Wifiview).
