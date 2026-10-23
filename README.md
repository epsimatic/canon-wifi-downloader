# Canon WiFi Photos Downloader

**a.k.a. Canon Image Transfer Utility 2 (ITU2) emulator**

A set of scripts to pair & download photos and videos from a WiFi connected Canon DSLR camera via PTP/IP.

***Warning: vibe-coded.** Everything except this README is made with an LLM agent assistance and wasn't thoroughly checked by a human. It works perfectly for me but your mileage may vary.*

This is very much a PoC / work in progress right now. Expect mony the basic downloading function to work. 

### Usage instructions:

1. Run the pairing script:   
  ```bash
  python3 itu2.py
  ```
2. In Canon camera, enter **Wireless settings** → **Wi-Fi settings: Enable** → **Auto send images to computer** → **Auto send: Enable** → **OK**
3. Connect to your Wi-Fi → **IP address set: Auto** → **OK**
4. You should see `Downloader XXXX` in the list. Select it to connect. It'll show "Confirm in Canon app" shortly, then **Settings complete**.
5. **Reboot the camera.** It only starts transferring photos after a reboot.
6. Camera would blink the wireless icon on status screen. And your photos would slowly start appearing in the `downloads` folder

If camera shows a "Wireless OFF" icon, try restarting it. It only enters image transfer mode for a short time after a start-up.

### Protocol docs:

See [PROTOCOL.md](PROTOCOL.md)