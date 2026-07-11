#!/usr/bin/env bash
# ดึงโค้ดแอปเวอร์ชันล่าสุดจากตัวจริงในเครื่อง (แอปตอบปัญหา = source of truth) มาลง repo นี้
# ใช้เมื่อแก้โค้ดแอปในเครื่องแล้วอยาก deploy ขึ้น cloud:
#   ./sync_from_local.sh && git add -A && git commit -m "sync app" && git push
set -e
SRC="$HOME/Desktop/คลังกฎหมาย ภาษี/กฎหมายภาษีสรรพากร/กฎหมายภาษีสรรพากร/แอปตอบปัญหา"
cp "$SRC/app.py" "$SRC/build_index.py" "$SRC/requirements.txt" .
cp "$SRC/templates/index.html" "$SRC/templates/login.html" "$SRC/templates/tree.html" templates/
mkdir -p pwa && cp "$SRC/pwa/"* pwa/     # PWA: manifest + service worker + ไอคอน
echo "synced — ดู git diff แล้ว commit + push (Render deploy อัตโนมัติ)"
