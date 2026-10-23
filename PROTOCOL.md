# Canon Image Transfer Utility 2 — Auto-Send Protocol

**Status:** derived from black-box packet captures and interoperability testing with a
Canon EOS 250D, plus a working clean-room implementation (`itu2_*.py`). Everything marked
*observed* is directly evidenced; *inferred* means a reasonable but unproven
interpretation.

**Scope:** camera → computer "Auto send images to computer" only. The PC → Canon cloud
("Web service"/DRS) upload feature is a separate HTTP protocol and is **not** covered.

---

## 0. Terms

| Term | Meaning |
|------|---------|
| PC / host | the computer running ITU2 (or this emulator) |
| camera | Canon body with "Auto send images to computer" |
| GUID | the PC's 128-bit identity (`AppGuid`), persisted by ITU2 |
| PCName | the PC's display name, default `Downloader ####` |
| Initiator | PTP/IP client — **the PC** |
| Responder | PTP/IP server — **the camera**, TCP **15740** |
| Transfer mode | camera ready to send (`X_vendVer` mode `01`) |
| Pairing mode | camera waiting for the PC to finish pairing (mode `00`) |

---

## 1. Roles and topology

```
        SSDP/UPnP (UDP 1900) + HTTP description
   ┌───────────────────────────────┐
   │                               ▼
┌──────┐        TCP 15740        ┌────────┐
│  PC  │ ◄──── PTP/IP (initiator)│ camera │
└──────┘                         └────────┘
```

- Both sides advertise the **same** UPnP device/service type
  `ICPO-AutoSendImages`.
- The **camera is the PTP/IP server**. The PC opens the command socket and a second
  event socket to `camera:15740`.
- Discovery is **SSDP/UPnP only** — there is no mDNS in this feature.

---

## 2. Identity and local state

Windows registry / user settings:

| Item | Where | Notes |
|------|-------|-------|
| `AppGuid` | app settings | the PC GUID; used in UPnP and PTP/IP |
| `PCName` | app settings | default `Downloader ####` (this implementation) |
| `AliasID`, `AccessID` | app settings, DES-encrypted with key `ImgTrans` | **cloud** feature only |
| `Password` | app settings, plaintext | **cloud** feature only |
| save-folder rules | `SaveFolderRule`, `SaveFolderSeparatorType`, `SaveFolderDateOrderType`, `SaveFolderDateFormatType` | |
| upload filters | `isUploadJPEG`, `isUploadResizeJPEG`, `isUploadRAW`, `isUploadMovie`, `isRatingState0..5`, `UploadLimit*` | |
| misc | `SOFTWARE\Canon_Inc_IC\ImageTransferUtility2` (`SelectedCulture`), `...\CCLInfo` (`Country`), `HKCU\...\CameraSurveyProgram` (`UUID`) | |

For a clean-room emulator, only `GUID` and `PCName` are required. Generate a UUIDv4 once,
persist it, and use it verbatim.

---

## 3. Discovery (SSDP / UPnP)

### 3.1 Fixed identifiers

| Constant | Value |
|----------|-------|
| Device type | `urn:schemas-canon-com:device:ICPO-AutoSendImages:1` |
| Service type | `urn:schemas-canon-com:service:ICPO-AutoSendImages:1` |
| Service ID | `urn:schemas-canon-com:serviceId:ICPO-AutoSendImages-1` |
| Vendor namespace | `urn:schemas-canon-com:schema-upnp` |
| SCPD (PC) | `ImageSyncSvcDesc2.xml` (official; our impl uses `/scpd.xml`) |
| SCPD (camera) | `CameraSvcDesc.xml` |
| SSDP address | `239.255.255.250:1900` |

### 3.2 PC advertisement

The PC must advertise a UPnP **device** whose UDN/USN is `uuid:<GUID>`.

Device description served over HTTP (UTF-16 or UTF-8; the official one is UTF-16):

