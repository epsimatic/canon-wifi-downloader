# Canon WiFi Photos Downloader

**a.k.a. Canon Image Transfer Utility 2 (ITU2) emulator**

A set of scripts to pair & download photos and videos from a WiFi connected Canon DSLR camera via PTP/IP.

***Warning: vibe-coded.** all the code and most of the docs are assisted by a LLM agent and wasn't thoroughly checked by a human. It works perfectly for me but your mileage may vary.*

This is very much a PoC / work in progress right now. Expect only the basic downloading function to work. 

### Usage instructions:

1. Start the script:   
  ```bash
  python3 itu2.py                      # save to ./downloads
  python3 itu2.py -d ~/Pictures/camera # save to a specific directory
  python3 itu2.py -i http://immich.local:2283 --immich-key KEY  # upload to Immich
  python3 itu2.py --avif               # convert to AVIF after downloading
  ```
2. In Canon camera, enter **Wireless settings** → **Wi-Fi settings: Enable** → **Auto send images to computer** → **Auto send: Enable** → **OK**
3. Connect to your Wi-Fi → **IP address set: Auto** → **OK**
4. You should see `Downloader XXXX` in the list. Select it to connect. It'll show "Confirm in Canon app" shortly, then **Settings complete**.
5. **Reboot the camera.** It only starts transferring photos after a reboot.
6. Camera would blink the wireless icon on status screen. And your photos would slowly start appearing in your chosen destination.

If camera shows a "🛜OFF" icon, try restarting the camera. It only enters image transfer mode for a short time after a start-up.   
If that fails too, un-pair the camera (from the camera menu), delete `state.json` and start again.

Run with `--help` for all options.

### Destinations

Exactly one destination is used per run:

| Option | Destination |
|---|---|
| `-d DIR`, `--dir DIR` | Write files under `DIR`. Default: `downloads`. |
| `-i URL`, `--immich URL` | Upload each new photo/video to an Immich instance. |

#### Saving to a directory

By default, files are grouped by camera and by the camera's own folder:

```
downloads/
  EOS250D_123456/
    100CANON/
      IMG_0001.JPG
    101CANON/
      IMG_2000.JPG
```

Use `--flat` to write only the camera filename into the output directory. `--redownload-missing` re-fetches entries whose local file was deleted.

#### Uploading to Immich

Create an API key in Immich (**Account/User Settings → API Keys → New API Key**) and grant these permissions:

- `asset.upload`
- `album.read`
- `album.create`
- `albumAsset.create`
- `asset.share`
- `user.read`

`asset.upload` alone is enough if you do not use `--immich-album`. The easiest option in the Immich UI is to select all permissions.

Then pass the key with `--immich-key`, or export `IMMICH_API_KEY`. Use `--immich-key KEY --immich-save-key` to store key in settings.

### AVIF conversion

Pass `--avif` to convert JPEG downloads to AVIF before saving or uploading. AVIF is typically 5–8× smaller than the camera JPEG at the default settings. The JPEG is **replaced** by the `.avif` file unless `--keep-original` is given.

- Requires [`avifenc`](https://github.com/AOMediaCodec/libavif) from libavif: `brew install libavif` (macOS) or `apt install libavif-bin` (Debian/Ubuntu). Set `AVIFENC` to use a non-PATH binary.
- Defaults: quality `55`, speed `4`, source chroma (4:2:0 for camera JPEGs). EXIF/XMP/ICC and capture metadata are preserved.
- Only JPEG objects are converted; RAW (CR3) and movies are saved unchanged. If encoding fails for an object, the original JPEG is kept and a warning is logged.

## Protocol docs:

See [PROTOCOL.md](PROTOCOL.md)