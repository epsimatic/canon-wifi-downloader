# Canon WiFi Photos Downloader

**a.k.a. Canon Image Transfer Utility 2 (ITU2) emulator**

A set of scripts to pair & download photos and videos from a WiFi connected Canon DSLR camera via PTP/IP.

***Warning: vibe-coded.** Everything except this README is assisted by a LLM agent and wasn't thoroughly checked by a human. It works perfectly for me but your mileage may vary.*

This is very much a PoC / work in progress right now. Expect only the basic downloading function to work. 

### Usage instructions:

1. Run the pairing script:   
  ```bash
  python3 itu2.py
  ```
2. In Canon camera, enter **Wireless settings** → **Wi-Fi settings: Enable** → **Auto send images to computer** → **Auto send: Enable** → **OK**
3. Connect to your Wi-Fi → **IP address set: Auto** → **OK**
4. You should see `Downloader XXXX` in the list. Select it to connect. It'll show "Confirm in Canon app" shortly, then **Settings complete**.
5. **Reboot the camera.** It only starts transferring photos after a reboot.
6. Camera would blink the wireless icon on status screen. And your photos would slowly start appearing under `downloads/<camera>/<camera folder>/`

If camera shows a "🛜OFF" icon, try restarting the camera. It only enters image transfer mode for a short time after a start-up.   
If that fails too, un-pair the camera (from the camera menu), delete `state.json` and start again.

### Folder layout

By default, files are grouped by camera and by the camera's own folder:

```
downloads/
  EOS250D_123456/
    100CANON/
      IMG_0001.JPG
    101CANON/
      IMG_2000.JPG
```

Use `--flat` to write only the camera filename into the output directory. Run with `--help` for more options.

### Protocol docs:

See [PROTOCOL.md](PROTOCOL.md)