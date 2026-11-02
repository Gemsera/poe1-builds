#!/usr/bin/env bash
# Sets up headless Path of Building Community for the poe1-builds skill.
# Idempotent: re-running is fast once everything is in place.
#
#   bash setup.sh            # install if missing
#   bash setup.sh --update   # also pull the latest Path of Building release
#
# Env: POB_HOME (default ~/.cache/poe1-pob), POB_REF (default master = latest PoB release;
#      use "dev" for unreleased fixes right after a new league launches)
set -euo pipefail

POB_HOME="${POB_HOME:-$HOME/.cache/poe1-pob}"
POB_REF="${POB_REF:-master}"
POB_REPO="https://github.com/PathOfBuildingCommunity/PathOfBuilding.git"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLED="$HERE/../bin/linux-x86_64"
UPDATE=0
[[ "${1:-}" == "--update" ]] && UPDATE=1

mkdir -p "$POB_HOME/luajit/bin" "$POB_HOME/luamods"
log() { echo "[setup] $*"; }

# 1) LuaJIT ---------------------------------------------------------------
LUAJIT="$POB_HOME/luajit/bin/luajit"
if ! "$LUAJIT" -v >/dev/null 2>&1; then
  # skill packages may drop the executable bit, so copy + chmod before testing
  if [[ "$(uname -m)" == "x86_64" && -f "$BUNDLED/luajit" ]]; then
    cp "$BUNDLED/luajit" "$LUAJIT" && chmod +x "$LUAJIT"
  fi
  if "$LUAJIT" -v >/dev/null 2>&1; then
    log "Using bundled LuaJIT"
  else
    rm -f "$LUAJIT"
    log "Building LuaJIT from source (needs git, make, gcc)..."
    SRC="$POB_HOME/src-luajit"
    rm -rf "$SRC"
    git clone -q --depth 1 https://github.com/LuaJIT/LuaJIT.git "$SRC"
    make -C "$SRC" -j"$(nproc 2>/dev/null || echo 2)" PREFIX="$POB_HOME/luajit" >/dev/null
    make -C "$SRC" install PREFIX="$POB_HOME/luajit" >/dev/null
    # install creates a versioned binary + symlink; make sure "luajit" exists
    if [[ ! -e "$LUAJIT" ]]; then ln -sf "$(ls "$POB_HOME/luajit/bin" | grep '^luajit-' | head -1)" "$LUAJIT"; fi
  fi
fi
"$LUAJIT" -v

# 2) lua-utf8 native module (the only C module PoB needs headless) ---------
UTF8="$POB_HOME/luamods/lua-utf8.so"
utf8_ok() { (cd "$POB_HOME" && LUA_CPATH="$POB_HOME/luamods/?.so;;" "$LUAJIT" -e 'require("lua-utf8")' >/dev/null 2>&1); }
if ! utf8_ok; then
  if [[ "$(uname -m)" == "x86_64" && -f "$BUNDLED/lua-utf8.so" ]]; then
    cp "$BUNDLED/lua-utf8.so" "$UTF8"
  fi
  if ! utf8_ok; then
    log "Building lua-utf8 from source..."
    SRC="$POB_HOME/src-luautf8"
    rm -rf "$SRC"
    git clone -q --depth 1 https://github.com/starwing/luautf8.git "$SRC"
    INC="$(ls -d "$POB_HOME"/luajit/include/luajit-2.1 2>/dev/null || true)"
    if [[ -z "$INC" ]]; then
      # bundled LuaJIT has no headers; fetch them
      SRCJ="$POB_HOME/src-luajit"
      [[ -d "$SRCJ" ]] || git clone -q --depth 1 https://github.com/LuaJIT/LuaJIT.git "$SRCJ"
      INC="$SRCJ/src"
    fi
    gcc -O2 -shared -fPIC -I"$INC" "$SRC/lutf8lib.c" -o "$UTF8"
  fi
fi
utf8_ok && log "lua-utf8 OK"

# 3) Path of Building (sparse checkout: Lua + data only, no images) -------
POB="$POB_HOME/pob"
if [[ ! -f "$POB/src/HeadlessWrapper.lua" ]]; then
  log "Downloading Path of Building ($POB_REF), Lua and data only (~360 MB, about a minute)..."
  rm -rf "$POB"
  git clone -q --depth 1 --filter=blob:none --no-checkout --branch "$POB_REF" "$POB_REPO" "$POB"
  git -C "$POB" sparse-checkout init --no-cone
  cat > "$POB/.git/info/sparse-checkout" <<'EOF'
/src/
!/src/**/*.png
!/src/**/*.jpg
!/src/**/*.webp
!/src/**/*.dds
!/src/**/*.zst
/runtime/lua/
/manifest.xml
/LICENSE.md
/changelog.txt
EOF
  git -C "$POB" checkout -q
elif [[ "$UPDATE" == "1" ]]; then
  log "Updating Path of Building to latest $POB_REF..."
  git -C "$POB" fetch -q --depth 1 --filter=blob:none origin "$POB_REF"
  git -C "$POB" checkout -q FETCH_HEAD
fi
log "Path of Building: $(head -1 "$POB/changelog.txt" 2>/dev/null || echo unknown) @ $(git -C "$POB" log -1 --format='%h %cd' --date=short)"

# 4) Smoke test + name index for the preflight (one PoB start-up for both) --------
printf 'classes\n' | POB_HOME="$POB_HOME" python3 "$HERE/pob.py" batch - | sed -n '2,4p'
POB_HOME="$POB_HOME" python3 "$HERE/pob.py" warm
log "Ready."
