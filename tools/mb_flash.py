#!/usr/bin/env python3
"""
mb_flash.py -- install the Home Assistant bridge on a Metroboard (ESP32-C3).

Edits the board's littlefs partition offline, through the bootloader, with
esptool. That works no matter what the running app is doing, and doesn't need
a REPL.

  python3 tools/mb_flash.py backup             full 4 MB flash dump, before anything else
  python3 tools/mb_flash.py ls                 list the board's filesystem
  python3 tools/mb_flash.py install            install or update the bridge
  python3 tools/mb_flash.py install --app-from BACKUP
                                               reinstall after a vendor update
  python3 tools/mb_flash.py uninstall          remove the bridge, back to stock

Pass --image FILE to work on a local copy of the partition instead of the board.

Requires: pip install esptool littlefs-python "mpy-cross==1.25.0.post2"
"""
import argparse, datetime, glob, json, os, subprocess, sys, tempfile

VFS_OFFSET = 0x200000
VFS_SIZE   = 0x200000
FLASH_SIZE = 0x400000
BLOCK_SIZE = 4096

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_MAIN = "/base_app/app_main.py"
HOOK_ANCHOR = "        ctx.buttons = buttons\n"
HOOK_BEGIN = "        # --- mb_hass: Home Assistant (MQTT) bridge ---\n"
HOOK_END = "        # --- /mb_hass ---\n"
HOOK = (
    HOOK_BEGIN
    + "        try:\n"
    + "            import mb_hass\n"
    + "            asyncio.create_task(mb_hass.run(ctx))\n"
    + "        except Exception as _mbh_e:\n"
    + "            log.warning(\"mb_hass not started: %s\", _mbh_e)\n"
    + HOOK_END
)
STATE = "/mb_hass.state.json"   # what install changed, so uninstall can undo it


# ---------------------------------------------------------------- board i/o

def find_port():
    for pat in ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*",
                "/dev/ttyACM*", "/dev/ttyUSB*"):
        found = sorted(glob.glob(pat))
        if found:
            return found[0]
    sys.exit("No serial port found. Plug the board in over USB-C, or pass --port.")

def esptool(port, *args):
    cmd = [sys.executable, "-m", "esptool", "--port", port, "--baud", "921600", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        sys.exit("esptool failed: " + " ".join(args))

def load(args):
    if args.image:
        return open(args.image, "rb").read()
    port = args.port or find_port()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "vfs.bin")
        print("Reading filesystem from %s..." % port)
        esptool(port, "read-flash", hex(VFS_OFFSET), hex(VFS_SIZE), path)
        return open(path, "rb").read()

def save(args, buf):
    if args.image:
        open(args.image, "wb").write(buf)
        print("Wrote", args.image)
        return
    port = args.port or find_port()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "vfs.bin")
        open(path, "wb").write(buf)
        print("Writing filesystem back and resetting...")
        esptool(port, "--after", "hard-reset", "write-flash", hex(VFS_OFFSET), path)


# --------------------------------------------------------------- littlefs

def mount(buf):
    from littlefs import LittleFS
    fs = LittleFS(block_size=BLOCK_SIZE, block_count=len(buf) // BLOCK_SIZE,
                  read_size=256, prog_size=256, mount=False)
    fs.context.buffer = bytearray(buf)
    fs.mount()
    return fs

def exists(fs, path):
    try:
        fs.stat(path)
        return True
    except Exception:
        return False

def read(fs, path):
    with fs.open(path, "rb") as f:
        return f.read()

def write(fs, path, data):
    with fs.open(path, "wb") as f:
        f.write(data)
    print("  write ", path)

def remove(fs, path):
    if exists(fs, path):
        fs.remove(path)
        print("  remove", path)

def is_dir(fs, path):
    return fs.stat(path).type == 2

def remove_tree(fs, path):
    for name in fs.listdir(path):
        child = path + "/" + name
        if is_dir(fs, child):
            remove_tree(fs, child)
        else:
            fs.remove(child)
    fs.remove(path)

def copy_tree(src, dst, path):
    dst.mkdir(path)
    for name in src.listdir(path):
        child = path + "/" + name
        if is_dir(src, child):
            copy_tree(src, dst, child)
        else:
            with dst.open(child, "wb") as f:
                f.write(read(src, child))

def app_version(fs, app):
    path = "/%s/version.json" % app
    if exists(fs, path):
        return json.loads(read(fs, path)).get("version")
    return None


# ----------------------------------------------------------------- build

