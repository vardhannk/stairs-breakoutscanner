#!/usr/bin/env python3
"""
patch_mobile.py — make the main page usable on a phone.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_mobile.py --check
    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_mobile.py --apply

Three changes, all additive:

  1. imports ui_theme and calls apply() immediately after set_page_config,
     which injects the responsive CSS (columns stack under 640px, tap
     targets reach 44px, wide tables scroll rather than clip)
  2. sets initial_sidebar_state="collapsed" so the sidebar does not cover
     the whole screen on load — it still opens from the hamburger
  3. writes .streamlit/config.toml with the settings that matter behind a
     reverse proxy, without touching any that are already set

ui_theme.py must sit beside app.py. Nothing in breakout.py, scanner.py or
config.py is touched, so detection and scan behaviour are unchanged.

Idempotent. Backs up. Restores the original if the result fails to compile.
"""

import argparse
import ast
import datetime
import os
import re
import shutil
import subprocess
import sys

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
STAMP = datetime.datetime.now().strftime("%F-%H%M%S")

# --app-dir lets this run from a staging directory during a dry run, before
# the files have been installed next to app.py. Without it, a --check from
# /root/bo-staging looks for app.py in the staging folder and fails on a
# problem that does not exist.
APP_DIR = SELF_DIR
APP = CFG_DIR = CFG = THEME = None


def _resolve(app_dir: str) -> None:
    global APP_DIR, APP, CFG_DIR, CFG, THEME
    APP_DIR = os.path.abspath(app_dir)
    APP = os.path.join(APP_DIR, "app.py")
    CFG_DIR = os.path.join(APP_DIR, ".streamlit")
    CFG = os.path.join(CFG_DIR, "config.toml")
    # ui_theme must end up beside app.py, but during a dry run it may still
    # only exist in the staging folder. Either location satisfies --check;
    # --apply requires it to actually be installed.
    beside_app = os.path.join(APP_DIR, "ui_theme.py")
    THEME = beside_app if os.path.isfile(beside_app) \
        else os.path.join(SELF_DIR, "ui_theme.py")


_resolve(SELF_DIR)

OK, NO, SK, WN = "\033[32m  ok  \033[0m", "\033[31m fail \033[0m", \
                 "\033[36m skip \033[0m", "\033[33m warn \033[0m"

MARKER = "# ── mobile styling (added by patch_mobile.py)"

INJECT = f'''
{MARKER} ─────────────────────────
# Responsive CSS only. If ui_theme.py is missing the app renders exactly as
# it did before — styling must never be able to break a scan.
try:
    import ui_theme as _ui_theme
    _ui_theme.apply()
except Exception:
    pass
# ─────────────────────────────────────────────────────────────────────────
'''

# Real Python types, not pre-formatted strings. An earlier version of this
# file stored every value as a string and wrote it out bare, which turned
# `toolbarMode = "minimal"` into `toolbarMode = minimal` — invalid TOML.
# Streamlit discards a config file it cannot parse, silently and in full, so
# losing one line cost baseUrlPath as well and the app served on / behind an
# nginx location expecting /breakout/. That is a redirect loop.
#
# Kept deliberately small. The mobile work is entirely CSS in ui_theme.py;
# nothing here is required for it. toolbarMode only declutters the header on a
# narrow screen, and if this dict were empty the config file would not be
# touched at all.
CONFIG_WANT: dict[str, dict[str, object]] = {
    "client": {
        # "minimal" hides the Deploy button and the hamburger's developer
        # entries, which on a phone occupy most of the header
        "toolbarMode": "minimal",
    },
}