```xml
<?xml version="1.0" encoding="utf-16"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-canon-com:device:ICPO-AutoSendImages:1</deviceType>
    <friendlyName>{PCName}</friendlyName>
    <manufacturer>Canon</manufacturer>
    <manufacturerURL>http://www.canon.com</manufacturerURL>
    <modelDescription>Canon Digital Camera</modelDescription>
    <modelName>{PCName}</modelName>
    <serialNumber/>
    <UDN>uuid:{GUID}</UDN>
    <serviceList><service>
      <serviceType>urn:schemas-canon-com:service:ICPO-AutoSendImages:1</serviceType>
      <serviceId>urn:schemas-canon-com:serviceId:ICPO-AutoSendImages-1</serviceId>
      <SCPDURL>/scpd.xml</SCPDURL>
      <controlURL>/control</controlURL>
      <eventSubURL/>
      <X_deviceUsbId xmlns="urn:schemas-canon-com:schema-upnp"/>
      <X_deviceNickname xmlns="urn:schemas-canon-com:schema-upnp"/>
      <ns:X_vendVer xmlns:ns="urn:schemas-canon-com:schema-upnp">0-1801-0101-01-00-00</ns:X_vendVer>
      <ns:X_targetId xmlns:ns="urn:schemas-canon-com:schema-upnp"/>
    </service></serviceList>
    <presentationURL/>
  </device>
</root>
```

SCPD can be minimal (official Windows one only declares `X_ItuNullVariable`):

```xml
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <serviceStateTable>
    <stateVariable sendEvents="no">
      <name>X_ItuNullVariable</name><dataType>string</dataType>
    </stateVariable>
  </serviceStateTable>
</scpd>
```

**Critical:** the GUID must appear as the **UDN and SSDP USN**, because that is how the
camera learns the PC identity it later stores as `X_targetId`.

### 3.3 SSDP messages

PC → multicast, `ssdp:alive` (repeat periodically, e.g. every 15 s):

```
NOTIFY * HTTP/1.1
HOST: 239.255.255.250:1900
NT: <target>
NTS: ssdp:alive
USN: uuid:<GUID>::<target>
Location: http://<pc-ip>:<port>/desc.xml
Cache-Control: max-age=1800
Server: Microsoft-Windows/10.0 UPnP/1.0 UPnP-Device-Host/1.0
```

Targets to advertise: `upnp:rootdevice`, `uuid:<GUID>`,
`urn:schemas-canon-com:device:ICPO-AutoSendImages:1`,
`urn:schemas-canon-com:service:ICPO-AutoSendImages:1`.

PC → multicast search (official uses the **service** type):

```
M-SEARCH * HTTP/1.1
HOST: 239.255.255.250:1900
MAN: "ssdp:discover"
MX: 3
ST: urn:schemas-canon-com:service:ICPO-AutoSendImages:1
```

Camera → multicast search (uses the **device** type):

```
M-SEARCH * HTTP/1.1
HOST: 239.255.255.250:1900
MAN: "ssdp:discover"
MX: 2
ST: urn:schemas-canon-com:device:ICPO-AutoSendImages:1
```

Response (unicast, from source port 1900):

```
HTTP/1.1 200 OK
ST: <requested ST>
USN: uuid:<GUID>::<requested ST>
Location: http://<pc-ip>:<port>/desc.xml
Cache-Control: max-age=1800
Server: ...
Ext:
Date: ...
```

The camera also sends `NOTIFY ssdp:alive` when it enters the network (its own device),
with `Server: Camera OS/1.0 UPnP/1.0 Canon Device Discovery/1.0` and
`Location: http://<camera-ip>:49152/upnp/CameraDevDesc.xml`.

### 3.4 Camera device description

Observed (EOS 250D):

