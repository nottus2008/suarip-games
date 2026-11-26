# SuaRip Games

Drop an old game disc in the drive and get a verified image plus a manifest in your library folder.
It chooses the right ripping method for each disc, falls back when a disc is troublesome, and records
what happened in a `game.json` next to the image. Command line first, with an optional local web
interface.

> Status: 1.0, built and tested on Linux (Debian/MX) with several Hitachi-LG optical drives. Expect rough
> edges with other hardware, and please open an issue with the log if something misbehaves.

## What it does

- Looks at each disc (type, volume label, audio tracks, copy-protection files) and picks a method:
  - DVD / Blu-ray data discs: `dd`, falling back to `ddrescue` if there are read errors
  - CDs with audio tracks, or with protection-style files: [redumper](https://github.com/superg/redumper) (BIN/CUE)
  - Plain data CDs: `dd` / `ddrescue`, which gives an installable ISO quickly
- If redumper fails on a data-only CD, it saves a plain ISO instead and records the fallback.
- Hashes every image (CRC32, MD5, SHA1), copies it to your library, and re-hashes the copy.
- Groups multi-disc games and writes a `game.json` manifest per game.
- Per-drive redumper settings (some drives need generic-drive options to give good CD dumps).
- Optional notifications through [ntfy](https://ntfy.sh), plus a terminal bell and desktop popup.
- Ctrl+C skips the current disc; press it twice within 3 seconds to quit.

## Requirements

Linux, Python 3.8 or newer, and these tools:

```
sudo apt install gddrescue eject lsscsi genisoimage
```

redumper is a separate project and is not bundled. Download the Linux build from its
[releases page](https://github.com/superg/redumper/releases) and point the config at the binary
(or put it on your `PATH`). Your user needs access to the drive's `/dev/sr*` and `/dev/sg*` nodes,
which normally means being in the `cdrom` group:

```
sudo usermod -aG cdrom $USER      # then log out and back in
```

Turn off your desktop's automount for removable media, since the drive must not be mounted while ripping.

## Quick start

```
./suarip-games.py --list-drives     # shows your drives; creates ~/.config/suarip-games/config.json
./suarip-games.py                   # rip the disc that is in a drive
./suarip-games.py --watch           # keep going: rip each disc as it is inserted
```

Each disc shows a summary (drive, disc, title, engine, and why) and starts by itself after 30 seconds.
Press `t`, `d`, `e` to change the title, disc number or engine first, or `s` to skip the disc.
Use `--yes` to skip the prompt.

Useful options: `--title`, `--disc`, `--engine {auto,redumper,builtin}`, `--mode {simple,rescue,auto}`,
`--damaged`, `--resume`, `--give-up`, `--force-split`, `--no-fallback`, `--slim`, `--force`, `--test-notify`, `--version`.

## Problem discs

If a disc is scratched or worn, give it a different drive and a cleaning first. Then:

```
./suarip-games.py --damaged             # skip plain dd, go straight to ddrescue, keep what it recovers
./suarip-games.py --resume --dev /dev/sr1   # continue the earlier partial rip in another drive
```

ddrescue gives up after `give_up` (default 10 minutes) with no successful read, so one bad disc cannot
tie up a drive for hours (`--give-up 30m` to change it). Whatever was recovered is saved and marked
`damaged` in the manifest, with the number of unrecovered bytes and the mapfile kept beside the image.
These flags only change the dd/ddrescue path; discs with audio tracks stay with redumper.

## Web interface

```
./suarip-games-web.py               # then open http://127.0.0.1:8765
```

Standard library only. It has a Drives page (one card per drive with an editable title, start, skip and
a live log), a Library page, and a Setup page that checks your tools and permissions, shows the install
commands for anything missing, and lets you pick the redumper binary, scratch folder and library with a
folder browser. It never runs `sudo`.

It listens on this machine only. To reach it from another computer, use
`--host 0.0.0.0 --token SOMETHING`; it refuses to listen beyond localhost without a token, because the
page can write files, run tools and eject drives. Do not expose it to the internet.

## Configuration

`~/.config/suarip-games/config.json` is created on first run and shared by the CLI and the web interface.

| Key | Meaning |
|---|---|
| `library` | where finished rips go (a folder per game) |
| `scratch` | local working folder; rips happen here first, then copy to the library |
| `mount` | optional mount point of a network share; rips refuse to run if it is not mounted |
| `redumper`, `redumper_args` | path to redumper, and default extra options |
| `drives` | per-drive options, keyed by part of the drive's model name (see `--list-drives`) |
| `ntfy_url`, `ntfy_topic`, `ntfy_token` (or `ntfy_user`/`ntfy_password`) | notifications |
| `notify_on` | events that notify: clean, review, fallback, damaged, check-log, failed, skipped, ready |
| `prompt_timeout`, `slim`, `desktop_notify` | summary countdown, drop redumper raw files, desktop popups |
| `give_up` | ddrescue stops after this long with no successful read (default `10m`) |

### A note on generic drives

redumper only trusts a short list of known drives. On others it may report a Q error count equal to the
number of sectors; the fix is usually a drive profile such as
`--drive-type=GENERIC --drive-sector-order=DATA_SUB_C2 --speed=8` (try the other sector orders if not).
Such drives also often cannot read the CD lead-out, so redumper's track split can fail on the last few
samples; use `--force-split` to write the tracks anyway (the rip is marked `review`).

## Limitations

- A plain ISO is an installable copy, not a verified preservation dump.
- Images of copy-protected discs (SafeDisc, SecuROM, StarForce) are archives; they generally will not run
  from an image on modern systems.
- Xbox, Xbox 360, GameCube and Wii discs need special drives or firmware and are not supported.
- No Redump database lookup yet.

## Known gaps

- There is no run log file yet; output goes to the terminal (and the web page's live log).
- No free-space check before a rip. Keep a few GB free on the scratch drive (up to about 9 GB for a dual-layer DVD).
- No per-drive lock: do not start two copies of the tool on the same drive. (Different drives at the same
  time are fine; manifest writes are locked and atomic.)
- No Redump hash lookup and no BIN/CUE to ISO conversion yet (`bchunk` does the latter by hand).

Tested on Linux (MX/Debian) with Hitachi-LG drives. Reports for other drives are welcome.

## Legal

This tool is meant for making backup and archival copies of discs you own. It does not remove or bypass
copy protection. Copyright and anti-circumvention laws differ by country, and you are responsible for how
you use it.

## License

MIT, see `LICENSE` (replace the name in it with yours before publishing).
