# ค้นหากฎหมายภาษีสรรพากร (Thai Tax Law Search)

เว็บแอปค้นหากฎหมายภาษีของกรมสรรพากร 7,000+ ฉบับ — ประมวลรัษฎากร, พระราชกฤษฎีกา, กฎกระทรวง, ประกาศอธิบดี, ข้อหารือ, คำพิพากษาฎีกา ฯลฯ พร้อมระบบดูสายกฎหมายแม่-ลูก, สถานะถูกยกเลิก/แก้ไขเพิ่มเติม และโหมดแก้ไขข้อมูลสำหรับทีมงาน

> ส่วนหนึ่งของบริการ **Apinya Advisory** — ที่ปรึกษาภาษีโดยทีมงานสายภาษีตัวจริง

## สถาปัตยกรรม

```
repo นี้ (โค้ดแอป, public)          repo ข้อมูล (JSON corpus, private)
        │                                   │
        └────── Render build ───── clone ───┘
                     ↓
        build_index.py → rulings.db (SQLite FTS5)
                     ↓
        Flask app — ค้นหา BM25 (สาธารณะ) + โหมดแก้ไข (ต้อง login)
                     ↓ เมื่อทีมงานกดบันทึก
        สร้าง Pull Request เข้า repo ข้อมูลอัตโนมัติ → เจ้าของตรวจ diff → Merge
```

การแก้ไขจากหน้าเว็บ**ไม่แตะข้อมูลจริงโดยตรง** — ทุกการแก้ไขกลายเป็น Pull Request ให้เจ้าของคลังตรวจก่อนเสมอ

## Deploy บน Render (ฟรี)

1. Fork/ใช้ repo นี้ → Render dashboard → **New → Blueprint** → เลือก repo นี้ (อ่าน `render.yaml` อัตโนมัติ)
2. ใส่ env vars 2 ตัวที่เหลือใน dashboard:
   - `GITHUB_PR_TOKEN` — GitHub fine-grained token จำกัดเฉพาะ repo ข้อมูล, สิทธิ์ **Contents: Read-write** + **Pull requests: Read-write**
   - `EDITOR_USERS` — บัญชีทีมแก้ไข รูปแบบ `user=<hash>|user2=<hash>` (สร้าง hash: `python3 make_user.py ชื่อ`)
3. Deploy — build ใช้เวลา ~5 นาที (clone ข้อมูล + สร้าง index)

**อัปเดตข้อมูลบนเว็บ:** ข้อมูลถูก clone ตอน build — เมื่อ repo ข้อมูลมีของใหม่ (merge PR/scraper รายคืน) ให้กด **Manual Deploy** หรือยิง Deploy Hook

## Env vars

| ตัวแปร | ค่า | ความหมาย |
|---|---|---|
| `EDIT_BACKEND` | `github` | โหมดแก้ไข: สร้าง PR (cloud) / `local` = เขียนไฟล์ตรง จำกัด localhost |
| `DISABLE_AI` | `1` | ปิดโหมด AI ค้นหา/AI ตอบ/แชท (ต้องใช้ API keys เพิ่ม) |
| `DISABLE_SEMANTIC` | `1` | ปิดโหมด Vector/Hybrid (ต้องใช้ vector index เพิ่ม) |
| `LAW_DATA_ROOT` | `./data` | โฟลเดอร์คลัง JSON |
| `GITHUB_DATA_REPO` | `owner/repo` | repo ข้อมูล (private ได้) |
| `GITHUB_DATA_BRANCH` | `main` | branch หลักของ repo ข้อมูล |
| `GITHUB_PR_TOKEN` | secret | token สำหรับ clone ข้อมูล + สร้าง PR |
| `EDITOR_USERS` | secret | บัญชีทีมแก้ไข `user=hash\|user=hash` |
| `SECRET_KEY` | secret | Flask session key |

## รันในเครื่อง (โหมดเจ้าของคลัง)

```bash
pip install -r requirements.txt
python3 build_index.py        # ต้องมีคลัง JSON ในโฟลเดอร์แม่ (ดู schema ใน repo ข้อมูล)
python3 app.py                # http://127.0.0.1:5001 — แก้ไขได้เลยไม่ต้อง login (จำกัด localhost)
```

โค้ดตัวจริงพัฒนาในเครื่องเจ้าของโปรเจค — repo นี้ sync ด้วย `sync_from_local.sh`

## ข้อจำกัดความรับผิดชอบ

ข้อมูลรวบรวมจากแหล่งเผยแพร่สาธารณะของกรมสรรพากร (rd.go.th) เพื่อความสะดวกในการค้นคว้าเท่านั้น ไม่ใช่คำแนะนำทางกฎหมาย — โปรดตรวจสอบกับต้นฉบับ (มีลิงก์ในทุกเอกสาร) ก่อนนำไปใช้อ้างอิง