def _toml_value(v: object) -> str:
    """Format a Python value as TOML. Strings get quoted; that was the bug."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"')


def _parse_str(text: str):
    """
    Parse TOML text. Returns a dict, or None if no parser is installed.

    Three return kinds, kept distinct because they need different responses:

        dict   parsed fine
        None   no TOML library installed — we cannot verify, so do nothing
        False  a parser ran and the text is INVALID

    Three candidates because the interpreter version is not knowable here:
    tomllib is stdlib from 3.11, tomli is its backport, and toml is the older
    third-party library that Streamlit itself has long depended on — so on a
    box running Streamlit at all, one of these is essentially always present.
    """
    for name in ("tomllib", "tomli", "toml"):
        try:
            mod = __import__(name)
        except ImportError:
            continue
        try:
            return mod.loads(text)
        except Exception:
            return False
    return None


def _load_toml(path: str):
    """Parse a TOML file. {} when absent, None when no parser is available."""
    if not os.path.isfile(path):
        return {}
    return _parse_str(open(path).read())


def _insert_keys(text: str, additions: dict) -> str:
    """
    Add missing keys, leaving every existing byte where it is.

    Rewriting the file from a parsed copy is what caused the outage: the
    round trip silently dropped anything the parser did not model. Appending
    into the original text cannot lose a line it never looked at.
    """
    lines = text.splitlines()

    bounds, cur, start = {}, None, None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]") and not s.startswith("[["):
            if cur is not None:
                bounds[cur] = (start, i)
            cur, start = s[1:-1].strip(), i + 1
    if cur is not None:
        bounds[cur] = (start, len(lines))

    out = list(lines)
    # bottom-up, so an insertion never invalidates an index computed earlier
    order = sorted(additions, key=lambda s: bounds.get(s, (10 ** 9,))[0],
                   reverse=True)
    for sec in order:
        kv = additions[sec]
        if sec in bounds:
            s0, s1 = bounds[sec]
            j = s1
            while j > s0 and not lines[j - 1].strip():
                j -= 1
            for k, v in reversed(list(kv.items())):
                out.insert(j, f"{k} = {_toml_value(v)}")
        else:
            if out and out[-1].strip():
                out.append("")
            out.append(f"[{sec}]")
            for k, v in kv.items():
                out.append(f"{k} = {_toml_value(v)}")

    return "\n".join(out) + "\n"


def patch_app(apply: bool) -> int:
    if not os.path.isfile(APP):
        print(f"{NO} {APP} not found")
        print(f"       pass --app-dir /opt/breakoutscanner if running from "
              f"a staging folder")
        return 2
    if not os.path.isfile(THEME):
        print(f"{NO} ui_theme.py not found in {APP_DIR} or {SELF_DIR}")
        return 2
    if apply and not os.path.isfile(os.path.join(APP_DIR, "ui_theme.py")):
        print(f"{NO} ui_theme.py must be installed beside app.py before "
              f"--apply (found only in {SELF_DIR})")
        return 2

    src = orig = open(APP).read()

    # 1. sidebar collapsed on load
    m = re.search(r"st\.set_page_config\((.*?)\)", src, re.S)
    if not m:
        print(f"{NO} no st.set_page_config(...) call found in app.py")
        return 3
    call = m.group(0)
    if "initial_sidebar_state" in call:
        print(f"{SK} initial_sidebar_state already set")
    else:
        new_call = call[:-1].rstrip()
        if not new_call.endswith("("):
            new_call += ","
        new_call += ' initial_sidebar_state="collapsed")'
        src = src.replace(call, new_call, 1)
        print(f"{OK} sidebar starts collapsed")

    # 2. inject the stylesheet right after set_page_config
    if MARKER in src:
        print(f"{SK} ui_theme already wired")
    else:
        m2 = re.search(r"st\.set_page_config\(.*?\)", src, re.S)
        end = m2.end()
        src = src[:end] + "\n" + INJECT + src[end:]
        print(f"{OK} ui_theme.apply() injected")

    if src == orig:
        return 0

    try:
        ast.parse(src)
        print(f"{OK} app.py parses")
    except SyntaxError as e:
        print(f"{NO} would not parse: {e} — nothing written")
        return 4

    if not apply:
        print(f"{WN} --check only; app.py not written")
        return 0

    b = f"{APP}.bak-mobile-{STAMP}"
    shutil.copy2(APP, b)
    open(APP, "w").write(src)
    r = subprocess.run([sys.executable, "-m", "py_compile", APP],
                       capture_output=True, text=True)
    if r.returncode != 0:
        shutil.copy2(b, APP)
        print(f"{NO} py_compile failed; ORIGINAL RESTORED\n{r.stderr}")
        return 5
    print(f"{OK} app.py written  (backup {os.path.basename(b)})")
    return 0


def patch_config(apply: bool) -> int:
    if not CONFIG_WANT:
        print(f"{SK} config.toml not touched (nothing to add)")
        return 0

    original = open(CFG).read() if os.path.isfile(CFG) else ""

    current = _load_toml(CFG)
    if current is None:
        print(f"{WN} no TOML parser available (tomllib, tomli or toml)")
        print(f"{WN} refusing to edit config.toml unverified — skipping it")
        print(f"       costs nothing but a slightly busier header on mobile")
        return 0
    if current is False:
        print(f"{NO} {CFG} does not parse as TOML — not touching it")
        print(f"       Streamlit is ignoring it too, in full. Fix or remove it;")
        print(f"       a config it cannot read is why baseUrlPath stops applying")
        return 6

    additions = {}
    for sec, kv in CONFIG_WANT.items():
        for k, v in kv.items():
            if k not in current.get(sec, {}):
                additions.setdefault(sec, {})[k] = v
    if not additions:
        print(f"{SK} config.toml already has every setting")
        return 0

    flat = [f"{s}.{k} = {_toml_value(v)}"
            for s, kv in additions.items() for k, v in kv.items()]
    print(f"{OK} config.toml adds: {', '.join(flat)}")

    new_text = _insert_keys(original, additions) if original.strip() else \
        "".join(f"[{s}]\n" + "".join(f"{k} = {_toml_value(v)}\n"
                                     for k, v in kv.items())
                for s, kv in additions.items())

    # The guard that would have prevented the outage: parse the RESULT before
    # writing, and confirm every key that existed before still resolves to the
    # same value. Streamlit discards an unparseable config in full and says
    # nothing, so an invalid file loses settings you never edited.
    parsed = _parse_str(new_text)
    if parsed is None:
        print(f"{WN} cannot verify the result parses — skipping config.toml")
        return 0
    if parsed is False:
        print(f"{NO} the edit would produce invalid TOML — nothing written")
        print(f"       this is a bug in patch_mobile.py, not in your config")
        return 6
    lost = []
    for sec, kv in current.items():
        if not isinstance(kv, dict):
            continue
        for k, v in kv.items():
            if parsed.get(sec, {}).get(k) != v:
                lost.append(f"{sec}.{k}")
    if lost:
        print(f"{NO} the edit would change or lose: {', '.join(lost)}")
        print(f"       nothing written")
        return 6
    print(f"{OK} result parses; all {sum(len(v) for v in current.values() if isinstance(v, dict))} "
          f"existing keys preserved")

    if not apply:
        return 0

    os.makedirs(CFG_DIR, exist_ok=True)
    if os.path.isfile(CFG):
        shutil.copy2(CFG, f"{CFG}.bak-mobile-{STAMP}")
    with open(CFG, "w") as f:
        f.write(new_text)
    print(f"{OK} {CFG} updated (appended, not rewritten)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--app-dir", default=None,
                    help="directory containing app.py (default: beside this "
                         "script). Use when running from a staging folder.")
    a = ap.parse_args()

    if a.app_dir:
        _resolve(a.app_dir)
    print(f"\n       app dir: {APP_DIR}")
    rc = patch_app(a.apply)
    if rc:
        return rc
    rc = patch_config(a.apply)
    if rc:
        return rc

    if a.apply:
        print("\nNext:  sudo systemctl restart breakoutscanner")
    else:
        print(f"\n{WN} --check only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
