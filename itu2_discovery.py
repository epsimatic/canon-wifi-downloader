#!/usr/bin/env python3
"""
ITU2 emulator — discovery/advertisement prototype (M1).

Implements the Canon "ICPO-AutoSendImages" UPnP device so a camera in
"Auto send images to computer" mode can discover this host, and searches for the
camera's advert to learn its IP.

Behaviour reconstructed from packet captures; see
PROTOCOL.md and recon/captures/README.md.

Usage:
    python3 itu2_discovery.py [--iface-ip 192.0.2.10] [--http-port 2869]

It prints every SSDP search response / NOTIFY from a Canon device, and every
device description it fetches (with X_targetId / X_vendVer decoded).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.request
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MCAST_ADDR = "239.255.255.250"
SSDP_PORT = 1900
DEVICE_TYPE = "urn:schemas-canon-com:device:ICPO-AutoSendImages:1"
SERVICE_TYPE = "urn:schemas-canon-com:service:ICPO-AutoSendImages:1"
SERVICE_ID = "urn:schemas-canon-com:serviceId:ICPO-AutoSendImages-1"
VENDOR_NS = "urn:schemas-canon-com:schema-upnp"
VENDOR_VER = "0-1801-0101-01-00-00"
SERVER_HDR = "Microsoft-Windows/10.0 UPnP/1.0 UPnP-Device-Host/1.0"

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
DEFAULT_PC_NAME_FMT = "Downloader %04d"


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(st, f, indent=2)


def get_identity(state: dict) -> tuple[str, str]:
    changed = False
    if not state.get("guid"):
        state["guid"] = str(uuidlib.uuid4())
        changed = True
    if not state.get("pc_name"):
        import random

        state["pc_name"] = DEFAULT_PC_NAME_FMT % random.randint(1, 9998)
        changed = True
    if changed:
        save_state(state)
    return state["guid"], state["pc_name"]


def guess_iface_ip() -> str:
    """Pick the local IP that can reach the local /24 (default route iface)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"
    finally:
        s.close()


def device_description(pc_name: str, guid: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-16"?>\n'
        '<root xmlns="urn:schemas-upnp-org:device-1-0">\n'
        "\t<specVersion><major>1</major><minor>0</minor></specVersion>\n"
        "\t<device>\n"
        f"\t\t<deviceType>{DEVICE_TYPE}</deviceType>\n"
        f"\t\t<friendlyName>{html.escape(pc_name)}</friendlyName>\n"
        "\t\t<manufacturer>Canon</manufacturer>\n"
        "\t\t<manufacturerURL>http://www.canon.com</manufacturerURL>\n"
        "\t\t<modelDescription>Canon Digital Camera</modelDescription>\n"
        f"\t\t<modelName>{html.escape(pc_name)}</modelName>\n"
        "\t\t<serialNumber/>\n"
        f"\t\t<UDN>uuid:{guid.upper()}</UDN>\n"
        "\t\t<serviceList><service>\n"
        f"\t\t\t<serviceType>{SERVICE_TYPE}</serviceType>\n"
        f"\t\t\t<serviceId>{SERVICE_ID}</serviceId>\n"
        "\t\t\t<SCPDURL>/scpd.xml</SCPDURL>\n"
        "\t\t\t<controlURL>/control</controlURL>\n"
        "\t\t\t<eventSubURL></eventSubURL>\n"
        f'\t\t\t<X_deviceUsbId xmlns="{VENDOR_NS}"/>\n'
        f'\t\t\t<X_deviceNickname xmlns="{VENDOR_NS}"/>\n'
        f'\t\t\t<ns:X_vendVer xmlns:ns="{VENDOR_NS}">{VENDOR_VER}</ns:X_vendVer>\n'
        f'\t\t\t<ns:X_targetId xmlns:ns="{VENDOR_NS}"/>\n'
        "\t\t</service></serviceList>\n"
        "\t\t<presentationURL/>\n"
        "\t</device>\n"
        "</root>\n"
    ).encode("utf-8")


SCPD = (
    '<?xml version="1.0"?>\n'
    '<scpd xmlns="urn:schemas-upnp-org:service-1-0">\n'
    "  <specVersion><major>1</major><minor>0</minor></specVersion>\n"
    '  <serviceStateTable><stateVariable sendEvents="no">\n'
    "    <name>X_ItuNullVariable</name><dataType>string</dataType>\n"
    "  </stateVariable></serviceStateTable>\n"
    "</scpd>\n"
).encode("utf-8")


class HTTPHandler(BaseHTTPRequestHandler):
    guid = ""
    pc_name = ""
    log = None

    def log_message(self, *a):  # silence default logging
        pass

    def do_GET(self):
        if "desc" in self.path or self.path in ("/", "/rootDesc.xml"):
            body = device_description(self.pc_name, self.guid)
            ctype = 'text/xml; charset="utf-8"'
        elif "scpd" in self.path.lower():
            body = SCPD
            ctype = 'text/xml; charset="utf-8"'
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        if self.log:
            self.log(
                f"HTTP GET {self.path} from {self.client_address[0]} -> 200 ({len(body)} bytes)"
            )


