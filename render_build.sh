#!/usr/bin/env bash
# Render build: ติดตั้ง deps → clone คลังข้อมูล (repo private) → สร้าง search index
set -euo pipefail

pip install -r requirements.txt

rm -rf data
git clone --depth 1 \
  "https://x-access-token:${GITHUB_PR_TOKEN}@github.com/${GITHUB_DATA_REPO:-KK379724/klang-kotmai}.git" data

# ของที่ไม่ใช้บน cloud (บันทึกส่วนตัวขึ้นเว็บด้วย — user ยืนยัน 2026-07-09 ข้อมูลตรงกัน 100%)
rm -rf data/.edit_backups data/.trash "data/แอปตอบปัญหา" data/.git data/.github

# semantic index (vector cache) จาก GitHub Release — โหมด Vector/Hybrid บนเว็บ
# ถ้าโหลดไม่ได้ไม่เป็นไร แอป fallback ปิดโหมด semantic เอง
REL_JSON=$(curl -s -H "Authorization: Bearer ${GITHUB_PR_TOKEN}" \
  "https://api.github.com/repos/${GITHUB_DATA_REPO:-KK379724/klang-kotmai}/releases/tags/vec-latest" || true)
for NAME in vec_cache.npz vec_payloads.json.gz; do
  URL=$(echo "$REL_JSON" | python -c "import sys,json
try:
    assets = json.load(sys.stdin).get('assets', [])
    print(next((a['url'] for a in assets if a['name'] == '$NAME'), ''))
except Exception:
    print('')")
  if [ -n "$URL" ]; then
    curl -sL -H "Authorization: Bearer ${GITHUB_PR_TOKEN}" -H "Accept: application/octet-stream" \
      "$URL" -o "data/.$NAME" && echo "โหลด $NAME แล้ว" || true
  fi
done

LAW_DATA_ROOT=./data python build_index.py
