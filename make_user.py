#!/usr/bin/env python3
"""สร้าง hash รหัสผ่านสำหรับบัญชีทีมแก้ไข (env EDITOR_USERS)

ใช้:  python3 make_user.py ชื่อผู้ใช้
แล้วพิมพ์รหัสผ่าน — ได้บรรทัด 'user=hash' ไปวางใน EDITOR_USERS บน Render
หลายคนคั่นด้วย | เช่น:  alice=pbkdf2:...|bob=pbkdf2:...
"""
import sys
import getpass
from werkzeug.security import generate_password_hash

user = sys.argv[1] if len(sys.argv) > 1 else input("ชื่อผู้ใช้: ").strip()
pw = getpass.getpass("รหัสผ่าน: ")
pw2 = getpass.getpass("ยืนยันรหัสผ่าน: ")
if pw != pw2:
    sys.exit("รหัสผ่านไม่ตรงกัน")
if len(pw) < 8:
    sys.exit("รหัสผ่านต้องยาวอย่างน้อย 8 ตัวอักษร")
print(f"\n{user}={generate_password_hash(pw)}")
print("\n→ คัดลอกบรรทัดบนไปวางใน EDITOR_USERS (Render dashboard → Environment)")