class Discovery:
    def __init__(
        self, iface_ip: str, guid: str, pc_name: str, http_port: int, verbose=True
    ):
        self.iface_ip = iface_ip
        self.guid = guid.upper()
        self.pc_name = pc_name
        self.http_port = http_port
        self.location = f"http://{iface_ip}:{http_port}/desc.xml"
        self.verbose = verbose
        self.bootid = int(time.time()) & 0x7FFFFFFF
        self._stop = threading.Event()
        self.seen_cameras: dict[str, dict] = {}
        # optional callback invoked with the parsed camera dict whenever a camera
        # description is fetched
        self.on_camera = None
        self.camera_ip: str | None = None
        self._last_fetch: dict[str, float] = {}

    def log(self, *a):
        if self.verbose:
            print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

    # ---- HTTP description server ----
    def start_http(self):
        HTTPHandler.guid = self.guid
        HTTPHandler.pc_name = self.pc_name
        HTTPHandler.log = self.log
        srv = ThreadingHTTPServer(("0.0.0.0", self.http_port), HTTPHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.log(
            f"HTTP description server on :{self.http_port} (LOCATION {self.location})"
        )
        return srv

    # ---- SSDP advertise ----
    def _notify_targets(self):
        g = self.guid
        return [
            ("upnp:rootdevice", f"uuid:{g}::upnp:rootdevice"),
            (f"uuid:{g}", f"uuid:{g}"),
            (DEVICE_TYPE, f"uuid:{g}::{DEVICE_TYPE}"),
            (SERVICE_TYPE, f"uuid:{g}::{SERVICE_TYPE}"),
        ]

    def _send_notify(self, sock, nt, usn, nts="ssdp:alive"):
        if nts == "ssdp:alive":
            extra = (
                f"Cache-Control: max-age=1800\r\n"
                f"Location: {self.location}\r\n"
                f"Server: {SERVER_HDR}\r\n"
            )
        else:
            extra = ""
        msg = (
            f"NOTIFY * HTTP/1.1\r\nHOST: {MCAST_ADDR}:{SSDP_PORT}\r\n"
            f"{extra}NT: {nt}\r\nNTS: {nts}\r\nUSN: {usn}\r\n\r\n"
        ).encode()
        sock.sendto(msg, (MCAST_ADDR, SSDP_PORT))

    def advertise_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self.iface_ip),
            )
        except Exception:
            pass
        sock.bind((self.iface_ip, 0))
        first = True
        while not self._stop.is_set():
            for nt, usn in self._notify_targets():
                self._send_notify(sock, nt, usn)
            if first:
                self.log(f"advertised as uuid:{self.guid} name={self.pc_name}")
                first = False
            self._stop.wait(15)
        # byebye on shutdown
        for nt, usn in self._notify_targets():
            self._send_notify(sock, nt, usn, nts="ssdp:byebye")
        sock.close()

    # ---- SSDP M-SEARCH responder ----
    def respond_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception:
            pass
        sock.bind(("", SSDP_PORT))
        for ip in {self.iface_ip, "0.0.0.0"}:
            try:
                mreq = socket.inet_aton(MCAST_ADDR) + socket.inet_aton(ip)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except Exception:
                pass
        sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                continue
            text = data.decode("latin1", "replace")
            if text.startswith("NOTIFY"):
                if "canon" in text.lower() or "icpo" in text.lower():
                    self._handle_ssdp(data, addr[0])
                continue
            if not text.startswith("M-SEARCH"):
                continue
            st = ""
            for line in text.split("\r\n"):
                if line.lower().startswith("st:"):
                    st = line[3:].strip()
            self.log(f"M-SEARCH from {addr[0]} ST={st}")
            targets = {
                DEVICE_TYPE,
                SERVICE_TYPE,
                "upnp:rootdevice",
                "ssdp:all",
                f"uuid:{self.guid}",
            }
            if st not in targets:
                continue
            resp = self._search_response(st)
            sock.sendto(resp, addr)
        sock.close()

    def _search_response(self, st: str) -> bytes:
        g = self.guid
        if st == "upnp:rootdevice":
            usn = f"uuid:{g}::upnp:rootdevice"
        elif st == f"uuid:{g}":
            usn = f"uuid:{g}"
        else:
            usn = f"uuid:{g}::{st}"
        return (
            f"HTTP/1.1 200 OK\r\n"
            f"ST: {st}\r\n"
            f"USN: {usn}\r\n"
            f"Location: {self.location}\r\n"
            f"Cache-Control: max-age=1800\r\n"
            f"Server: {SERVER_HDR}\r\n"
            f"Ext:\r\n"
            f"Date: {time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}\r\n\r\n"
        ).encode()

    # ---- search for the camera + parse descriptions ----
    def search_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self.iface_ip),
            )
        except Exception:
            pass
        sock.bind((self.iface_ip, 0))
        sock.settimeout(0.5)
        while not self._stop.is_set():
            msearch = (
                f"M-SEARCH * HTTP/1.1\r\nHOST: {MCAST_ADDR}:{SSDP_PORT}\r\n"
                f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {SERVICE_TYPE}\r\n\r\n'
            ).encode()
            sock.sendto(msearch, (MCAST_ADDR, SSDP_PORT))
            t = time.time()
            while time.time() - t < 3 and not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    break
                self._handle_ssdp(data, addr[0])
            self._stop.wait(5)
        sock.close()

    def _handle_ssdp(self, data: bytes, src: str):
        text = data.decode("latin1", "replace")
        if "canon" not in text.lower() and "icpo" not in text.lower():
            return
        headers = {}
        for line in text.split("\r\n")[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        location = headers.get("location", "")
        usn = headers.get("usn", "")
        if not location or src == self.iface_ip:
            return
        if time.time() - self._last_fetch.get(src, 0.0) < 2.0:
            return
        self._last_fetch[src] = time.time()
        self.log(f"Canon SSDP from {src}: USN={usn} LOCATION={location}")
        self.fetch_description(src, location)

    def fetch_description(self, src: str, location: str):
        try:
            with urllib.request.urlopen(location, timeout=5) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception as e:
            self.log(f"  (failed to fetch description: {e})")
            return

        def field(name):
            m = re.search(
                rf"<(?:[A-Za-z0-9_-]+:)?{name}\b[^>]*>(.*?)</(?:[A-Za-z0-9_-]+:)?{name}>",
                body,
                re.S,
            )
            return m.group(1).strip() if m else ""

        d = {
            "ip": src,
            "location": location,
            "uuid": field("UDN"),
            "friendlyName": field("friendlyName"),
            "serialNumber": field("serialNumber"),
            "targetId": field("X_targetId"),
            "vendVer": field("X_vendVer"),
            "nickname": field("X_deviceNickname"),
        }
        self.seen_cameras[src] = d
        # ignore our own advertisement (it will have our GUID as UDN)
        if d["uuid"].lower().replace("uuid:", "").strip("{}") == self.guid.lower():
            return
        paired = (
            d["targetId"].lower().replace("uuid:", "").strip("{}") == self.guid.lower()
        )
        is_camera = bool(d["serialNumber"] or d["nickname"])
        mode = {"00": "Pairing", "01": "Transfer"}.get(
            (d["vendVer"].split("-")[3] if len(d["vendVer"].split("-")) == 6 else ""),
            "?",
        )
        label = "camera" if is_camera else "peer host"
        self.log(
            f"  {label} {d['ip']} name={d['friendlyName']!r} serial={d['serialNumber']} "
            f"nick={d['nickname']!r} mode={mode} targetId={d['targetId']!r} "
            f"-> {'PAIRED TO US' if paired else 'not paired to us'}"
        )
        d["paired"] = paired
        d["mode"] = mode
        d["is_camera"] = is_camera
        if self.on_camera:
            try:
                self.on_camera(d)
            except Exception as e:
                self.log(f"  on_camera error: {e}")

    def probe_loop(self):
        """Actively poll a known camera IP (the camera's SSDP window can be brief)."""
        if not self.camera_ip:
            return
        url = f"http://{self.camera_ip}:49152/upnp/CameraDevDesc.xml"
        while not self._stop.is_set():
            try:
                s = socket.create_connection((self.camera_ip, 49152), timeout=1.2)
                s.close()
                self.fetch_description(self.camera_ip, url)
            except Exception:
                pass
            self._stop.wait(1.5)

    def run(self):
        srv = self.start_http()
        threads = [
            threading.Thread(target=self.respond_loop, daemon=True),
            threading.Thread(target=self.advertise_loop, daemon=True),
            threading.Thread(target=self.search_loop, daemon=True),
            threading.Thread(target=self.probe_loop, daemon=True),
        ]
        for t in threads:
            t.start()
        self.log(
            "running — put the camera in 'Auto send images to computer' and select this computer"
        )
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.log("stopping…")
            self._stop.set()
            time.sleep(1)
        finally:
            srv.shutdown()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--iface-ip", default=None, help="local IP to advertise (default: auto)"
    )
    ap.add_argument("--http-port", type=int, default=2869)
    args = ap.parse_args()
    state = load_state()
    guid, pc_name = get_identity(state)
    iface_ip = args.iface_ip or guess_iface_ip()
    print(f"identity: guid={guid} pc_name={pc_name}")
    print(f"iface_ip: {iface_ip}")
    d = Discovery(iface_ip, guid, pc_name, args.http_port)
    d.run()


if __name__ == "__main__":
    main()
