#!/usr/bin/env bash
# Render build: ติดตั้ง deps → clone คลังข้อมูล (repo private) → สร้าง search index
set -euo pipefail

pip install -r requirements.txt

rm -rf data
git clone --depth 1 \
  "https://x-access-token:${GITHUB_PR_TOKEN}@github.com/${GITHUB_DATA_REPO:-KK379724/klang-kotmai}.git" data

# ของที่ไม่ใช้บน cloud (บันทึกส่วนตัวขึ้นเว็บด้วย — user ยืนยัน 2026-07-09 ข้อมูลตรงกัน 100%)
rm -rf data/.edit_backups data/.trash "data/แอปตอบปัญหา" data/.git data/.github

LAW_DATA_ROOT=./data python build_index.py