def build_mpy(src, out_dir):
    """Precompile for MicroPython 1.25 (.mpy v6). Compiling a big module on the
    board itself, next to the running app, runs out of memory."""
    out = os.path.join(out_dir, os.path.basename(src)[:-3] + ".mpy")
    r = subprocess.run([sys.executable, "-m", "mpy_cross", "-O2", src, "-o", out],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        sys.exit('mpy-cross failed. Install it with: pip install "mpy-cross==1.25.0.post2"')
    return open(out, "rb").read()


# -------------------------------------------------------------- commands

def cmd_backup(args):
    port = args.port or find_port()
    out = args.out or "metroboard-%s.bin" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    print("Dumping all 4 MB of flash from %s to %s..." % (port, out))
    esptool(port, "read-flash", "0", hex(FLASH_SIZE), out)
    print("Done. Keep this file. It has your Wi-Fi credentials and the board's secrets in it.")

def cmd_ls(args):
    fs = mount(load(args))
    def walk(p="/"):
        for name in sorted(fs.listdir(p)):
            fp = p.rstrip("/") + "/" + name
            st = fs.stat(fp)
            if st.type == 2:
                print("    dir ", fp)
                walk(fp)
            else:
                print("%8d" % st.size, fp)
    walk()

def cmd_install(args):
    config = args.config or os.path.join(ROOT, "mb_hass.json")
    if not os.path.exists(config):
        sys.exit("No config at %s. Copy mb_hass.example.json to mb_hass.json and set your broker." % config)
    json.load(open(config))  # fail early on a typo

    with tempfile.TemporaryDirectory() as d:
        bridge = build_mpy(os.path.join(ROOT, "mb_hass.py"), d)
        client = build_mpy(os.path.join(ROOT, "lib", "mqtt_as.py"), d)

    fs = mount(load(args))

    state = json.loads(read(fs, STATE)) if exists(fs, STATE) else {
        "boot_select": read(fs, "/boot_select.txt").decode().strip() if exists(fs, "/boot_select.txt") else None,
        "quarantine_before": read(fs, "/ota_quarantine.version").decode().strip() if exists(fs, "/ota_quarantine.version") else None,
    }

    # Versions the board must not switch to, or it would leave the hooked app.
    skip = [app_version(fs, "updated_app")]

    print("Installing:")
    if args.app_from:
        # A vendor update has replaced base_app with a compiled build. Put back
        # the readable one from the owner's own backup.
        backup = open(args.app_from, "rb").read()
        if len(backup) == FLASH_SIZE:
            backup = backup[VFS_OFFSET:VFS_OFFSET + VFS_SIZE]
        old = mount(backup)
        if not exists(old, APP_MAIN):
            sys.exit("%s has no readable %s either." % (args.app_from, APP_MAIN))
        skip.append(app_version(fs, "base_app"))
        if exists(fs, "/base_app"):
            remove_tree(fs, "/base_app")
        copy_tree(old, fs, "/base_app")
        print("  write  /base_app (v%s, from %s)" % (app_version(fs, "base_app"), args.app_from))

    if not exists(fs, APP_MAIN):
        sys.exit("%s not found. A vendor update has probably replaced the app with a compiled "
                 "build. Run install again with --app-from and the backup you made first." % APP_MAIN)
    app_main = read(fs, APP_MAIN).decode()
    if "import mb_hass" not in app_main:
        if app_main.count(HOOK_ANCHOR) != 1:
            sys.exit("Couldn't find where to add the hook in %s. Your app version may differ "
                     "from the one this was built for (v0.8.0)." % APP_MAIN)
        app_main = app_main.replace(HOOK_ANCHOR, HOOK_ANCHOR + "\n" + HOOK, 1)

    # Source copies would be compiled on the board at import time. Only ship .mpy.
    remove(fs, "/mb_hass.py")
    remove(fs, "/mqtt_as.py")
    write(fs, "/mb_hass.mpy", bridge)
    write(fs, "/mqtt_as.mpy", client)
    write(fs, "/mb_hass.json", open(config, "rb").read())
    write(fs, APP_MAIN, app_main.encode())

    # base_app is the readable copy of the app, where the hook lives. The board
    # only skips one update version, so skip the newest one it knows about.
    write(fs, "/boot_select.txt", b"base_app")
    current = app_version(fs, "base_app")
    newer = [v for v in skip if v and v != current]
    if newer:
        newest = max(newer, key=lambda v: tuple(int(n) for n in v.split(".")))
        write(fs, "/ota_quarantine.version", newest.encode())
    write(fs, STATE, json.dumps(state).encode())

    save(args, bytes(fs.context.buffer))
    print("Done. The board should show up in Home Assistant about 20 seconds after it boots.")

def cmd_uninstall(args):
    fs = mount(load(args))
    state = json.loads(read(fs, STATE)) if exists(fs, STATE) else {}

    print("Uninstalling:")
    if exists(fs, APP_MAIN):
        app_main = read(fs, APP_MAIN).decode()
        if HOOK_BEGIN in app_main:
            start = app_main.index("\n" + HOOK_BEGIN)
            end = app_main.index(HOOK_END) + len(HOOK_END)
            write(fs, APP_MAIN, (app_main[:start] + app_main[end:]).encode())

    if "boot_select" in state:
        if state["boot_select"]:
            write(fs, "/boot_select.txt", state["boot_select"].encode())
        else:
            remove(fs, "/boot_select.txt")
    if "quarantine_before" in state:
        if state["quarantine_before"]:
            write(fs, "/ota_quarantine.version", state["quarantine_before"].encode())
        else:
            remove(fs, "/ota_quarantine.version")

    for path in ("/mb_hass.mpy", "/mqtt_as.mpy", "/mb_hass.json", "/mb_hass.py", "/mqtt_as.py", STATE):
        remove(fs, path)

    save(args, bytes(fs.context.buffer))
    print("Done. The board is back to stock.")


def main():
    ap = argparse.ArgumentParser(description="Install the Home Assistant bridge on a Metroboard.")
    ap.add_argument("--port", help="serial port (found automatically if left out)")
    ap.add_argument("--image", help="work on a local copy of the filesystem partition instead of the board")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backup", help="dump the whole 4 MB flash")
    p.add_argument("out", nargs="?")
    p.set_defaults(fn=cmd_backup)
    sub.add_parser("ls", help="list the filesystem").set_defaults(fn=cmd_ls)
    p = sub.add_parser("install", help="install or update the bridge")
    p.add_argument("--config", help="path to mb_hass.json (default: ./mb_hass.json)")
    p.add_argument("--app-from", metavar="BACKUP",
                   help="restore the readable app from your backup first (after a vendor update)")
    p.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove the bridge").set_defaults(fn=cmd_uninstall)

    args = ap.parse_args()
    args.fn(args)

if __name__ == "__main__":
    main()