```xml
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>http://192.0.2.20:49152/upnp/</URLBase>
  <device>
    <deviceType>urn:schemas-canon-com:device:ICPO-AutoSendImages:1</deviceType>
    <friendlyName>Canon EOS 250D</friendlyName>
    <manufacturer>Canon</manufacturer>
    <manufacturerURL>http://www.canon.com/</manufacturerURL>
    <modelDescription>Canon Digital Camera</modelDescription>
    <modelName>Canon EOS 250D</modelName>
    <serialNumber>000000000000</serialNumber>
    <UDN>uuid:00000000-0000-0000-0001-XXXXXXXXXXXX</UDN>
    <serviceList><service>
      <serviceType>urn:schemas-canon-com:service:ICPO-AutoSendImages:1</serviceType>
      <serviceId>urn:schemas-canon-com:serviceId:ICPO-AutoSendImages-1</serviceId>
      <SCPDURL>CameraSvcDesc.xml</SCPDURL>
      <controlURL>control/CanonCamera/</controlURL>
      <eventSubURL/>
      <ns:X_targetId xmlns:ns="urn:schemas-canon-com:schema-upnp">uuid:{PC-GUID}</ns:X_targetId>
      <ns:X_vendVer xmlns:ns="urn:schemas-canon-com:schema-upnp">0-1801-32e9-00-00-00</ns:X_vendVer>
      <ns:X_deviceUsbId xmlns:ns="urn:schemas-canon-com:schema-upnp">32e9</ns:X_deviceUsbId>
      <ns:X_deviceNickname xmlns:ns="urn:schemas-canon-com:schema-upnp">EOS250D_XXXXXX</ns:X_deviceNickname>
    </service></serviceList>
    <presentationURL>/</presentationURL>
  </device>
</root>
```

Key fields:

| Field | Meaning |
|-------|---------|
| `URLBase` | `http://<camera-ip>:49152/upnp/` — **the camera IP comes from here** |
| `serialNumber` | body serial |
| `X_targetId` | the GUID of the computer this camera is paired with |
| `X_vendVer` | `0-1801-<usbId>-<mode>-00-00`; **mode `00` = pairing, `01` = transfer** |
| `X_deviceNickname` | camera nickname; escapes `~L ~G ~A ~Q ~S` = `< > & " '` |

The PC ignores any advert whose `X_targetId` is not its own GUID (except during first
pairing — see §3.5).

### 3.5 Pairing flow

