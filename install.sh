#!/usr/bin/env bash
# Installation script for KDE Custom Touchpad Drivers
# Usage: ./install.sh [--no-enable]  (--no-enable skips systemctl enable)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "=== KDE Custom Touchpad Driver — Installer ==="
echo "Project directory: $PROJECT_DIR"
echo ""

# 1. Create Python virtual environment
echo "[1/4] Creating Python virtual environment..."
python3 -m venv .venv
echo "      Virtual environment created at $PROJECT_DIR/.venv"

# 2. Install Python dependencies
echo "[2/4] Installing Python dependencies..."
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt --quiet
echo "      Dependencies installed."

# 3. Install systemd user service
echo "[3/4] Installing systemd user service..."
SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"

# Substitute the project path into the service file so it points to this clone
sed "s|%h/Projects/kde-custom-touchpad-drivers|$PROJECT_DIR|g" \
    systemd/kde-custom-touchpad.service \
    > "$SERVICE_DIR/kde-custom-touchpad.service"

echo "      Service installed to $SERVICE_DIR/kde-custom-touchpad.service"

# 4. Permission checks
echo "[4/4] Checking system permissions..."

MISSING_PERMS=0

if ! groups | grep -q '\binput\b'; then
    MISSING_PERMS=1
    echo ""
    echo "  ⚠️  WARNING: '$USER' is not in the 'input' group."
    echo "     To read touchpad devices without root, run:"
    echo "       sudo usermod -aG input $USER"
    echo "     Then log out and back in (or reboot)."
fi

UDEV_RULE="/etc/udev/rules.d/99-uinput.rules"
if [ ! -f "$UDEV_RULE" ]; then
    MISSING_PERMS=1
    echo ""
    echo "  ⚠️  WARNING: udev rule for /dev/uinput not found."
    echo "     To allow virtual key/mouse injection, create it with:"
    echo "       echo 'KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\", OPTIONS+=\"static_node=uinput\"' \\"
    echo "         | sudo tee $UDEV_RULE"
    echo "       sudo udevadm control --reload-rules && sudo udevadm trigger"
fi

if [ "$MISSING_PERMS" -eq 0 ]; then
    echo "      All permissions look good."
fi

# Enable and optionally start the service
echo ""
echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

if [[ "${1:-}" != "--no-enable" ]]; then
    systemctl --user enable kde-custom-touchpad.service
    echo ""
    echo "=== Installation complete ==="
    echo ""
    echo "Start the daemon now:"
    echo "  systemctl --user start kde-custom-touchpad.service"
    echo ""
    echo "Watch live logs:"
    echo "  journalctl --user -u kde-custom-touchpad.service -f"
    echo ""
    echo "Run a one-shot verbose test (Ctrl+C to stop):"
    echo "  $PROJECT_DIR/.venv/bin/python precision_daemon.py --verbose"
else
    echo ""
    echo "=== Installation complete (service not enabled) ==="
fi
