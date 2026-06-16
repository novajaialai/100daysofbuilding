#!/usr/bin/env bash
# One-time root setup: install ydotoold as a system service so ydotool can
# inject keystrokes via /dev/uinput (root-only). Run with:  sudo bash setup-ydotoold.sh
set -e
install -m 0644 /home/jake/voice/ydotoold.service /etc/systemd/system/ydotoold.service
modprobe uinput || true
systemctl daemon-reload
systemctl enable --now ydotoold.service
sleep 1
echo "--- status ---"
systemctl --no-pager --full status ydotoold.service | head -n 8
ls -l /run/ydotoold.sock
echo "Done. ydotoold is running; socket at /run/ydotoold.sock (owned by jake)."