1. PC advertises (GUID as UDN/USN, `PCName`).
2. Camera discovers the PC (its own M-SEARCH + the PC's NOTIFY/response), fetches the
   PC's description, and shows it in the camera's computer list.
3. User selects the computer on the camera. The camera stores that GUID as
   `X_targetId` and switches to **pairing mode** (`X_vendVer` mode `00`), showing
   "Use Canon app/software to finish the pairing".
4. PC sees the camera (TargetId == its GUID, mode `00`), opens PTP/IP to
   `camera:15740` with its GUID and `PCName`, and runs the pairing session (§6.1).
5. Camera shows **"Settings complete"** and persists the pairing.
6. **Reboot the camera.** On boot it enters **transfer mode** (`X_vendVer` mode `01`)
   and is ready to send.

No cryptographic material was observed: pairing is effectively "camera accepts the
PTP/IP client whose GUID it stored".

---

## 4. PTP/IP transport

### 4.1 Framing

Every packet:

```
uint32 length      // total including this 8-byte header
uint32 type
uint8  body[length-8]
```

Little-endian.

### 4.2 Packet types

| Type | Name | Body |
|------|------|------|
| 1 | Init Command Request | `GUID[16]`, name UTF-16LE NUL-terminated, `uint32 version` |
| 2 | Init Command Ack | `uint32 connectionNumber`, `GUID[16]`, name UTF-16LE, `uint32 version` |
| 3 | Init Event Request | `uint32 connectionNumber` |
| 4 | Init Event Ack | — |
| 5 | Init Fail | `uint32 reason` |
| 6 | Operation Request | `uint32 dataPhase`, `uint16 opcode`, `uint32 transactionId`, `uint32 params[]` |
| 7 | Operation Response | `uint16 responseCode`, `uint32 transactionId`, `uint32 params[]` |
| 8 | Event | `uint16 eventCode`, `uint32 transactionId`, `uint32 params[]` |
| 9 | Start Data | `uint32 transactionId`, `uint64 totalLength` |
| 10 | Data | `uint32 transactionId`, bytes |
| 11 | End Data | `uint32 transactionId`, bytes |
| 12 | Cancel | `uint32 transactionId` |

**Canon quirk (important):** in data-in phases the final chunk arrives as type **12**,
not type 11. Treat types 10, 11 and 12 as data carriers whose body starts with the
transaction id, and accumulate until `totalLength`.

Version bytes observed: `00 00 01 00` (interpreted as 1.0).

### 4.3 Connection setup

```
TCP connect camera:15740                       (command socket)
→ Init Command Request  guid=<PC GUID> name=<PCName> ver=1.0
← Init Command Ack      connection=1, camera GUID/name
TCP connect camera:15740                       (event socket)
→ Init Event Request    connection=1
← Init Event Ack
→ OpenSession(0x1002, session=0x41)
← OK
```

The event socket is opened but Canon reports events by **polling** `0x9116` on the
command channel; no traffic was observed on the event socket after the handshake.

The camera enforces a **single client**: close a session before opening another.

### 4.4 Response codes

| Code | Meaning |
|------|---------|
| `0x2001` | OK |
| `0x2003` | General error (also returned by `0x9116` after a session is closed) |

---

## 5. Canon vendor operations

### 5.1 Opcode reference (observed)

| Opcode | Direction | Params | Response / data | Meaning (inferred where marked) |
|--------|-----------|--------|-----------------|----------------------------------|
| `0x1001` | PC→cam | — | 591-byte DeviceInfo | standard PTP GetDeviceInfo |
| `0x1002` | PC→cam | `[0x41]` | OK | OpenSession (session id 65) |
| `0x1003` | PC→cam | — | OK | CloseSession |
| `0x9101` | PC→cam | — | 8 bytes | *unknown*; status/selection |
| `0x9102` | PC→cam | `[0x00020001]` | 36 bytes | *unknown*; property/status read |
| `0x9114` | PC→cam | `[0x41]` / `[0]` | OK | enable/disable remote-ish mode |
| `0x9115` | PC→cam | `[1]` / `[0]` | OK | SetEventMode on/off |
| `0x9116` | PC→cam | — | event table | **GetEvent** (see §5.2) |
| `0x9127` | PC→cam | `[propId 0xD1xx]` | data | **GetDevicePropValue** |
| `0x913D` | PC→cam | `[0xFFF]` | OK | *unknown* |
| `0x913F` | PC→cam | `[0x01000000, 0x110, 0x31000000]` | params `[0x80000436, 1]` | camera-support/capability probe |
| `0x916D` | PC→cam | `[0]` | OK | auto-send session control / finish |
| `0x9170` | PC→cam | `[handle]` | 150–158 bytes | **object/dir info** |
| `0x9173` | PC→cam | `[0x00020001, offset/format, 0x200000]` | OK | transfer window/format control |
| `0x9174` | PC→cam | `[handle, offset, length, 0, 0]` | data + params `[transferred, remaining, 0]` | **GetPartialObject** |
| `0x9177` | PC→cam | `[handle, 0]` | OK | object finished |
| `0x91E9` | PC→cam | `[handle, percent]` | OK | transfer progress |
| `0x91F0` | PC→cam | `[handle]` | OK | object aux |

Handles look like `0x9190xxxx` (e.g. `0x91903B82`). Storage/dir property IDs look like
`0xD1xx`.

### 5.2 `0x9116` GetEvent payload

A sequence of records:

```
repeat:
  uint32 length        // total record size, including these 8 bytes
  uint32 kind
  uint8  body[length-8]
```

Observed kinds:

| kind | count (one sample) | body |
|------|--------------------|------|
| `0x0000C189` | 133 | first `uint32` = Canon property ID `0xD1xx`, then value (4 bytes or UTF‑16/ASCII) |
| `0x0000C18A` | 116 | misc/status |
| `0x0000C1A4` | 1 | storage id (`0x00020001`) |
| `0x0000C19D` | 1 | misc |
| `0x0000C1B1` | 1 | misc |
| `0x0000C1AB` | 1 per new object | **new-object notification** (see below) |

Object notification (`kind = 0xC1AB`) body:

```
+0   uint32 handle          e.g. 0x91903B82
+4   uint32 0x00003801      (observed constant/flags)
+8   uint32 0x00000020      (observed)
+12  uint32 size            total object size in bytes
+16  uint32 0x00000000
+20  uint32 thumbnailHandle e.g. 0x91903B80
+24  char[] path            NUL-terminated ASCII, e.g. "B:/DCIM/100CANON/IMG_0952.JPG"
```

Verified against the capture: `handle=0x91903B82`, `size=3832873`,
`path=B:/DCIM/100CANON/IMG_0952.JPG`.

The first `0x9116` after setup returns the **full current table** (including pending
objects). Later polls are small deltas (usually 8 bytes = empty). New photos appear as
new `0xC1AB` records.

### 5.3 Device properties (`0x9127`)

`0xD1xx` are Canon EOS device-property IDs. Examples decoded:

| Prop ID | Value seen |
|---------|-----------|
| `0xD1AF` | serial `000000000000` |
| `0xD1A6` | `DR-E18` (battery) |
| `0xD169`,`0xD16A`,`0xD16B` | small status values |

Additional standard PTP properties used by the reference implementation:
`0x01000001` ModelID, `2` ProductName, `7` FirmVersion, `16` BatteryQuality,
`21` BodyIDEx, `101` UUID, `1037` LensName, `0x01000023` BatteryName, etc.

### 5.4 Object info (`0x9170`)

Returns 150 bytes for a storage/root handle and 158 bytes for a file handle. It contains
UTF‑16 folder names (`100CANON`) and timestamps (`20211008T185511.0`). The exact layout is
not needed: the **path and size come from the `0xC1AB` event**, so the implementation can
treat `0x9170` as an optional info step.

### 5.5 Object read (`0x9174`)

Request: `[handle, offset, length, 0, 0]`.
Response: data phase, then `OpResp` params `[transferred, remaining, 0]`.

Observed for a 3,832,873-byte object:

```
0x9174 [h, 0,        0x200000, 0, 0] → 2,097,152 bytes, params [0x200000, 0x1A7DA9, 0]
0x9174 [h, 0x200000, 0x1A7DA9, 0, 0] → 1,735,721 bytes, params [0x1A7DA9, 0, 0]
```

`0x200000` (2 MB) is a safe chunk size. Use `remaining` (params[1]) or the event size to
drive the loop.

---

## 6. End-to-end sequences

### 6.1 Pairing session

```
InitCmdReq / InitCmdAck
InitEventReq / InitEventAck
OpenSession(0x1002, 0x41)
0x9114 [0x41]          # enable
0x9115 [1]             # event mode
0x9116                 # initial table
0x9170 [0x91900000]    # root/storage info
0x9170 [0x91900000]
0x9102 [0x00020001]
0x913D [0xFFF]
0x9101
0x9102 [0x00020001]
0x916D [0]             # finish pairing
0x9115 [0]
0x9114 [0]
0x1003                 # close
```

The camera then displays "Settings complete". **Reboot the camera** to switch to
transfer mode.

### 6.2 Transfer session

```
InitCmdReq / InitCmdAck
InitEventReq / InitEventAck
OpenSession(0x1002, 0x41)
0x9114 [0x41]
0x9115 [1]
0x9116                 # ← initial table: contains pending 0xC1AB objects
0x9170 [0x91900000]
0x9170 [0x91900000]
0x9102 [0x00020001]
0x913D [0xFFF]
0x9101
0x9102 [0x00020001]

for each pending object (handle, size, path) from 0x9116:
    0x9170 [handle]
    0x913F [0x01000000, 0x110, 0x31000000]
    0x9101
    0x9174 [handle, 0, min(size,0x200000), 0, 0]
    0x9102 [0x00020001]
    while remaining > 0:
        0x9174 [handle, offset, min(remaining,0x200000), 0, 0]
        0x9102 [0x00020001]
    0x91F0 [handle]
    0x91E9 [handle, 100]
    0x9177 [handle, 0]

poll 0x9116 every ~1–2 s for new 0xC1AB objects
when done:
    0x916D [0]
    0x9115 [0]
    0x9114 [0]
    0x1003
```

The camera keeps the files; auto-send does not delete them.

---

## 7. Implementation notes (lessons from the live implementation)

### 7.1 Camera Wi‑Fi lifecycle (the big gotcha)

- The camera only keeps Wi‑Fi/services active while it is **in auto-send context** (the
  auto-send screen / actively sending), and while the paired computer is advertising.
- After pairing completes, the camera drops off the network until **rebooted**.
- It may come up for **brief windows** (seconds). Be ready to react immediately.
- If nothing completes, it turns Wi‑Fi off ("Off" icon).
- Keep the UPnP advertiser running **during** the PTP/IP session; stopping it can make
  the camera drop.

### 7.2 Detection

- Listen for **SSDP NOTIFY** (multicast) as well as M‑SEARCH replies. The camera's
  `ssdp:alive` is the reliable signal; it may not answer our M‑SEARCH.
- Also actively poll a known camera IP (`http://<ip>:49152/upnp/CameraDevDesc.xml`) every
  ~1.5 s when the camera's SSDP window is short.
- **Ignore your own advert** (UDN == your GUID) or you will try to connect to yourself.
- Parse namespaced tags (`ns:X_targetId`, `ns:X_vendVer`, `ns:X_deviceNickname`).
- Normalize GUIDs (strip `uuid:`, braces, lowercase) before comparing.
- `X_vendVer` parse: split on `-`, require 6 fields; field[3] `00` = pairing,
  `01` = transfer.

### 7.3 Identity

- Use the GUID as **UDN and USN**, not just as the PTP/IP client GUID.
- `InitCmdReq` name = `PCName`, encoded UTF‑16LE **with NUL terminator**.
- GUID → 16 raw bytes (strip hyphens/braces).

### 7.4 PTP/IP

- Always open **two** TCP connections (command + event) and send `InitEventReq`; some
  cameras require the event channel even though events are polled.
- Increment the transaction id per request; responses echo it.
- `OpenSession` uses session id `0x41` in the observed implementation.
- Data-in phases: read `StartData.totalLength`, then accumulate packets whose body starts
  with the transaction id. **Canon sends the final chunk as type 12, not 11.**
- Set generous socket timeouts (**≥120 s**). A 2 MB chunk took 30–90 s over the camera's
  Wi‑Fi; a short timeout will wedge the transfer and the camera will drop.
- After `CloseSession`, `0x9116` returns `0x2003`; stop polling.
- Do not run two sessions concurrently (single client).

### 7.5 File handling

- The local filename is the basename of the camera path (`B:/DCIM/100CANON/IMG_1005.JPG`).
- Write to a temp file, then rename (a robust client should do this).
- Auto-send does **not** delete camera files; dedupe locally if desired.
- Apply the save-folder rules / filters described in §2 if matching reference behavior.

### 7.6 Transport/security

- PTP/IP is **plaintext**; no TLS/encryption observed.
- Pairing trust is the stored GUID. There is no challenge/response.
- Treat the network as untrusted anyway: bound lengths, validate handles/paths.

---

## 8. Reference sequences and artifacts

- Working implementation:
  - `itu2_discovery.py` — SSDP/UPnP advertise/search + HTTP description
  - `itu2_ptpip.py` — PTP/IP initiator, event parser, object read
  - `itu2_pair.py` — mode-aware pair/transfer runner
- Capture and decoded payloads: `recon/captures/` (`README.md`, `carved/`).

---

## 9. Unknowns / future work

- Exact semantics of `0x9101`, `0x9102`, `0x9114` param `0x41`, `0x913D`, `0x913F`,
  `0x916D`, `0x9173`, `0x91F0` (all return OK; some return small datasets).
- Meaning of event kinds `0xC18A`, `0xC1A4`, `0xC19D`, `0xC1B1`.
- Full layout of the 150/158-byte `0x9170` record.
- Whether the event socket ever carries data (Canon appears to poll).
- Optimal chunk size / throughput; whether transfers can be resumed.
- RAW/CR3 and movie handling, multi-object ordering, multiple cameras.
- Behavior on other bodies/firmware (only EOS 250D verified).
- The PC → Canon cloud ("DRS") HTTP upload is separate and out of scope.
