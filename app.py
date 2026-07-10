"""
app.py — Flask search server สำหรับค้นหาข้อหารือภาษีอากร
เปิด http://127.0.0.1:5000 ในเบราว์เซอร์หลังจากรันไฟล์นี้
"""
import sqlite3, os, json, re, urllib.request, urllib.error, hashlib, time, threading, sys, glob
from flask import Flask, request, jsonify, render_template, Response, stream_with_context, session

try:
    from pythainlp.tokenize import word_tokenize
    HAS_THAI = True
except ImportError:
    HAS_THAI = False

try:
    from duckduckgo_search import DDGS
    HAS_DDG = True
except ImportError:
    HAS_DDG = False

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY') or 'local-dev-only-secret'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# LAW_DATA_ROOT: โฟลเดอร์คลัง JSON — บนเครื่อง user = โฟลเดอร์แม่ของแอป, บน cloud = data repo ที่ clone มา
DATA_ROOT = os.path.abspath(os.getenv('LAW_DATA_ROOT') or os.path.join(BASE_DIR, '..'))
DB_PATH  = os.getenv('LAW_DB_PATH') or os.path.join(BASE_DIR, 'rulings.db')
JSON_DIR = os.path.join(DATA_ROOT, 'ข้อหารือ 2569-2540')

# ── Deployment mode ───────────────────────────────────────────────────────────
# EDIT_BACKEND: 'local' (default) = แก้ไฟล์ตรง จำกัด localhost | 'github' = ต้อง login,
#               การแก้ไขถูกส่งเป็น Pull Request เข้า repo ข้อมูล (ใช้บน cloud)
EDIT_BACKEND     = os.getenv('EDIT_BACKEND', 'local')
DISABLE_AI       = os.getenv('DISABLE_AI') == '1'         # ปิดโหมด AI ค้นหา/AI ตอบ/แชท
DISABLE_SEMANTIC = os.getenv('DISABLE_SEMANTIC') == '1'   # ปิดโหมด Vector/Hybrid
DISABLED_MODES = (['ai', 'answer', 'chat'] if DISABLE_AI else []) + \
                 (['semantic', 'hybrid'] if DISABLE_SEMANTIC else [])

# Lookup ทุก JSON ใน repo — ใช้โดย detail endpoint
def _build_json_lookup() -> dict[str, str]:
    # หมายเหตุ: ต้องรวม 'บันทึกส่วนตัว' ด้วย ไม่งั้นแก้/ลบบันทึกส่วนตัวหลัง restart ไม่ได้ (หา fp ไม่เจอ)
    _EXCLUDE = {'__pycache__', 'ข้อหารือ-search', 'ตัวจัดระเบียบไฟล json',
                'url30662', 'แอปตอบปัญหา'}
    _YEAR_RE = re.compile(r'^\d{4}$')
    lookup: dict[str, str] = {}
    base = DATA_ROOT
    try:
        for name in os.listdir(base):
            if name in _EXCLUDE or name.startswith('.') or _YEAR_RE.match(name):
                continue
            folder = os.path.join(base, name)
            if not os.path.isdir(folder):
                continue
            for fp in glob.glob(os.path.join(folder, '*.json')):
                stem = os.path.splitext(os.path.basename(fp))[0]
                lookup[stem] = fp
    except Exception:
        pass
    return lookup

_JSON_LOOKUP: dict[str, str] = _build_json_lookup()

PER_PAGE = 20

# ── ลำดับศักดิ์กฎหมาย ────────────────────────────────────────────────────────
# ใช้สำหรับจัดกลุ่มเอกสารใน AI prompt และแสดง label
DOC_HIERARCHY: dict[str, tuple[int, str]] = {
    'law_section':                   (1, 'มาตราประมวลรัษฎากร'),
    'royal_decree':                  (2, 'พระราชกฤษฎีกา'),
    'ministerial_regulation':        (3, 'กฎกระทรวง'),
    'ministry_notification':         (4, 'ประกาศกระทรวงการคลัง'),
    'ministry_order':                (4, 'คำสั่งกระทรวงการคลัง'),
    'director_general_notification': (5, 'ประกาศอธิบดีกรมสรรพากร'),
    'notification':                  (5, 'ประกาศอธิบดีกรมสรรพากร'),
    'department_notification':       (5, 'ประกาศกรมสรรพากร'),
    'department_order':              (5, 'คำสั่งกรมสรรพากร'),
    'committee_ruling':              (6, 'คำวินิจฉัยคณะกรรมการวินิจฉัยภาษีอากร'),
    'court_judgment':                (7, 'คำพิพากษาศาลภาษีอากร'),
    'ruling':                        (8, 'ข้อหารือกรมสรรพากร'),
    'regulation':                    (5, 'ระเบียบ/แนวปฏิบัติ'),
}

def doc_hierarchy_level(doc_type: str) -> int:
    return DOC_HIERARCHY.get(doc_type, (9,))[0]

# SQL fragment สำหรับ ORDER BY
# 1) ลำดับศักดิ์กฎหมาย (primary)
# 2) เลขฉบับ/เลขมาตรา จาก ID (ฉบับ 123 ก่อน 225 เสมอ)
# 3) law_section suffix: ทวิ=2, ตรี=3, จัตวา=4, เบญจ=5, ฉ=6, สัตต=7, อัฏฐ=8, นว=9, ทศ=10
# 4) BM25 relevance (tiebreaker)
_HIERARCHY_ORDER = """(CASE m.doc_type
    WHEN 'law_section'                   THEN 1
    WHEN 'royal_decree'                  THEN 2
    WHEN 'ministerial_regulation'        THEN 3
    WHEN 'ministry_notification'         THEN 4
    WHEN 'ministry_order'                THEN 4
    WHEN 'director_general_notification' THEN 5
    WHEN 'notification'                  THEN 5
    WHEN 'department_notification'       THEN 5
    WHEN 'department_order'              THEN 5
    WHEN 'regulation'                    THEN 5
    WHEN 'committee_ruling'              THEN 6
    WHEN 'court_judgment'                THEN 7
    WHEN 'ruling'                        THEN 8
    ELSE 9 END) ASC,
(CASE
    WHEN m.id LIKE 'section-%'            THEN CAST(SUBSTR(m.id,9)  AS INTEGER)
    WHEN m.id LIKE 'royal-decree-%'       THEN CAST(SUBSTR(m.id,14) AS INTEGER)
    WHEN m.id LIKE 'mr%'                  THEN CAST(SUBSTR(m.id,3)  AS INTEGER)
    WHEN m.id LIKE 'dgsb%'               THEN CAST(SUBSTR(m.id,5)  AS INTEGER)
    WHEN m.id LIKE 'dgvat%'              THEN CAST(SUBSTR(m.id,6)  AS INTEGER)
    WHEN m.id LIKE 'dggs%'               THEN CAST(SUBSTR(m.id,5)  AS INTEGER)
    WHEN m.id LIKE 'dgg%'                THEN CAST(SUBSTR(m.id,4)  AS INTEGER)
    WHEN m.id LIKE 'dgs%'                THEN CAST(SUBSTR(m.id,4)  AS INTEGER)
    WHEN m.id LIKE 'dg%'                 THEN CAST(SUBSTR(m.id,3)  AS INTEGER)
    WHEN m.id LIKE 'rdord_%'             THEN CAST(SUBSTR(m.id,9)  AS INTEGER)
    WHEN m.id LIKE 'committee-ruling-%'  THEN CAST(SUBSTR(m.id,18) AS INTEGER)
    WHEN m.id LIKE 'mof_mfc%'            THEN CAST(SUBSTR(m.id,8)  AS INTEGER)
    ELSE 99999 END) ASC,
(CASE WHEN m.doc_type='law_section' THEN
    CASE
        WHEN m.id LIKE '%-ทวิ'   THEN 2
        WHEN m.id LIKE '%-ตรี'   THEN 3
        WHEN m.id LIKE '%-จัตวา' THEN 4
        WHEN m.id LIKE '%-เบญจ'  THEN 5
        WHEN m.id LIKE '%-ฉ'     THEN 6
        WHEN m.id LIKE '%-สัตต'  THEN 7
        WHEN m.id LIKE '%-อัฏฐ'  THEN 8
        WHEN m.id LIKE '%-นว'    THEN 9
        WHEN m.id LIKE '%-ทศ'    THEN 10
        ELSE 1 END
ELSE 0 END) ASC,
fts.rank ASC"""

# ลำดับศักดิ์กฎหมายนำก่อน → ในศักดิ์เดียวกันใหม่→เก่า (ยกเว้นประมวลฯ เรียงตามมาตรา) → BM25
# กฎหมายแม่ (1-3) → กฎหมายลูก (4-5) → คำวินิจฉัย/ฎีกา (6-7) → ข้อหารือ (8)
# (ตกลงกับ user 2026-07-09: "ศักดิ์ก่อน + ใหม่→เก่า ยกเว้นประมวลรัษฎากรตามมาตรา")
_RELEVANCE_ORDER = """(CASE m.doc_type
    WHEN 'law_section'                   THEN 1
    WHEN 'royal_decree'                  THEN 2
    WHEN 'ministerial_regulation'        THEN 3
    WHEN 'ministry_notification'         THEN 4
    WHEN 'ministry_order'                THEN 4
    WHEN 'director_general_notification' THEN 5
    WHEN 'notification'                  THEN 5
    WHEN 'department_notification'       THEN 5
    WHEN 'department_order'              THEN 5
    WHEN 'regulation'                    THEN 5
    WHEN 'committee_ruling'              THEN 6
    WHEN 'court_judgment'                THEN 7
    WHEN 'ruling'                        THEN 8
    ELSE 9 END) ASC,
(CASE WHEN m.doc_type='law_section' AND m.id LIKE 'section-%'
    THEN CAST(SUBSTR(m.id,9) AS INTEGER) ELSE 0 END) ASC,
(CASE WHEN m.doc_type='law_section' THEN
    CASE
        WHEN m.id LIKE '%-ทวิ'   THEN 2
        WHEN m.id LIKE '%-ตรี'   THEN 3
        WHEN m.id LIKE '%-จัตวา' THEN 4
        WHEN m.id LIKE '%-เบญจ'  THEN 5
        WHEN m.id LIKE '%-ฉ'     THEN 6
        WHEN m.id LIKE '%-สัตต'  THEN 7
        WHEN m.id LIKE '%-อัฏฐ'  THEN 8
        WHEN m.id LIKE '%-นว'    THEN 9
        WHEN m.id LIKE '%-ทศ'    THEN 10
        ELSE 1 END
ELSE 0 END) ASC,
(CASE WHEN m.doc_type='law_section' THEN 0 ELSE m.year END) DESC,
(CASE WHEN m.doc_type='law_section' THEN '' ELSE COALESCE(m.date,'') END) DESC,
(CASE
    WHEN m.doc_type='law_section'        THEN 0
    WHEN m.id LIKE 'royal-decree-%'       THEN CAST(SUBSTR(m.id,14) AS INTEGER)
    WHEN m.id LIKE 'mr%'                  THEN CAST(SUBSTR(m.id,3)  AS INTEGER)
    WHEN m.id LIKE 'dgsb%'               THEN CAST(SUBSTR(m.id,5)  AS INTEGER)
    WHEN m.id LIKE 'dgvat%'              THEN CAST(SUBSTR(m.id,6)  AS INTEGER)
    WHEN m.id LIKE 'dggs%'               THEN CAST(SUBSTR(m.id,5)  AS INTEGER)
    WHEN m.id LIKE 'dgg%'                THEN CAST(SUBSTR(m.id,4)  AS INTEGER)
    WHEN m.id LIKE 'dgs%'                THEN CAST(SUBSTR(m.id,4)  AS INTEGER)
    WHEN m.id LIKE 'dg%'                 THEN CAST(SUBSTR(m.id,3)  AS INTEGER)
    WHEN m.id LIKE 'rdord_%'             THEN CAST(SUBSTR(m.id,9)  AS INTEGER)
    WHEN m.id LIKE 'committee-ruling-%'  THEN CAST(SUBSTR(m.id,18) AS INTEGER)
    WHEN m.id LIKE 'mof_mfc%'            THEN CAST(SUBSTR(m.id,8)  AS INTEGER)
    ELSE 0 END) DESC,
rank ASC"""

def doc_type_label(doc_type: str) -> str:
    return DOC_HIERARCHY.get(doc_type, (9, doc_type))[1]

# ── Synonym mapping สำหรับ query expansion ──────────────────────────────────
TAX_SYNONYMS: dict[str, list[str]] = {
    # ภาษีหลัก
    'ภาษีมูลค่าเพิ่ม':        ['VAT'],
    'VAT':                     ['ภาษีมูลค่าเพิ่ม'],
    'ภาษีเงินได้นิติบุคคล':  ['CIT'],
    'CIT':                     ['ภาษีเงินได้นิติบุคคล'],
    'ภาษีเงินได้บุคคลธรรมดา': ['PIT'],
    'PIT':                     ['ภาษีเงินได้บุคคลธรรมดา'],
    'อากรแสตมป์':             ['Stamp', 'อากรสแตมป์'],
    'อากรสแตมป์':             ['Stamp', 'อากรแสตมป์'],
    'Stamp':                   ['อากรแสตมป์', 'อากรสแตมป์'],
    'ภาษีธุรกิจเฉพาะ':       ['SBT'],
    'SBT':                     ['ภาษีธุรกิจเฉพาะ'],
    # WHT
    'WHT':             ['หัก ณ ที่จ่าย', 'ภาษีหัก', 'หักภาษี'],
    'ภาษีหัก':        ['WHT', 'หัก ณ ที่จ่าย'],
    'หักภาษี':        ['WHT', 'หัก ณ ที่จ่าย'],
    'หัก ณ ที่จ่าย': ['WHT', 'ภาษีหัก', 'หักภาษี'],
    # รายได้และธุรกรรม
    'เงินปันผล':        ['dividend'],
    'dividend':         ['เงินปันผล'],
    'ดอกเบี้ย':        ['interest'],
    'interest':         ['ดอกเบี้ย'],
    'ค่าลิขสิทธิ์':   ['royalty'],
    'royalty':          ['ค่าลิขสิทธิ์'],
    'ค่าเช่า':         ['rent'],
    'rent':             ['ค่าเช่า'],
    'เงินเดือน':       ['ค่าจ้าง'],
    'ค่าจ้าง':        ['เงินเดือน'],
    # เอกสาร
    'ใบกำกับภาษี':    ['tax invoice', 'invoice'],
    'invoice':         ['ใบกำกับภาษี'],
    # การลดหย่อน
    'ค่าลดหย่อน':     ['deduction'],
    'deduction':       ['ค่าลดหย่อน'],
    # ทรัพย์สิน
    'อสังหาริมทรัพย์': ['property'],
    'property':         ['อสังหาริมทรัพย์'],
    # การค้าระหว่างประเทศ
    'ส่งออก': ['export'],
    'export':  ['ส่งออก'],
    'นำเข้า': ['import'],
    'import':  ['นำเข้า'],
    # อื่นๆ
    'ประกันชีวิต': ['insurance'],
    'insurance':   ['ประกันชีวิต'],
    'กำไร':       ['profit'],
    'profit':     ['กำไร'],
    'ขาดทุน':    ['loss'],
    'loss':       ['ขาดทุน'],
}

# ── Synonym เฉพาะสำหรับค้นหา ข้อหารือ — ground ด้วย vocabulary จริงใน corpus ──
# ใช้ขยาย query ก่อน OR-search เฉพาะ doc_type='ruling'
# เน้นคำที่พบบ่อยใน title/ruling_text ของข้อหารือจริง
RULING_QUERY_EXPAND: dict[str, list[str]] = {
    # การออก/ถอนทะเบียน VAT
    'ออกจาก':         ['ถอนทะเบียน', 'เลิกประกอบกิจการ', 'แจ้งเลิก'],
    'ออก':            ['ถอน', 'เลิก', 'แจ้งเลิก', 'ถอนทะเบียน'],
    'เลิก':           ['ถอนทะเบียน', 'เลิกประกอบกิจการ', 'ยกเลิกทะเบียน', 'แจ้งเลิก'],
    'ถอน':            ['ถอนทะเบียน', 'เลิกกิจการ', 'เลิกประกอบกิจการ'],
    'ยกเลิก':         ['ถอนทะเบียน', 'เลิกประกอบกิจการ', 'แจ้งเลิก'],
    'จดทะเบียน':      ['ทะเบียนภาษีมูลค่าเพิ่ม', 'ผู้ประกอบการจดทะเบียน'],
    'ทะเบียน':        ['จดทะเบียน', 'ถอนทะเบียน', 'แจ้งการเปลี่ยนแปลงทะเบียน'],
    'ภพ09':           ['ภ.พ.09', 'แบบแจ้งการเปลี่ยนแปลงทะเบียน'],
    'เกณฑ์':          ['เงื่อนไข', 'หลักเกณฑ์', 'คุณสมบัติ'],
    'รายได้':         ['รายรับ', 'เงินได้', 'มูลค่า'],
    # การลดหย่อน/ยกเว้น
    'ลดหย่อน':        ['หักลดหย่อน', 'ยกเว้น', 'ค่าลดหย่อน', 'หัก'],
    'ยกเว้น':         ['ลดหย่อน', 'ยกเว้นภาษีมูลค่าเพิ่ม', 'ยกเว้นภาษี', 'ได้รับยกเว้น'],
    'คืนภาษี':        ['ขอคืน', 'ขอคืนภาษี', 'คืนเงินภาษี'],
    # ประเภทกิจการ/ผู้ประกอบการ
    'บริษัท':         ['นิติบุคคล', 'ห้างหุ้นส่วน', 'ผู้ประกอบการ'],
    'บุคคลธรรมดา':   ['บุคคล', 'ผู้ประกอบการ'],
    'นายจ้าง':        ['ผู้จ่ายเงิน', 'ผู้จ่าย'],
    'ลูกจ้าง':        ['ผู้รับ', 'พนักงาน', 'ผู้รับเงิน'],
    # ธุรกรรม
    'ส่งออก':         ['export', 'ส่งสินค้าออก', 'ส่งออกนอกราชอาณาจักร'],
    'นำเข้า':         ['import', 'นำสินค้าเข้า'],
    'ขาย':            ['โอน', 'จำหน่าย', 'จ่าย'],
    'ซื้อ':           ['รับโอน', 'ซื้อขาย'],
    'เช่า':           ['ค่าเช่า', 'สัญญาเช่า', 'เช่าซื้อ'],
    # เอกสาร
    'ใบกำกับ':        ['ใบกำกับภาษี', 'tax invoice'],
    'ภ.พ.30':         ['แบบภาษีมูลค่าเพิ่ม', 'ยื่นแบบ'],
}

# ── AI Response Cache (in-memory, TTL 1 hour) ─────────────────────────────────
_ai_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 3600

def _cache_key(*args) -> str:
    raw = json.dumps(args, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()

def _cache_get(key: str):
    entry = _ai_cache.get(key)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    if entry:
        del _ai_cache[key]
    return None

def _cache_set(key: str, val: dict):
    _ai_cache[key] = (time.time(), val)


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def tokenize_query(q: str) -> list[str]:
    """แยก query ออกเป็น list ของ token"""
    if HAS_THAI:
        tokens = word_tokenize(q.strip(), engine='newmm', keep_whitespace=False)
        return [t.strip() for t in tokens if t and t.strip() and len(t.strip()) >= 1]
    # fallback: split by space + strip punctuation
    return [t.strip() for t in q.split() if t.strip()]


def build_fts_query(tokens: list[str], mode='AND') -> str:
    """สร้าง FTS5 query string"""
    quoted = [f'"{t}"' for t in tokens]
    if mode == 'AND':
        return ' '.join(quoted)
    return ' OR '.join(quoted)


def get_synonym_extras(tokens: list[str]) -> list[str]:
    """คืน token เพิ่มเติมจาก TAX_SYNONYMS เพื่อ OR-expansion"""
    extras = []
    seen = set(tokens)
    for t in tokens:
        for syn in TAX_SYNONYMS.get(t, []):
            if syn not in seen:
                extras.append(syn)
                seen.add(syn)
    return extras


def get_ruling_expand_extras(tokens: list[str]) -> list[str]:
    """คืน token เพิ่มเติมจาก RULING_QUERY_EXPAND — ใช้เฉพาะตอน search ข้อหารือ"""
    extras = []
    seen = set(tokens)
    for t in tokens:
        for syn in RULING_QUERY_EXPAND.get(t, []):
            if syn not in seen:
                extras.append(syn)
                seen.add(syn)
    return extras


def _format_ruling_rows(rows) -> list[dict]:
    result = []
    for r in rows:
        snippet = (r['ruling_text'] or r['facts'] or '')[:250].replace('\n', ' ')
        result.append({
            'id':       r['id'],
            'ref':      r['ref_number'],
            'title':    r['title'],
            'year':     r['year'],
            'tax_type': r['tax_type'].split(',') if r['tax_type'] else [],
            'snippet':  snippet,
        })
    return result


def find_related_rulings(q: str, db, limit: int = 5) -> list[dict]:
    """หาข้อหารือ (doc_type='ruling') ที่ใกล้เคียงกับ query

    Strategy:
    - Precision: AND search ต่อ synonym แต่ละตัว + tax context จาก query
      เช่น 'ถอนทะเบียน' → ["ถอน","ทะเบียน","ภาษีมูลค่าเพิ่ม"]
      เพราะ FTS5 trigram + PyThaiNLP เก็บ word ละตัว (ไม่ใช่ compound phrase)
    - Recall fallback: OR search กว้างขึ้น ถ้า precision ได้น้อย
    - Deduplicate by ref_number
    """
    tokens = tokenize_query(q)
    if not tokens:
        return []

    _STOP = {'ที่', 'และ', 'ใน', 'มี', 'ได้', 'ไม่', 'ของ', 'การ', 'กี่', 'วัน',
             'จะ', 'ต้อง', 'ไร', 'เมื่อ', 'ระบบ', 'จาก', 'โดย', 'หรือ', 'ก็', 'แล้ว',
             'ออก', 'ออกจาก', 'อยาก', 'ต้องการ'}

    # tax context: token ยาวพอ ไม่ใช่ stop word → ใช้ anchor AND search ให้ตรงหมวดภาษี
    _tax_context = [t for t in tokens if t not in _STOP and len(t) >= 4][:3]

    seen_refs: set[str] = set()
    results: list = []

    def _run(fts_q: str, fetch: int):
        try:
            return db.execute(
                'SELECT m.id, m.ref_number, m.title, m.year, m.tax_type,'
                ' m.date, m.source_url, m.facts, m.ruling_text, MIN(fts.rank) as rank'
                ' FROM fts JOIN meta m ON fts.doc_id = m.id'
                ' WHERE fts MATCH ? AND m.doc_type = ?'
                ' GROUP BY m.id'
                ' ORDER BY rank ASC LIMIT ?',
                [fts_q, 'ruling', fetch]
            ).fetchall()
        except Exception:
            return []

    def _add(rows):
        for r in rows:
            ref = r['ref_number'] or r['id']
            if ref not in seen_refs and len(results) < limit:
                seen_refs.add(ref)
                results.append(r)

    # Step 1: precision — AND(synonym_tokens + tax_context) ต่อ synonym แต่ละตัว
    ruling_extras = get_ruling_expand_extras(tokens)
    for syn in ruling_extras[:8]:
        syn_toks = [t for t in tokenize_query(syn) if t not in _STOP and len(t) >= 2]
        if not syn_toks:
            continue
        # รวม synonym tokens + tax context เพื่อ anchor ให้ตรงหมวดภาษีที่ถาม
        and_toks = list(dict.fromkeys(syn_toks + _tax_context))
        fts_q = build_fts_query(and_toks, 'AND')
        _add(_run(fts_q, limit * 2))
        if len(results) >= limit:
            break

    # Step 2: recall — OR search ถ้าได้น้อย
    if len(results) < limit:
        meaningful = [t for t in tokens if t not in _STOP and len(t) >= 2]
        syn_flat = list(dict.fromkeys(
            t for syn in ruling_extras[:6]
            for t in tokenize_query(syn)
            if t not in _STOP and len(t) >= 2
        ))
        expanded = list(dict.fromkeys(
            meaningful + get_synonym_extras(tokens) + syn_flat
        ))
        _add(_run(build_fts_query(expanded, 'OR'), limit * 3))

    return _format_ruling_rows(results[:limit])


def _enrich_from_json(row_dict: dict) -> dict:
    """อ่าน full content จากไฟล์ JSON สำหรับ candidate — แก้ค่าที่ถูก truncate ใน meta"""
    fp = _JSON_LOOKUP.get(row_dict.get('id', ''))
    if not fp:
        return row_dict
    try:
        with open(fp, encoding='utf-8') as f:
            jd = json.load(f)
        content = jd.get('content', {}) or {}
        dt = row_dict.get('doc_type', '')
        if dt == 'ruling':
            row_dict['facts']       = content.get('ข้อเท็จจริง', '') or content.get('ข้อหารือ', '') or row_dict.get('facts') or ''
            row_dict['ruling_text'] = content.get('แนววินิจฉัย', '') or row_dict.get('ruling_text') or ''
        elif dt in ('director_general_notification', 'notification', 'royal_decree',
                    'ministerial_regulation', 'department_notification', 'department_order',
                    'ministry_notification', 'committee_ruling'):
            row_dict['ruling_text'] = content.get('full_text', '') or content.get('บทบัญญัติ', '') or row_dict.get('ruling_text') or ''
            row_dict['facts']       = content.get('อาศัยอำนาจตาม', '') or row_dict.get('facts') or ''
        elif dt == 'law_section':
            row_dict['ruling_text'] = content.get('text', '') or row_dict.get('ruling_text') or ''
            row_dict['facts']       = content.get('explanation', '') or row_dict.get('facts') or ''
    except Exception:
        pass
    return row_dict


def extract_json(text: str) -> dict:
    """ดึง JSON จาก response ของ AI — รองรับ markdown code block และข้อความปน"""
    text = text.strip()
    # Strip markdown code block
    if '```' in text:
        text = re.sub(r'^```[a-z]*\n?', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n?```', '', text)
        text = text.strip()
    # ลอง parse ตรง
    try:
        return json.loads(text)
    except Exception:
        pass
    # หา JSON object {...} แรกที่เจอในข้อความ — ใช้ balanced-bracket เพื่อกัน over-match
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start != -1:
                candidate = text[start:i+1]
                try:
                    return json.loads(candidate)
                except Exception:
                    start = -1
    raise ValueError(f'ไม่พบ JSON ใน response: {text[:300]}')


def highlight_snippet(text: str, tokens: list[str], length=280) -> str:
    """ตัดข้อความให้สั้น + เน้น token ที่ค้นหา"""
    if not text:
        return ''
    # Normalize whitespace: แปลง newline/tab/NBSP + ช่องว่างซ้ำ → space เดียว
    text = text.replace('\xa0', ' ')
    text = re.sub(r'[\r\n\t]+', ' ', text)
    text = re.sub(r' {2,}', ' ', text)
    # หาตำแหน่งแรกที่มี token
    pos = 0
    for t in tokens:
        idx = text.find(t)
        if idx != -1:
            pos = max(0, idx - 60)
            break
    snippet = text[pos: pos + length]
    if pos > 0:
        snippet = '…' + snippet
    if pos + length < len(text):
        snippet += '…'
    # Highlight
    for t in tokens:
        if len(t) >= 2:
            snippet = snippet.replace(t, f'<mark>{t}</mark>')
    return snippet


@app.route('/')
def index():
    return render_template('index.html', disabled_modes=DISABLED_MODES)


# ── Auth (ใช้เมื่อ EDIT_BACKEND=github — ทีมแก้ไขต้อง login ก่อน) ─────────────
def _editors() -> dict:
    """EDITOR_USERS format: 'user=<hash>|user2=<hash>' — hash สร้างด้วย make_user.py"""
    out = {}
    for pair in (os.getenv('EDITOR_USERS') or '').split('|'):
        u, _, h = pair.strip().partition('=')
        if u and h:
            out[u] = h
    return out


@app.route('/login')
def login_page():
    return render_template('login.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    from werkzeug.security import check_password_hash
    b = request.get_json(force=True, silent=True) or {}
    u = (b.get('username') or '').strip()
    p = b.get('password') or ''
    h = _editors().get(u)
    if h and p and check_password_hash(h, p):
        session['user'] = u
        session.permanent = True
        return jsonify({'ok': True, 'user': u})
    time.sleep(1)   # ถ่วง brute force
    return jsonify({'error': 'id หรือรหัสผ่านไม่ถูกต้อง'}), 401


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.pop('user', None)
    return jsonify({'ok': True})


@app.route('/api/me')
def api_me():
    return jsonify({'user': session.get('user'), 'edit_backend': EDIT_BACKEND})


def _apply_filters(sql: str, params: list, tax: str, year_min: str, year_max: str, doc_type: str) -> tuple[str, list]:
    """เพิ่ม WHERE clauses สำหรับ filter ทั้งหมด"""
    if tax:
        sql += ' AND m.tax_type LIKE ?'
        params.append(f'%{tax}%')
    if year_min:
        sql += ' AND m.year >= ?'
        params.append(int(year_min))
    if year_max:
        sql += ' AND m.year <= ?'
        params.append(int(year_max))
    if doc_type:
        sql += ' AND m.doc_type = ?'
        params.append(doc_type)
    return sql, params


@app.route('/api/search')
def search():
    q           = request.args.get('q', '').strip()
    section_ref = request.args.get('section_ref', '').strip()
    tax         = request.args.get('tax', '').strip()
    year_min    = request.args.get('year_min', '').strip()
    year_max    = request.args.get('year_max', '').strip()
    doc_type    = request.args.get('doc_type', '').strip()
    page        = max(1, int(request.args.get('page', 1)))
    no_tokenize = request.args.get('no_tokenize', '').lower() in ('1', 'true', 'yes')

    # ── section_ref mode: ค้นหาเอกสารที่เกี่ยวข้องกับมาตรานี้โดยตรงจาก law_links ──
    if section_ref and not q:
        db = get_db()
        base_where = "ll.sections LIKE ? AND m.doc_type NOT IN ('law_section','act','personal_note','tax_benefit_summary')"
        base_params = [f'%{section_ref}%']
        rows = db.execute(f"""
            SELECT DISTINCT m.id, m.ref_number, m.title, m.year, m.tax_type,
                   m.date, m.source_url, m.summary, m.facts, m.ruling_text,
                   m.doc_type, m.repealed, m.plain_summary
            FROM law_links ll JOIN meta m ON ll.doc_id = m.id
            WHERE {base_where}
            ORDER BY m.year DESC LIMIT ? OFFSET ?
        """, base_params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
        total_row = db.execute(f"""
            SELECT COUNT(DISTINCT m.id) FROM law_links ll JOIN meta m ON ll.doc_id = m.id
            WHERE {base_where}
        """, base_params).fetchone()
        total = total_row[0] if total_row else 0
        tokens = [section_ref]
        results = [{
            'id':            r['id'],
            'ref_number':    r['ref_number'],
            'title':         r['title'],
            'year':          r['year'],
            'tax_type':      r['tax_type'].split(',') if r['tax_type'] else [],
            'date':          r['date'],
            'source_url':    r['source_url'],
            'snippet':       highlight_snippet(r['ruling_text'] or r['facts'] or r['summary'] or '', tokens),
            'plain_summary': r['plain_summary'] or '',
            'doc_type':      r['doc_type'] or 'ruling',
            'repealed':      bool(r['repealed']),
            'has_amendment': False,
        } for r in rows]
        db.close()
        return jsonify({'results': results, 'total': total, 'page': page,
                        'per_page': PER_PAGE, 'tokens': tokens, 'mode': 'section_ref'})

    if not q:
        return jsonify({'results': [], 'total': 0, 'tokens': []})

    if no_tokenize:
        # ไม่ตัดคำ: ใช้ whitespace split เท่านั้น แต่ละคำใน query เป็น exact phrase
        tokens = [t for t in q.split() if t] or [q]
    else:
        tokens = tokenize_query(q)
    if not tokens:
        return jsonify({'results': [], 'total': 0, 'tokens': []})

    db = get_db()

    def run_fts(mode):
        fts_q = build_fts_query(tokens, mode)
        # GROUP BY ต้องอยู่หลัง WHERE clauses ทั้งหมด — _apply_filters เพิ่ม AND เข้ามาก่อน
        sql = '''
            SELECT m.id, m.ref_number, m.title, m.year, m.tax_type,
                   m.date, m.source_url, m.summary, m.facts, m.ruling_text,
                   m.doc_type, m.repealed, m.plain_summary, MIN(fts.rank) as rank
            FROM fts
            JOIN meta m ON fts.doc_id = m.id
            WHERE fts MATCH ?
        '''
        params = [fts_q]
        sql, params = _apply_filters(sql, params, tax, year_min, year_max, doc_type)
        sql += ' GROUP BY m.id'

        count_sql = re.sub(
            r'SELECT .+?FROM fts',
            'SELECT COUNT(DISTINCT m.id) FROM fts',
            sql, flags=re.DOTALL
        ).split('GROUP BY')[0]

        # BM25 relevance ก่อนเสมอ — hierarchy เป็น tiebreaker
        # ช่วยให้ ข้อหารือ ที่เกี่ยวข้องมากปรากฏใน page 1 แม้ไม่ได้ filter
        sql += f' ORDER BY {_RELEVANCE_ORDER} LIMIT ? OFFSET ?'
        params_page = params + [PER_PAGE, (page - 1) * PER_PAGE]

        rows = db.execute(sql, params_page).fetchall()
        try:
            total = db.execute(count_sql, params).fetchone()[0]
        except Exception:
            total = len(rows)
        return rows, total

    # AND search → fallback OR
    try:
        rows, total = run_fts('AND')
        used_mode = 'AND'
        if total < 5 and len(tokens) > 1:
            rows_or, total_or = run_fts('OR')
            if total_or > total:
                rows, total = rows_or, total_or
                used_mode = 'OR'
    except Exception as e:
        try:
            rows, total = run_fts('OR')
            used_mode = 'OR'
        except Exception:
            rows, total = [], 0
            used_mode = 'none'

    # ดึง amended_by counts สำหรับ docs ที่พบ
    ids_in_page = [r['id'] for r in rows]
    # doc_ids ที่มี amended_by annotation (ถูกแก้ไขบางส่วนแต่ยังบังคับใช้)
    _amended_ids: set[str] = set()
    if ids_in_page:
        try:
            ph = ','.join('?' * len(ids_in_page))
            amend_rows = db.execute(
                f"SELECT DISTINCT doc_id FROM doc_relations "
                f"WHERE doc_id IN ({ph}) AND relation='amended_by'",
                ids_in_page
            ).fetchall()
            _amended_ids = {row[0] for row in amend_rows}
        except Exception:
            pass

    results = []
    for r in rows:
        # เลือก snippet ที่ดีที่สุด
        snippet_src = r['ruling_text'] or r['facts'] or r['summary'] or ''
        snippet = highlight_snippet(snippet_src, tokens)

        _has_amendment = r['id'] in _amended_ids

        results.append({
            'id':            r['id'],
            'ref_number':    r['ref_number'],
            'title':         r['title'],
            'year':          r['year'],
            'tax_type':      r['tax_type'].split(',') if r['tax_type'] else [],
            'date':          r['date'],
            'source_url':    r['source_url'],
            'snippet':       snippet,
            'plain_summary': r['plain_summary'] or '',
            'doc_type':      r['doc_type'] or 'ruling',
            'repealed':      bool(r['repealed']),
            'has_amendment': _has_amendment,
        })

    # ── Q&A hits: parallel query เฉพาะ training type ──────────────────────────
    qa_hits = []
    try:
        fts_q_qa = build_fts_query(tokens, 'OR')
        qa_rows = db.execute('''
            SELECT m.id, m.title, m.summary, m.facts, m.ruling_text, m.source_url,
                   m.plain_summary, m.doc_type, MIN(fts.rank) as rank
            FROM fts JOIN meta m ON fts.doc_id = m.id
            WHERE fts MATCH ? AND m.doc_type = 'training'
              AND (m.facts LIKE 'Q:%' OR m.facts LIKE '%Q: %' OR m.ruling_text LIKE 'Q:%' OR m.ruling_text LIKE '%Q: %')
            GROUP BY m.id ORDER BY rank LIMIT 5
        ''', [fts_q_qa]).fetchall()
        for r in qa_rows:
            qa_text = r['facts'] or r['ruling_text'] or ''
            qa_hits.append({
                'id':            r['id'],
                'title':         r['title'],
                'source_url':    r['source_url'] or '',
                'snippet':       highlight_snippet(qa_text[:2000], tokens),
                'plain_summary': r['plain_summary'] or '',
                'doc_type':      'training',
            })
    except Exception:
        pass

    db.close()
    return jsonify({
        'results':     results,
        'total':       total,
        'page':        page,
        'per_page':    PER_PAGE,
        'tokens':      tokens,
        'mode':        used_mode,
        'qa_hits':     qa_hits,
        'no_tokenize': no_tokenize,
    })


@app.route('/api/detail/<path:ruling_id>')
def detail(ruling_id):
    ruling_id = re.sub(r'[^a-zA-Z0-9฀-๿\-\._]', '', ruling_id)
    fp = _JSON_LOOKUP.get(ruling_id) or os.path.join(JSON_DIR, f'{ruling_id}.json')
    try:
        with open(fp, encoding='utf-8') as f:
            d = json.load(f)
        # Normalize ฎีกา สำหรับ renderDetail ใน UI
        if d.get('type') == 'supreme_court_judgment':
            d['title']      = d.get('title') or d.get('subject', '')
            d['ref_number'] = d.get('ref_number') or d.get('case_number', '')
            if not d.get('content'):
                d['content'] = {
                    'ข้อเท็จจริง': d.get('facts', ''),
                    'คำวินิจฉัย':  d.get('ruling', ''),
                    'หลักการสำคัญ': d.get('key_principle', ''),
                }
        return jsonify(d)
    except FileNotFoundError:
        return jsonify({'error': 'ไม่พบข้อมูล'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Manual edit (แก้ไฟล์ JSON จากใน UI) ───────────────────────────────────────
_EDIT_BACKUP_DIR = os.path.join(DATA_ROOT, '.edit_backups')
_EDIT_LIST_FIELDS = {'keywords', 'tax_type', 'related_sections'}


def _edit_guard():
    """local mode: จำกัด localhost | github mode (cloud): ต้อง login ด้วยบัญชีทีมแก้ไข"""
    if EDIT_BACKEND == 'github':
        if session.get('user'):
            return None
        return jsonify({'error': 'ต้องเข้าสู่ระบบก่อนแก้ไข', 'login': '/login'}), 401
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'error': 'แก้ไขได้เฉพาะจากเครื่อง local เท่านั้น'}), 403
    return None


# ── GitHub PR backend (EDIT_BACKEND=github) ──────────────────────────────────
def _gh_api(method: str, path: str, body: dict | None = None) -> dict:
    token = os.getenv('GITHUB_PR_TOKEN') or ''
    req = urllib.request.Request(
        'https://api.github.com' + path,
        data=json.dumps(body).encode('utf-8') if body is not None else None,
        headers={'Authorization': f'Bearer {token}',
                 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json',
                 'User-Agent': 'law-app-editor'},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b'{}')


def _create_edit_pr(fp: str, new_doc: dict, editor: str) -> str:
    """สร้าง branch + commit + Pull Request เข้า repo ข้อมูล — คืน URL ของ PR
    เจ้าของ repo ตรวจ diff บน GitHub แล้ว Merge เอง (เครื่อง local pull ทุกเช้า)"""
    import base64
    from urllib.parse import quote
    repo = os.getenv('GITHUB_DATA_REPO', 'KK379724/klang-kotmai')
    base_branch = os.getenv('GITHUB_DATA_BRANCH', 'main')
    rel = os.path.relpath(fp, DATA_ROOT).replace(os.sep, '/')

    base_sha = _gh_api('GET', f'/repos/{repo}/git/ref/heads/{base_branch}')['object']['sha']
    safe_editor = re.sub(r'[^a-zA-Z0-9_-]', '', editor) or 'editor'
    branch = f"edit/{safe_editor}-{time.strftime('%Y%m%d-%H%M%S')}"
    _gh_api('POST', f'/repos/{repo}/git/refs', {'ref': f'refs/heads/{branch}', 'sha': base_sha})

    path_q = quote(rel)
    try:
        cur_sha = _gh_api('GET', f'/repos/{repo}/contents/{path_q}?ref={base_branch}').get('sha')
    except urllib.error.HTTPError:
        cur_sha = None   # ไฟล์ยังไม่มีบน repo
    content_b64 = base64.b64encode(
        json.dumps(new_doc, ensure_ascii=False, indent=2).encode('utf-8')
    ).decode('ascii')
    put_body = {'message': f"แก้ไข {new_doc.get('id', rel)} โดย {editor} (ผ่านแอปค้นหา)",
                'content': content_b64, 'branch': branch}
    if cur_sha:
        put_body['sha'] = cur_sha
    _gh_api('PUT', f'/repos/{repo}/contents/{path_q}', put_body)

    pr = _gh_api('POST', f'/repos/{repo}/pulls', {
        'title': f"[แก้จากแอป] {new_doc.get('ref_number') or new_doc.get('id', rel)} — โดย {editor}",
        'head': branch, 'base': base_branch,
        'body': (f"ผู้แก้ไข: **{editor}**\nไฟล์: `{rel}`\n"
                 f"เวลา: {time.strftime('%Y-%m-%d %H:%M:%S')} (เวลา server)\n\n"
                 "ตรวจ diff แล้วกด **Merge** เพื่อรับการแก้ไขเข้าคลัง — "
                 "เครื่อง local จะ pull อัตโนมัติเวลา 06:00"),
    })
    return pr.get('html_url', '')


@app.route('/api/raw/<path:ruling_id>')
def api_raw(ruling_id):
    """คืน JSON ดิบตรงจากไฟล์ (ไม่ผ่าน normalize แบบ /api/detail) สำหรับโหมดแก้ไข"""
    ruling_id = re.sub(r'[^a-zA-Z0-9฀-๿\-\._]', '', ruling_id)
    fp = _JSON_LOOKUP.get(ruling_id) or os.path.join(JSON_DIR, f'{ruling_id}.json')
    try:
        with open(fp, encoding='utf-8') as f:
            return jsonify({'doc': json.load(f), 'fpath': fp})
    except FileNotFoundError:
        return jsonify({'error': 'ไม่พบข้อมูล'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _update_law_index_row(d: dict):
    """อัปเดต rulings.db แถวเดียวให้ตรงกับไฟล์ที่แก้ — mapping เดียวกับ build_index.py
    (จำเป็นเพราะ update_incremental ของ watcher เพิ่มเฉพาะ id ใหม่ ไม่ re-index ไฟล์ที่แก้)"""
    import build_index as _bi
    d = dict(d)
    if d.get('type') == 'supreme_court_judgment':
        d['type'] = 'court_judgment'
        if not d.get('title'):
            d['title'] = d.get('subject', '')
        if not d.get('ref_number'):
            d['ref_number'] = d.get('case_number', '')
        if not d.get('content'):
            d['content'] = {'ข้อเท็จจริง': d.get('facts', ''), 'คำวินิจฉัย': d.get('ruling', '')}
        if not d.get('summary'):
            d['summary'] = d.get('key_principle', '') or d.get('why_important', '')
    if not d.get('title') and d.get('ref_number'):
        d['title'] = d['ref_number']

    doc_type   = d.get('type', 'ruling') or 'ruling'
    content    = d.get('content', {}) or {}
    title      = d.get('title', '') or ''
    ref_number = d.get('ref_number', '') or ''
    summary    = d.get('summary', '') or ''
    facts, ruling_txt = _bi.extract_facts_ruling(doc_type, content)

    keywords_txt  = ' '.join(d.get('keywords', []) or [])
    sections_txt  = ' '.join(d.get('related_sections', []) or [])
    chain = d.get('authorizing_law_chain', []) or []
    chain_txt = ' '.join(
        f"{c.get('law','')} {' '.join(c.get('sections',[]) or [])}"
        for c in chain if isinstance(c, dict)
    )
    full_content = ' '.join(filter(None, [
        title, ref_number, summary, facts, ruling_txt, keywords_txt, sections_txt,
        d.get('why_issued', '') or '', chain_txt,
        d.get('key_principle', '') or '', d.get('why_important', '') or '',
        d.get('full_text', '') or '',
    ]))

    year_str = d.get('date', '')
    year = int(year_str[:4]) + 543 if year_str and len(year_str) >= 4 else 0
    if not year:
        if d.get('year_be'):
            year = int(d['year_be'])
        else:
            _m = re.search(r'ruling-(\d{4})-', d.get('id', ''))
            if _m:
                year = int(_m.group(1))

    db = get_db()
    # upsert: ถ้าเป็น doc ใหม่ (เช่น บันทึกส่วนตัวที่เพิ่งสร้าง) ให้มีแถวก่อน แล้ว UPDATE เติมค่า
    db.execute('INSERT OR IGNORE INTO meta (id) VALUES (?)', (d['id'],))
    db.execute(
        '''UPDATE meta SET ref_number=?, title=?, year=?, tax_type=?, date=?, source_url=?,
           summary=?, facts=?, ruling_text=?, doc_type=?, repealed=?, plain_summary=?,
           chapter=?, part=? WHERE id=?''',
        (ref_number, title, year, _bi.normalize_tax_type(d.get('tax_type') or []),
         d.get('date', ''), d.get('source_url', ''), summary[:600], facts[:800],
         ruling_txt[:10000] if doc_type == 'law_section' else ruling_txt[:800],
         doc_type, 1 if d.get('repealed') else 0, (d.get('plain_summary') or '')[:400],
         (d.get('chapter') or '') or None, (d.get('part') or '') or None, d['id'])
    )
    db.execute('DELETE FROM fts WHERE doc_id=?', (d['id'],))
    db.execute('INSERT INTO fts VALUES (?,?,?)',
               (d['id'], _bi.tokenize_thai(title), _bi.tokenize_thai(full_content)))

    # ── refresh ความสัมพันธ์กฎหมายของ doc นี้ (แผนผัง/สายกฎหมาย/แก้ไข-ยกเลิก) ──
    # user แก้เลขมาตรา/เลขฉบับใน full_text หรือ chain → ลิงก์ต้องอัปเดตตาม
    db.execute('DELETE FROM law_links WHERE doc_id=?', (d['id'],))
    seen_links = set()
    for c in chain:
        if not isinstance(c, dict):
            continue
        plaw = (c.get('law') or '').strip()
        if not plaw or plaw in seen_links:
            continue
        seen_links.add(plaw)
        secs = ','.join(str(s) for s in (c.get('sections') or []) if s and str(s) != 'null')
        rel = (c.get('relationship') or '').strip()
        db.execute('INSERT OR IGNORE INTO law_links VALUES (?,?,?,?)', (d['id'], plaw, secs, rel))

    db.execute('DELETE FROM doc_relations WHERE doc_id=?', (d['id'],))
    if doc_type not in ('ruling', 'court_judgment', 'supreme_court_judgment'):
        ft_text = (content.get('full_text') or '') if isinstance(content, dict) else ''
        if ft_text:
            for pat, relname in ((_bi._AMENDED_BY_RE, 'amended_by'),
                                 (_bi._REPEALS_RE, 'repeals'),
                                 (_bi._AMENDS_RE, 'amends')):
                for m in pat.finditer(ft_text):
                    try:
                        db.execute('INSERT INTO doc_relations VALUES (?,?,?)',
                                   (d['id'], int(_bi._th2ar(m.group(1))), relname))
                    except (ValueError, sqlite3.Error):
                        pass

    db.commit()
    db.close()


def _reembed_law_doc(d: dict, fpath: str):
    """re-embed เอกสารเดียวเข้า law_th_v2 — point id เป็น uuid5 deterministic เขียนทับตัวเดิมเสมอ"""
    import law_search as _ls
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct
    text = _ls.doc_to_text(d)
    if not text.strip():
        raise RuntimeError('เอกสารไม่มีข้อความให้ embed')
    vec = _ls.embed(text)
    payload = _ls._make_payload(d, fpath)
    client = QdrantClient(path=QDRANT_PATH)
    client.upsert(QDRANT_COLLECTION,
                  [PointStruct(id=_ls._point_id(payload['doc_id']), vector=vec, payload=payload)])
    _VEC_CACHE['V_unit'] = None
    _VEC_CACHE['payloads'] = None


def _push_local_edit(fp: str) -> bool:
    """local mode: commit+push ไฟล์ที่แก้ขึ้น GitHub ทันที — เว็บสาธารณะจะได้ข้อมูลตอน rebuild ตี 5
    คืน False ถ้าไฟล์เป็น local-only (ติด gitignore เช่น บันทึกส่วนตัว/)"""
    import subprocess
    rel = os.path.relpath(fp, DATA_ROOT)
    git = ['git', '-C', DATA_ROOT]
    if subprocess.run(git + ['check-ignore', '-q', rel], capture_output=True).returncode == 0:
        return False

    def run(args):
        r = subprocess.run(git + args, capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
        return r

    run(['add', '--', rel])
    r = subprocess.run(
        git + ['-c', 'user.name=KK379724', '-c', 'user.email=kunanon63@gmail.com',
               'commit', '-m', f'แก้ไข {os.path.basename(rel)} (แก้มือผ่านแอป local)', '--', rel],
        capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        if 'nothing to commit' in (r.stdout + r.stderr):
            return False   # เนื้อหาไม่เปลี่ยนจริง
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
    # กัน push ถูกปฏิเสธเมื่อ Actions push ไปก่อนกลางคืน — autostash คุ้มครองไฟล์ค้างอื่นๆ
    run(['pull', '--rebase', '--autostash', 'origin', 'main'])
    run(['push', 'origin', 'main'])
    return True


@app.route('/api/save/<path:ruling_id>', methods=['POST'])
def api_save(ruling_id):
    guard = _edit_guard()
    if guard:
        return guard
    ruling_id = re.sub(r'[^a-zA-Z0-9฀-๿\-\._]', '', ruling_id)
    fp = _JSON_LOOKUP.get(ruling_id) or os.path.join(JSON_DIR, f'{ruling_id}.json')
    if not os.path.isfile(fp):
        return jsonify({'error': 'ไม่พบไฟล์'}), 404
    body = request.get_json(force=True, silent=True) or {}
    try:
        with open(fp, encoding='utf-8') as f:
            doc = json.load(f)
    except Exception as e:
        return jsonify({'error': f'อ่านไฟล์เดิมไม่ได้: {e}'}), 500

    if 'raw' in body:
        # โหมดขั้นสูง: แทนที่ JSON ทั้งไฟล์ — validate ก่อนเสมอ
        try:
            new_doc = json.loads(body['raw'])
        except Exception as e:
            return jsonify({'error': f'JSON ไม่ถูกต้อง: {e}'}), 400
        if not isinstance(new_doc, dict):
            return jsonify({'error': 'JSON ต้องเป็น object'}), 400
        if new_doc.get('id') != doc.get('id'):
            return jsonify({'error': 'ห้ามเปลี่ยนค่า id (ใช้เชื่อมกับ index)'}), 400
    else:
        new_doc = doc
        fields = body.get('fields') or {}
        for key, val in fields.items():
            if key == 'id':
                continue
            val = str(val)
            if key.startswith('content.'):
                content = new_doc.get('content')
                if not isinstance(content, dict):
                    content = {}
                content[key[len('content.'):]] = val
                new_doc['content'] = content
            elif key in _EDIT_LIST_FIELDS:
                new_doc[key] = [w.strip() for w in val.split(',') if w.strip()]
            else:
                new_doc[key] = val.strip() if len(val) < 200 else val

    new_doc['manually_edited'] = True   # scraper/backfill ต้องข้ามไฟล์ที่มี flag นี้
    new_doc['edited_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')

    # สำรองของเดิมก่อนทับเสมอ — กู้คืนได้จาก .edit_backups/ (นอก glob ของ index ทุกตัว)
    os.makedirs(_EDIT_BACKUP_DIR, exist_ok=True)
    bak = os.path.join(_EDIT_BACKUP_DIR, f"{ruling_id}.{time.strftime('%Y%m%d-%H%M%S')}.json")
    import shutil
    shutil.copy2(fp, bak)

    tmp = fp + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(new_doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, fp)

    warnings = []
    try:
        _update_law_index_row(new_doc)
    except Exception as e:
        warnings.append(f'อัปเดตดัชนีคำสำคัญไม่สำเร็จ: {e} — รัน build_index.py เพื่อซ่อม')
    if not DISABLE_SEMANTIC:
        try:
            _reembed_law_doc(new_doc, fp)
        except Exception as e:
            warnings.append(f'อัปเดตดัชนีความหมายไม่สำเร็จ: {e} — semantic จะเห็นเนื้อหาเก่าจนกว่า law_search --index')

    result = {'ok': True, 'backup': os.path.basename(bak), 'warnings': warnings}
    if EDIT_BACKEND == 'github':
        try:
            result['pr_url'] = _create_edit_pr(fp, new_doc, session.get('user', 'editor'))
        except Exception as e:
            warnings.append(f'สร้าง Pull Request ไม่สำเร็จ: {e} — การแก้ไขนี้อยู่บน server ชั่วคราวเท่านั้น จะหายเมื่อ restart')
    else:
        # local mode: push ขึ้น GitHub อัตโนมัติ เว็บสาธารณะได้ข้อมูลตอน rebuild ตี 5
        try:
            result['pushed'] = _push_local_edit(fp)
        except Exception as e:
            warnings.append(f'push ขึ้น GitHub ไม่สำเร็จ: {e} — เว็บสาธารณะจะยังไม่เห็นการแก้นี้ (commit ค้างอยู่ในเครื่อง สั่ง git push ทีหลังได้)')
    return jsonify(result)


@app.route('/api/whats_new')
def api_whats_new():
    """กฎหมาย/เอกสารที่ลงวันที่ล่าสุดในคลัง — หน้า "มาใหม่" ดึงคนกลับเว็บ"""
    db = get_db()
    rows = db.execute(
        """SELECT id, ref_number, title, year, tax_type, doc_type, repealed, plain_summary, summary, date
           FROM meta WHERE date IS NOT NULL AND date != '' AND doc_type NOT IN ('personal_note','law_section','act')
           ORDER BY date DESC LIMIT 30""").fetchall()
    db.close()
    results = [{
        'id': r['id'], 'ref_number': r['ref_number'] or '', 'title': r['title'] or '',
        'year': r['year'], 'doc_type': r['doc_type'], 'repealed': r['repealed'],
        'date': r['date'],
        'tax_type': [t for t in (r['tax_type'] or '').split(',') if t],
        'snippet': (r['plain_summary'] or r['summary'] or '')[:250],
    } for r in rows]
    return jsonify({'results': results, 'total': len(results)})


@app.route('/api/ocr_queue')
def api_ocr_queue():
    """คิวเอกสาร OCR เพี้ยน (สร้างโดย scan_ocr_quality.py ทุกคืน) — สำหรับทีมแก้ไข"""
    guard = _edit_guard()
    if guard:
        return guard
    qf = os.path.join(DATA_ROOT, 'ocr_fix_queue.json')
    if not os.path.exists(qf):
        return jsonify({'queue': [], 'note': 'ยังไม่มีคิว — รัน scan_ocr_quality.py ก่อน'})
    with open(qf, encoding='utf-8') as f:
        data = json.load(f)
    return jsonify({'queue': data.get('queue', [])[:200],
                    'scanned': data.get('scanned'), 'flagged': data.get('flagged')})


_OCR_IDS_CACHE = {'ids': None, 'mtime': -1}

@app.route('/api/ocr_queue_ids')
def api_ocr_queue_ids():
    """คืนชุด id ที่อยู่ในคิว OCR — ให้หน้าเว็บติดป้าย '⏳ รอตรวจ OCR' บนเอกสารที่ full_text อาจเพี้ยน
    (เปิดให้ทุกคนเห็น = ความโปร่งใสว่าเอกสารไหนยังไม่ผ่านการตรวจ)"""
    qf = os.path.join(DATA_ROOT, 'ocr_fix_queue.json')
    try:
        m = os.path.getmtime(qf)
        if _OCR_IDS_CACHE['ids'] is None or m != _OCR_IDS_CACHE['mtime']:
            with open(qf, encoding='utf-8') as f:
                data = json.load(f)
            _OCR_IDS_CACHE['ids'] = [it.get('id') for it in data.get('queue', []) if it.get('id')]
            _OCR_IDS_CACHE['mtime'] = m
    except Exception:
        _OCR_IDS_CACHE['ids'] = _OCR_IDS_CACHE['ids'] or []
    return jsonify({'ids': _OCR_IDS_CACHE['ids']})


@app.route('/api/delete/<path:ruling_id>', methods=['POST'])
def api_delete(ruling_id):
    """ลบบันทึกส่วนตัว (เฉพาะ type=personal_note, เฉพาะเครื่อง local) — ย้ายเข้าถังขยะ กู้คืนได้
    (กติกา: ห้ามลบถาวรโดยไม่ผ่าน user — .trash เก็บไฟล์ไว้เสมอ)"""
    if EDIT_BACKEND != 'local' or request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'error': 'ลบได้เฉพาะเจ้าของคลังบนเครื่อง local เท่านั้น'}), 403
    ruling_id = re.sub(r'[^a-zA-Z0-9฀-๿\-\._]', '', ruling_id)
    fp = _JSON_LOOKUP.get(ruling_id) or os.path.join(JSON_DIR, f'{ruling_id}.json')
    if not os.path.isfile(fp):
        return jsonify({'error': 'ไม่พบไฟล์'}), 404
    try:
        with open(fp, encoding='utf-8') as f:
            doc = json.load(f)
    except Exception as e:
        return jsonify({'error': f'อ่านไฟล์ไม่ได้: {e}'}), 500
    if doc.get('type') != 'personal_note':
        return jsonify({'error': 'ปุ่มลบใช้ได้เฉพาะบันทึกส่วนตัวเท่านั้น (เอกสารกฎหมายห้ามลบ)'}), 400

    import shutil
    trash_dir = os.path.join(DATA_ROOT, '.trash')
    os.makedirs(trash_dir, exist_ok=True)
    dest = os.path.join(trash_dir, f"{os.path.basename(fp)}.{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.move(fp, dest)
    _JSON_LOOKUP.pop(ruling_id, None)

    warnings = []
    try:
        db = get_db()
        for table, col in (('meta', 'id'), ('fts', 'doc_id'), ('law_links', 'doc_id'), ('doc_relations', 'doc_id')):
            db.execute(f'DELETE FROM {table} WHERE {col}=?', (ruling_id,))
        db.commit()
        db.close()
    except Exception as e:
        warnings.append(f'ลบออกจากดัชนีไม่สำเร็จ: {e}')
    try:
        import law_search as _ls
        from qdrant_client import QdrantClient
        client = QdrantClient(path=QDRANT_PATH)
        client.delete(QDRANT_COLLECTION, points_selector=[_ls._point_id(ruling_id)])
        _VEC_CACHE['V_unit'] = None
        _VEC_CACHE['payloads'] = None
    except Exception as e:
        warnings.append(f'ลบออกจากดัชนีความหมายไม่สำเร็จ: {e}')
    # บันทึกการลบขึ้น GitHub ด้วย (เว็บจะหายตาม rebuild ตี 5)
    try:
        import subprocess
        rel = os.path.relpath(fp, DATA_ROOT)
        git = ['git', '-C', DATA_ROOT]
        if subprocess.run(git + ['ls-files', '--error-unmatch', rel], capture_output=True).returncode == 0:
            subprocess.run(git + ['rm', '--cached', '-q', rel], capture_output=True, timeout=30)
            subprocess.run(git + ['-c', 'user.name=KK379724', '-c', 'user.email=kunanon63@gmail.com',
                                  'commit', '-q', '-m', f'ลบบันทึกส่วนตัว {ruling_id} (ย้ายเข้าถังขยะจากแอป)'],
                           capture_output=True, timeout=60)
            subprocess.run(git + ['pull', '--rebase', '--autostash', '-q', 'origin', 'main'],
                           capture_output=True, timeout=90)
            subprocess.run(git + ['push', '-q', 'origin', 'main'], capture_output=True, timeout=90)
    except Exception as e:
        warnings.append(f'sync การลบขึ้น GitHub ไม่สำเร็จ: {e}')
    return jsonify({'ok': True, 'trash': dest, 'warnings': warnings})


_CREATE_FOLDER = {'personal_note': 'บันทึกส่วนตัว'}

@app.route('/api/create', methods=['POST'])
def api_create():
    """เพิ่มเอกสารใหม่ (ตอนนี้รองรับ personal_note = บันทึกส่วนตัว/เคสที่เจอเอง)
    local → เขียนไฟล์+commit+push | เว็บ(ทีม) → สร้าง PR ให้เจ้าของอนุมัติ"""
    guard = _edit_guard()
    if guard:
        return guard
    body = request.get_json(force=True, silent=True) or {}
    dtype = (body.get('type') or 'personal_note').strip()
    if dtype not in _CREATE_FOLDER:
        return jsonify({'error': f'ตอนนี้เพิ่มได้เฉพาะ: {", ".join(_CREATE_FOLDER)} (เอกสารกฎหมายเพิ่มผ่าน scraper/AI ตาม flow ปกติ)'}), 400

    title = (body.get('title') or '').strip()
    full_text = (body.get('full_text') or '').strip()
    if not title:
        return jsonify({'error': 'กรุณาใส่หัวข้อ (title)'}), 400
    if not full_text:
        return jsonify({'error': 'กรุณาใส่เนื้อหา'}), 400

    ts = time.strftime('%Y%m%d-%H%M%S')
    new_id = f'note-{ts}'
    tax_type = body.get('tax_type') or []
    if isinstance(tax_type, str):
        tax_type = [w.strip() for w in tax_type.split(',') if w.strip()]
    author = session.get('user') or ('kunanon' if EDIT_BACKEND == 'local' else 'editor')

    _HLABEL = {9: 'อื่นๆ / บันทึกส่วนตัว'}
    doc = {
        'id': new_id,
        'type': dtype,
        'schema_version': '1.0',
        'hierarchy_level': 9,
        'hierarchy_label': _HLABEL[9],
        'ref_number': '',
        'title': title,
        'tax_type': tax_type,
        'date': (body.get('date') or time.strftime('%Y-%m-%d')),
        'category': (body.get('category') or 'เคสที่เจอจากการทำงาน').strip(),
        'summary': (body.get('summary') or '').strip(),
        'plain_summary': (body.get('plain_summary') or '').strip(),
        'why_issued': '',
        'authorizing_law_chain': [],
        'related_sections': [],
        'content': {'full_text': full_text},
        'manually_edited': True,           # ผู้ใช้พิมพ์เอง — scraper/backfill ห้ามแตะ
        'author': author,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'edited_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }

    out_dir = os.path.join(DATA_ROOT, _CREATE_FOLDER[dtype])
    os.makedirs(out_dir, exist_ok=True)
    fp = os.path.join(out_dir, f'{new_id}.json')
    tmp = fp + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, fp)
    _JSON_LOOKUP[new_id] = fp

    warnings = []
    try:
        _update_law_index_row(doc)
    except Exception as e:
        warnings.append(f'เพิ่มเข้าดัชนีคำสำคัญไม่สำเร็จ: {e} — รัน build_index.py เพื่อซ่อม')
    if not DISABLE_SEMANTIC:
        try:
            _reembed_law_doc(doc, fp)
        except Exception as e:
            warnings.append(f'เพิ่มเข้าดัชนีความหมายไม่สำเร็จ: {e}')

    result = {'ok': True, 'id': new_id, 'warnings': warnings}
    if EDIT_BACKEND == 'github':
        try:
            result['pr_url'] = _create_edit_pr(fp, doc, author)
        except Exception as e:
            warnings.append(f'สร้าง Pull Request ไม่สำเร็จ: {e} — บันทึกนี้อยู่บน server ชั่วคราว จะหายเมื่อ restart')
    else:
        try:
            result['pushed'] = _push_local_edit(fp)
        except Exception as e:
            warnings.append(f'push ขึ้น GitHub ไม่สำเร็จ: {e} — commit ค้างในเครื่อง สั่ง git push ทีหลังได้')
    return jsonify(result)


@app.route('/api/ai_enrich', methods=['POST'])
def api_ai_enrich():
    """ให้ AI (free chain) สรุป + สกัดสายกฎหมายจาก full_text ที่ user เพิ่งพิมพ์แก้
    — คืนค่าให้เติมในฟอร์มแก้ไข user ตรวจก่อนบันทึกเอง ไม่เขียนไฟล์ตรง"""
    guard = _edit_guard()
    if guard:
        return guard
    b = request.get_json(force=True, silent=True) or {}
    full_text = (b.get('full_text') or '').strip()
    title = (b.get('title') or '').strip()
    if len(full_text) < 50:
        return jsonify({'error': 'เนื้อหาเต็มสั้นเกินไป — พิมพ์ full_text ก่อนแล้วค่อยกดสรุป'}), 400
    prompt = (
        "คุณคือผู้เชี่ยวชาญกฎหมายภาษีไทย อ่านเอกสารต่อไปนี้แล้วตอบเป็น JSON เท่านั้น (ห้ามมีข้อความอื่น):\n"
        '{"summary": "สรุปสาระสำคัญ 2-4 ประโยค ภาษาทางการ", '
        '"plain_summary": "สรุปภาษาชาวบ้านที่คนทั่วไปเข้าใจ 1-3 ประโยค", '
        '"keywords": ["คำสำคัญ 4-8 คำ"], '
        '"authorizing_law_chain": [{"law": "ชื่อกฎหมายแม่ที่อ้างอำนาจ", "sections": ["มาตรา X"], "relationship": "อาศัยอำนาจตาม"}]}\n'
        "กติกา: authorizing_law_chain เอาเฉพาะที่ระบุจริงในเนื้อหา (มักอยู่ท่อน 'อาศัยอำนาจตามความใน...') "
        "ถ้าไม่มีให้ใส่ [] ห้ามเดา ห้ามใส่ null\n\n"
        f"ชื่อเอกสาร: {title}\n\nเนื้อหา:\n{full_text[:12000]}"
    )
    try:
        import subprocess
        r = subprocess.run(
            ['python3', os.path.expanduser('~/scripts/ai_router.py'), prompt, '--mode', 'thai', '--json'],
            capture_output=True, text=True, timeout=150,
        )
        out = r.stdout.strip()
        start, end = out.find('{'), out.rfind('}') + 1
        if start < 0 or end <= start:
            return jsonify({'error': f'AI ไม่ตอบเป็น JSON: {out[:200]}'}), 502
        data = json.loads(out[start:end])
        return jsonify({'ok': True, 'fields': {
            'summary': data.get('summary', ''),
            'plain_summary': data.get('plain_summary', ''),
            'keywords': data.get('keywords', []),
            'authorizing_law_chain': data.get('authorizing_law_chain', []),
        }})
    except Exception as e:
        return jsonify({'error': f'เรียก AI ไม่สำเร็จ: {e}'}), 502


# กลุ่ม doc_type ที่ควรอ่านพร้อมกัน — ใช้กรอง _resolve_num ให้แม่นขึ้น
_TYPE_FAMILY: dict[str, tuple] = {
    'director_general_notification': ('director_general_notification', 'notification'),
    'notification':                  ('director_general_notification', 'notification'),
    'department_notification':       ('department_notification',),
    'department_order':              ('department_order',),
    'ministerial_regulation':        ('ministerial_regulation',),
    'royal_decree':                  ('royal_decree',),
    'ministry_notification':         ('ministry_notification',),
}


@app.route('/api/related/<path:ruling_id>')
def related(ruling_id):
    """คืนความสัมพันธ์ทางกฎหมาย: law_chain, amended_by, amends, repeals, sibling_docs"""
    ruling_id = re.sub(r'[^a-zA-Z0-9฀-๿\-\._]', '', ruling_id)
    db = get_db()

    # ── อ่าน meta + ref_number ──────────────────────────────────────────────
    row = db.execute(
        'SELECT id, ref_number, title, doc_type, repealed, date FROM meta WHERE id=?',
        (ruling_id,)
    ).fetchone()
    if not row:
        db.close()
        return jsonify({'error': 'ไม่พบ'}), 404

    ref_number = row['ref_number'] or ''
    doc_type   = row['doc_type'] or ''

    # หาเลขฉบับของ doc นี้
    _ISSUE_RE_LOCAL = re.compile(r'ฉบับที่\s*(\d+)')
    m_num = _ISSUE_RE_LOCAL.search(ref_number)
    doc_num = int(m_num.group(1)) if m_num else None

    # ── law_chain (authorizing_law_chain จาก law_links) ─────────────────────
    link_rows = db.execute(
        'SELECT parent_law, sections, relationship FROM law_links WHERE doc_id=?',
        (ruling_id,)
    ).fetchall()
    law_chain = []
    for lr in link_rows:
        parent_law = lr['parent_law']
        secs = [s.strip() for s in (lr['sections'] or '').split(',') if s.strip()]
        # หา sibling docs: docs อื่นที่ใช้ parent_law เดียวกัน (max 5)
        siblings = db.execute(
            '''SELECT m.id, m.ref_number, m.title, m.doc_type, m.year
               FROM law_links ll JOIN meta m ON ll.doc_id=m.id
               WHERE ll.parent_law=? AND ll.doc_id!=?
               ORDER BY m.year DESC LIMIT 5''',
            (parent_law, ruling_id)
        ).fetchall()
        law_chain.append({
            'law':          parent_law,
            'sections':     secs,
            'relationship': lr['relationship'] or '',
            'siblings':     [dict(s) for s in siblings],
        })

    # ── doc_relations ───────────────────────────────────────────────────────
    # สกัด prefix จาก ruling_id (ตัดเลขท้ายออก): dg45→dg, dgvat45→dgvat, dgs45→dgs
    _id_prefix = re.sub(r'\d+$', '', ruling_id)

    def _resolve_num(num: int, prefer_type: str = '') -> list[dict]:
        """หา doc จากเลขฉบับ — ลอง ID ตรงก่อน แล้วค่อย LIKE fallback"""
        # 1) ลอง ID ตรง: dg45 → dg73 (เร็วและแม่นที่สุด)
        if _id_prefix:
            direct = db.execute(
                'SELECT id, ref_number, title, doc_type, year, repealed FROM meta WHERE id=?',
                (f'{_id_prefix}{num}',)
            ).fetchone()
            if direct:
                return [dict(direct)]
        # 2) LIKE กรอง doc_type ก่อน
        type_filter = _TYPE_FAMILY.get(prefer_type, ())
        if type_filter:
            placeholders = ','.join('?' * len(type_filter))
            rows = db.execute(
                f"SELECT id, ref_number, title, doc_type, year, repealed "
                f"FROM meta WHERE ref_number LIKE ? AND doc_type IN ({placeholders}) LIMIT 3",
                (f'%ฉบับที่ {num})%', *type_filter)
            ).fetchall()
            if not rows:
                rows = db.execute(
                    f"SELECT id, ref_number, title, doc_type, year, repealed "
                    f"FROM meta WHERE ref_number LIKE ? AND doc_type IN ({placeholders}) LIMIT 3",
                    (f'%ฉบับที่ {num}%', *type_filter)
                ).fetchall()
            if rows:
                return [dict(r) for r in rows]
        # 3) fallback ไม่กรอง type
        rows = db.execute(
            "SELECT id, ref_number, title, doc_type, year, repealed "
            "FROM meta WHERE ref_number LIKE ? LIMIT 3",
            (f'%ฉบับที่ {num})%',)
        ).fetchall()
        if not rows:
            rows = db.execute(
                "SELECT id, ref_number, title, doc_type, year, repealed "
                "FROM meta WHERE ref_number LIKE ? LIMIT 3",
                (f'%ฉบับที่ {num}%',)
            ).fetchall()
        return [dict(r) for r in rows]

    # amended_by: annotations ใน full_text ของ doc นี้บอกว่าถูกแก้ไขโดย N
    # doc_relations: (doc_id=ruling_id, target_num=N, relation='amended_by')
    amend_num_rows = db.execute(
        "SELECT DISTINCT target_num FROM doc_relations WHERE doc_id=? AND relation='amended_by'",
        (ruling_id,)
    ).fetchall()
    amended_by_resolved = []
    for (n,) in amend_num_rows:
        for d_row in _resolve_num(n, doc_type):
            amended_by_resolved.append({
                'amender_id':    d_row['id'],
                'amender_ref':   d_row['ref_number'],
                'amender_title': d_row['title'],
                'amender_type':  d_row['doc_type'],
                'amender_year':  d_row['year'],
            })
    # เพิ่ม: หาจากด้าน amender — docs อื่นที่มี 'amends' → doc_num นี้
    if doc_num:
        amend_other_rows = db.execute(
            "SELECT DISTINCT dr.doc_id, m.ref_number, m.title, m.doc_type, m.year "
            "FROM doc_relations dr JOIN meta m ON dr.doc_id=m.id "
            "WHERE dr.target_num=? AND dr.relation='amends' AND dr.doc_id!=?",
            (doc_num, ruling_id)
        ).fetchall()
        seen_ids = {a['amender_id'] for a in amended_by_resolved}
        for r in amend_other_rows:
            if r['doc_id'] not in seen_ids:
                amended_by_resolved.append({
                    'amender_id':    r['doc_id'],
                    'amender_ref':   r['ref_number'],
                    'amender_title': r['title'],
                    'amender_type':  r['doc_type'],
                    'amender_year':  r['year'],
                })

    # amends: doc นี้แก้ไขบางส่วนของ docs อื่น (ยังบังคับใช้)
    amends_nums = db.execute(
        "SELECT DISTINCT target_num FROM doc_relations WHERE doc_id=? AND relation='amends'",
        (ruling_id,)
    ).fetchall()
    amends_resolved = []
    for (tnum,) in amends_nums:
        amends_resolved.extend(_resolve_num(tnum, doc_type))

    # repeals: doc นี้ยกเลิกทั้งฉบับ
    repeals_nums = db.execute(
        "SELECT DISTINCT target_num FROM doc_relations WHERE doc_id=? AND relation='repeals'",
        (ruling_id,)
    ).fetchall()
    repeals_resolved = []
    for (tnum,) in repeals_nums:
        repeals_resolved.extend(_resolve_num(tnum, doc_type))

    # ── หา docs อื่นที่ยกเลิก doc นี้ (repealed_by) ─────────────────────────
    repealed_by = []
    if doc_num:
        rb_rows = db.execute(
            "SELECT dr.doc_id, m.ref_number, m.title, m.doc_type, m.year "
            "FROM doc_relations dr JOIN meta m ON dr.doc_id=m.id "
            "WHERE dr.target_num=? AND dr.relation='repeals' AND dr.doc_id!=?",
            (doc_num, ruling_id)
        ).fetchall()
        repealed_by = [dict(r) for r in rb_rows]

    # ── กฎหมายลูก (child laws) ────────────────────────────────────────────────
    # docs ที่อ้างอิง doc นี้เป็น parent law ใน authorizing_law_chain
    child_laws = []

    if doc_type == 'law_section' and ruling_id.startswith('section-'):
        # section-48 → '48', section-48-ทวิ → '48 ทวิ', section-3-สัตต → '3 สัตต'
        sec_rest = ruling_id[len('section-'):]
        parts = sec_rest.split('-', 1)
        sec_base = parts[0]
        sec_suffix = parts[1].replace('-', ' ') if len(parts) > 1 else ''
        sec_num = f'{sec_base} {sec_suffix}'.strip() if sec_suffix else sec_base

        # ค้น law_links: docs ที่อ้าง ประมวลรัษฎากร มาตรา นี้
        child_rows = db.execute('''
            SELECT ll.doc_id, ll.sections, m.ref_number, m.title, m.doc_type, m.year, m.repealed
            FROM law_links ll JOIN meta m ON ll.doc_id = m.id
            WHERE ll.parent_law LIKE '%ประมวลรัษฎากร%'
            AND (
                ll.sections LIKE ? OR ll.sections LIKE ? OR
                ll.sections LIKE ? OR ll.sections LIKE ?
            )
            ORDER BY m.year DESC, ll.doc_id ASC
            LIMIT 200
        ''', (
            f'%มาตรา {sec_num}%', f'%มาตรา{sec_num}%',
            f'{sec_num},%', f'% {sec_num},%',
        )).fetchall()

        # กรองออก false positive: "มาตรา 48" ต้องไม่ตามด้วยตัวเลข (เพื่อกัน มาตรา 480)
        _sec_pat = re.compile(
            r'(?:มาตรา\s*)?' + re.escape(sec_num) + r'(?=[^0-9]|$)'
        )
        child_laws = [
            {
                'doc_id':   r['doc_id'],
                'ref':      r['ref_number'] or r['doc_id'],
                'title':    r['title'] or '',
                'doc_type': r['doc_type'] or '',
                'year':     r['year'] or 0,
                'repealed': bool(r['repealed']),
            }
            for r in child_rows
            if _sec_pat.search(r['sections'] or '')
        ]

    elif doc_type not in ('ruling', 'court_judgment') and ref_number:
        # สำหรับ doc ที่มี ref_number ชัดเจน (เช่น กฎกระทรวง ฉบับที่ 126)
        # ค้น law_links ที่มี parent_law ตรงกับ ref_number ของ doc นี้
        child_rows = db.execute('''
            SELECT ll.doc_id, ll.sections, m.ref_number, m.title, m.doc_type, m.year, m.repealed
            FROM law_links ll JOIN meta m ON ll.doc_id = m.id
            WHERE ll.parent_law LIKE ?
            ORDER BY m.year DESC
            LIMIT 100
        ''', (f'%{ref_number}%',)).fetchall()
        child_laws = [
            {
                'doc_id':   r['doc_id'],
                'ref':      r['ref_number'] or r['doc_id'],
                'title':    r['title'] or '',
                'doc_type': r['doc_type'] or '',
                'year':     r['year'] or 0,
                'repealed': bool(r['repealed']),
            }
            for r in child_rows
        ]

    elif doc_type == 'ruling':
        # ข้อหารือ: หา กฎหมายลูก ที่อ้างมาตราเดียวกัน ผ่าน ruling_related_laws
        try:
            child_rows = db.execute('''
                SELECT m.id, m.ref_number, m.title, m.doc_type, m.year, m.repealed,
                       rl.shared_count, rl.shared_secs
                FROM ruling_related_laws rl JOIN meta m ON rl.child_id = m.id
                WHERE rl.ruling_id = ?
                ORDER BY rl.shared_count DESC, m.year DESC
                LIMIT 30
            ''', (ruling_id,)).fetchall()
            child_laws = [
                {
                    'doc_id':      r['id'],
                    'ref':         r['ref_number'] or r['id'],
                    'title':       r['title'] or '',
                    'doc_type':    r['doc_type'] or '',
                    'year':        r['year'] or 0,
                    'repealed':    bool(r['repealed']),
                    'shared_count': r['shared_count'],
                    'shared_secs':  r['shared_secs'] or '',
                }
                for r in child_rows
            ]
        except Exception:
            child_laws = []

    # ── ข้อหารือที่เกี่ยวข้อง (สำหรับ กฎหมายลูก ทุกประเภท) ──────────────────
    related_rulings = []
    if doc_type in ('royal_decree', 'director_general_notification', 'ministerial_regulation',
                    'ministry_notification', 'department_notification', 'department_order'):
        try:
            rr_rows = db.execute('''
                SELECT m.id, m.ref_number, m.title, m.year,
                       rl.shared_count, rl.shared_secs
                FROM ruling_related_laws rl JOIN meta m ON rl.ruling_id = m.id
                WHERE rl.child_id = ?
                ORDER BY rl.shared_count DESC, m.year DESC
                LIMIT 20
            ''', (ruling_id,)).fetchall()
            related_rulings = [
                {
                    'id':          r['id'],
                    'ref_number':  r['ref_number'] or r['id'],
                    'title':       r['title'] or '',
                    'year':        r['year'] or 0,
                    'shared_count': r['shared_count'],
                    'shared_secs':  r['shared_secs'] or '',
                }
                for r in rr_rows
            ]
        except Exception:
            related_rulings = []

    db.close()
    return jsonify({
        'id':              ruling_id,
        'ref_number':      ref_number,
        'repealed':        bool(row['repealed']),
        'law_chain':       law_chain,
        'amended_by':      amended_by_resolved,
        'amends':          amends_resolved,
        'repeals':         repeals_resolved,
        'repealed_by':     repealed_by,
        'child_laws':      child_laws,
        'related_rulings': related_rulings,
    })


@app.route('/api/law_tree/<path:doc_id>')
def law_tree(doc_id):
    """คืน law tree: ประมวลรัษฎากร → กฎหมายลูก → ข้อหารือ สำหรับเอกสารที่ระบุ"""
    doc_id = re.sub(r'[^a-zA-Z0-9฀-๿\-\._]', '', doc_id)
    db = get_db()

    links = db.execute(
        "SELECT parent_law, sections FROM law_links WHERE doc_id=? AND parent_law LIKE '%ประมวลรัษฎากร%'",
        (doc_id,)
    ).fetchall()

    if not links:
        return jsonify({'nodes': [], 'current_id': doc_id})

    nodes = []
    seen_secs: set = set()

    for link in links:
        raw_secs = [s.strip() for s in (link['sections'] or '').split(',') if s.strip()]
        for sec in raw_secs:
            if sec in seen_secs:
                continue
            seen_secs.add(sec)
            if len(nodes) >= 4:
                break

            # ประมวลรัษฎากร section ID: "มาตรา 40 ทวิ" → "section-40-ทวิ"
            sec_num_raw = re.sub(r'^มาตรา\s*', '', sec).strip()
            sec_num_base = re.sub(r'\s*\(.*', '', sec_num_raw).strip()  # ตัด (1), (2) ออก
            sec_id = ('section-' + re.sub(r'\s+', '-', sec_num_base)) if sec_num_base else None

            sec_info = None
            if sec_id:
                row = db.execute(
                    "SELECT id, title FROM meta WHERE id=? AND doc_type='law_section'",
                    (sec_id,)
                ).fetchone()
                if row:
                    sec_info = {'id': row['id'], 'title': row['title'] or sec}

            # กฎหมายลูกที่อ้างมาตรานี้ (max 12)
            child_rows = db.execute("""
                SELECT DISTINCT m.id, m.ref_number, m.title, m.doc_type, m.year, m.repealed
                FROM law_links ll JOIN meta m ON ll.doc_id = m.id
                WHERE ll.parent_law LIKE '%ประมวลรัษฎากร%'
                  AND ll.sections LIKE ?
                  AND m.doc_type NOT IN ('ruling', 'court_judgment', 'law_section', 'act')
                ORDER BY (CASE m.doc_type
                    WHEN 'royal_decree'                  THEN 1
                    WHEN 'ministerial_regulation'        THEN 2
                    WHEN 'ministry_notification'         THEN 3
                    WHEN 'director_general_notification' THEN 4
                    ELSE 5 END), m.year DESC
                LIMIT 12
            """, (f'%{sec}%',)).fetchall()

            child_laws = [
                {
                    'id':         r['id'],
                    'ref':        r['ref_number'] or r['id'],
                    'title':      (r['title'] or '')[:55],
                    'doc_type':   r['doc_type'] or '',
                    'year':       r['year'] or 0,
                    'repealed':   bool(r['repealed']),
                    'is_current': r['id'] == doc_id,
                }
                for r in child_rows
            ]

            # จำนวนข้อหารือที่อ้างมาตรานี้
            ruling_count = db.execute(
                "SELECT count(DISTINCT doc_id) FROM law_links "
                "WHERE parent_law LIKE '%ประมวลรัษฎากร%' AND sections LIKE ? AND doc_id LIKE 'ruling-%'",
                (f'%{sec}%',)
            ).fetchone()[0]

            nodes.append({
                'section':       sec,
                'section_id':    sec_info['id'] if sec_info else None,
                'section_title': sec_info['title'] if sec_info else sec,
                'child_laws':    child_laws,
                'ruling_count':  ruling_count,
            })

    return jsonify({'nodes': nodes, 'current_id': doc_id})


@app.route('/api/all_docs')
def all_docs():
    """แสดงเอกสารทั้งหมดตาม doc_type — ประมวลเรียงตามมาตรา, ประเภทอื่นเรียงใหม่→เก่า"""
    doc_type = request.args.get('doc_type', '').strip()
    page = max(1, int(request.args.get('page', 1)))
    PER = 30

    if not doc_type:
        return jsonify({'error': 'กรุณาระบุ doc_type'}), 400

    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM meta WHERE doc_type=?", (doc_type,)).fetchone()[0]
    # เรียงตามกฎ user: ประมวลรัษฎากร → ตามเลขมาตรา | ประเภทอื่น → ใหม่ไปเก่า
    if doc_type == 'law_section':
        order_by = ("(CASE WHEN id LIKE 'section-%' THEN CAST(SUBSTR(id,9) AS INTEGER) "
                    "ELSE 999999 END) ASC, id ASC")
    else:
        order_by = "COALESCE(date,'') DESC, year DESC, id DESC"
    page_rows = db.execute(
        "SELECT id, ref_number, title, year, date, doc_type, repealed, tax_type "
        f"FROM meta WHERE doc_type=? ORDER BY {order_by} LIMIT ? OFFSET ?",
        (doc_type, PER, (page - 1) * PER)
    ).fetchall()

    page_ids = [r['id'] for r in page_rows]
    amended_ids: set[str] = set()
    if page_ids:
        ph2 = ','.join('?' * len(page_ids))
        amended_ids = {r['doc_id'] for r in db.execute(
            f"SELECT DISTINCT doc_id FROM doc_relations WHERE doc_id IN ({ph2}) AND relation='amended_by'",
            page_ids
        ).fetchall()}

    results = [{
        'id':           r['id'],
        'ref_number':   r['ref_number'] or '',
        'title':        r['title'] or '',
        'year':         r['year'],
        'date':         r['date'] or '',
        'doc_type':     r['doc_type'] or '',
        'repealed':     bool(r['repealed']),
        'has_amendment': r['id'] in amended_ids,
        'tax_type':     r['tax_type'].split(',') if r['tax_type'] else [],
    } for r in page_rows]

    db.close()
    return jsonify({
        'results':  results,
        'total':    total,
        'page':     page,
        'per_page': PER,
        'doc_type': doc_type,
    })


@app.route('/api/by_law')
def by_law():
    """แสดงเอกสารทั้งหมดที่อ้างอิงกฎหมายนี้ เรียงตามมาตรา (ประมวลรัษฎากร) หรือวันที่ (อื่นๆ)"""
    law_name = request.args.get('law', '').strip()
    page = max(1, int(request.args.get('page', 1)))
    PER = 30

    if not law_name:
        return jsonify({'error': 'กรุณาระบุชื่อกฎหมาย'}), 400

    db = get_db()
    is_revenue_code = law_name == 'ประมวลรัษฎากร'

    if is_revenue_code:
        # แสดงมาตราประมวลรัษฎากรทั้งหมด เรียงตามเลขมาตรา
        # section-48 → SUBSTR(id,9)='48' → CAST=48
        # suffix order: ทวิ=2, ตรี=3, จัตวา=4, ฉ=6, สัตต=7, อัฏฐ=8, นว=9, ทศ=10, เอกาทศ=11, ทวาทศ=12, เตรส=13, จตุทศ=14, ปัณรส=15, โสฬส=16, สัตตรส=17, อัฏฐารส=18
        rows = db.execute(
            """SELECT id, ref_number, title, year, date, doc_type, repealed, tax_type
               FROM meta WHERE doc_type='law_section'
               ORDER BY
                 CAST(SUBSTR(id, 9) AS INTEGER) ASC,
                 CASE
                   WHEN id NOT LIKE '%-ท%' AND id NOT LIKE '%-ต%' AND id NOT LIKE '%-จ%'
                        AND id NOT LIKE '%-ฉ%' AND id NOT LIKE '%-ส%' AND id NOT LIKE '%-อ%'
                        AND id NOT LIKE '%-น%' AND id NOT LIKE '%-ป%' AND id NOT LIKE '%-โ%'
                        AND id NOT LIKE '%-เ%' THEN 1
                   WHEN id LIKE '%-ทวิ'      THEN 2
                   WHEN id LIKE '%-ตรี'      THEN 3
                   WHEN id LIKE '%-จัตวา'    THEN 4
                   WHEN id LIKE '%-เบญจ'     THEN 5
                   WHEN id LIKE '%-ฉ'        THEN 6
                   WHEN id LIKE '%-สัตต'     THEN 7
                   WHEN id LIKE '%-อัฏฐ'     THEN 8
                   WHEN id LIKE '%-นว'       THEN 9
                   WHEN id LIKE '%-ทศ'       THEN 10
                   WHEN id LIKE '%-เอกาทศ'   THEN 11
                   WHEN id LIKE '%-ทวาทศ'    THEN 12
                   WHEN id LIKE '%-เตรส'     THEN 13
                   WHEN id LIKE '%-จตุทศ'    THEN 14
                   WHEN id LIKE '%-ปัณรส'    THEN 15
                   WHEN id LIKE '%-โสฬส'     THEN 16
                   WHEN id LIKE '%-สัตตรส'   THEN 17
                   WHEN id LIKE '%-อัฏฐารส'  THEN 18
                   ELSE 99
                 END ASC"""
        ).fetchall()
        sort_mode = 'section'
    else:
        doc_ids = [r['doc_id'] for r in db.execute(
            'SELECT DISTINCT doc_id FROM law_links WHERE parent_law=?', (law_name,)
        ).fetchall()]

        if not doc_ids:
            db.close()
            return jsonify({'results': [], 'total': 0, 'law': law_name, 'page': page,
                            'per_page': PER, 'sort_mode': 'date'})

        ph = ','.join('?' * len(doc_ids))
        rows = db.execute(
            f'''SELECT id, ref_number, title, year, date, doc_type, repealed, tax_type
                FROM meta WHERE id IN ({ph})
                ORDER BY date ASC, id ASC''',
            doc_ids
        ).fetchall()
        sort_mode = 'date'

    total = len(rows)
    page_rows = rows[(page - 1) * PER: page * PER]

    # doc_ids ในหน้านี้ที่มี amended_by
    page_ids = [r['id'] for r in page_rows]
    amended_ids: set[str] = set()
    if page_ids:
        ph2 = ','.join('?' * len(page_ids))
        amended_ids = {r['doc_id'] for r in db.execute(
            f"SELECT DISTINCT doc_id FROM doc_relations WHERE doc_id IN ({ph2}) AND relation='amended_by'",
            page_ids
        ).fetchall()}

    results = [{
        'id':           r['id'],
        'ref_number':   r['ref_number'] or '',
        'title':        r['title'] or '',
        'year':         r['year'],
        'date':         r['date'] or '',
        'doc_type':     r['doc_type'] or '',
        'repealed':     bool(r['repealed']),
        'has_amendment': r['id'] in amended_ids,
        'tax_type':     r['tax_type'].split(',') if r['tax_type'] else [],
    } for r in page_rows]

    db.close()
    return jsonify({
        'results':   results,
        'total':     total,
        'page':      page,
        'per_page':  PER,
        'law':       law_name,
        'sort_mode': sort_mode,
    })


QDRANT_PATH = os.path.expanduser(
    '~/Desktop/คลังกฎหมาย ภาษี/กฎหมายภาษีสรรพากร/.qdrant_index'
)
# v2 = text-embedding-3-small 1536-dim ผ่าน law_search.py
# (เดิม 'law_th' = nomic-embed 768-dim — เลิกใช้ เพราะให้ vector เดิมทุก Thai query)
QDRANT_COLLECTION = 'law_th_v2'


def _load_embed_env():
    """โหลด GITHUB_TOKEN* จาก ~/.zshrc — แอปที่เปิดผ่าน .command ไม่สืบทอด shell env"""
    need = ('GITHUB_TOKEN', 'GITHUB_TOKEN_2', 'GITHUB_TOKEN_3')
    if any(os.getenv(k) for k in need):
        return
    try:
        import subprocess
        out = subprocess.run(['zsh', '-c', 'source ~/.zshrc 2>/dev/null && env'],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            k, _, v = line.partition('=')
            if k in need:
                os.environ.setdefault(k, v)
    except Exception:
        pass


_load_embed_env()
if os.path.expanduser('~/scripts') not in sys.path:
    sys.path.insert(0, os.path.expanduser('~/scripts'))

# in-memory cache สำหรับ vector matrix (โหลดครั้งเดียวตอน request แรก ~1.5s)
_VEC_CACHE: dict = {'V_unit': None, 'payloads': None}


def _load_vec_files():
    """โหลด vector cache จากไฟล์ export (.vec_cache.npz จาก GitHub Release) — สำหรับ cloud ที่ไม่มี Qdrant"""
    import gzip
    import numpy as np
    vec_f = os.path.join(DATA_ROOT, '.vec_cache.npz')
    pay_f = os.path.join(DATA_ROOT, '.vec_payloads.json.gz')
    if not (os.path.exists(vec_f) and os.path.exists(pay_f)):
        return None, None
    V = np.load(vec_f)['vectors'].astype(np.float32)
    norms = np.linalg.norm(V, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    with gzip.open(pay_f, 'rt', encoding='utf-8') as f:
        payloads = json.load(f)
    return V / norms, payloads


def _get_vec_cache():
    """โหลด vectors ครั้งแรก แล้ว cache ตลอด process — cloud: ไฟล์ก่อน / local: Qdrant สดก่อน"""
    if _VEC_CACHE['V_unit'] is not None:
        return _VEC_CACHE['V_unit'], _VEC_CACHE['payloads']
    if EDIT_BACKEND == 'github':
        try:
            V, payloads = _load_vec_files()
            if V is not None:
                _VEC_CACHE['V_unit'], _VEC_CACHE['payloads'] = V, payloads
                return V, payloads
        except Exception:
            pass
    try:
        import numpy as np
        from qdrant_client import QdrantClient
        client = QdrantClient(path=QDRANT_PATH)
        payloads: list[dict] = []
        vectors: list[list[float]] = []
        offset = None
        while True:
            batch, next_offset = client.scroll(
                QDRANT_COLLECTION, limit=500, offset=offset,
                with_vectors=True, with_payload=True,
            )
            for pt in batch:
                if pt.vector and pt.payload:
                    payloads.append(pt.payload)
                    vectors.append(pt.vector)
            offset = next_offset
            if offset is None:
                break
        if not vectors:
            return None, None
        V = np.array(vectors, dtype=np.float32)
        norms = np.linalg.norm(V, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        V_unit = V / norms
        _VEC_CACHE['V_unit'] = V_unit
        _VEC_CACHE['payloads'] = payloads
        return V_unit, payloads
    except Exception:
        try:
            V, payloads = _load_vec_files()
            if V is not None:
                _VEC_CACHE['V_unit'], _VEC_CACHE['payloads'] = V, payloads
                return V, payloads
        except Exception:
            pass
        return None, None


def _bm25_search(q: str, doc_type: str = '', limit: int = 50) -> list[str]:
    """คืน list of doc_id เรียงตาม BM25 rank (ดีสุดก่อน) — AND fallback OR"""
    tokens = tokenize_query(q)
    if not tokens:
        return []
    db = get_db()
    try:
        def _run(mode):
            fts_q = build_fts_query(tokens, mode)
            base = ('SELECT m.id FROM fts JOIN meta m ON fts.doc_id = m.id '
                    'WHERE fts MATCH ?' + (' AND m.doc_type = ?' if doc_type else '') +
                    ' GROUP BY m.id ORDER BY MIN(fts.rank) ASC LIMIT ?')
            params = [fts_q] + ([doc_type] if doc_type else []) + [limit]
            return [r['id'] for r in db.execute(base, params).fetchall()]

        ids = _run('AND')
        if len(ids) < 5:
            seen = set(ids)
            for oid in _run('OR'):
                if oid not in seen:
                    ids.append(oid)
                    seen.add(oid)
        return ids
    except Exception:
        return []
    finally:
        db.close()


def _embed_query(text: str) -> list[float] | None:
    """embed ด้วย GitHub Models text-embedding-3-small (ผ่าน ~/scripts/law_search.py)
    — คืน None ถ้าไม่มี token/เรียกไม่สำเร็จ"""
    try:
        import law_search as _ls
        return _ls.embed(text)
    except Exception:
        return None


@app.route('/api/semantic_search', methods=['POST'])
def semantic_search():
    """Semantic search ด้วย Qdrant law_th_v2 (text-embedding-3-small ผ่าน GitHub Models)"""
    data     = request.get_json(force=True) or {}
    q        = data.get('q', '').strip()
    top_n    = min(int(data.get('top_n', 10)), 30)
    doc_type = data.get('doc_type', '').strip()

    if not q:
        return jsonify({'error': 'กรุณาระบุคำค้นหา'}), 400

    # 1) Embed query
    vec = _embed_query(q)
    if vec is None:
        return jsonify({'error': 'embed query ไม่สำเร็จ — ตรวจสอบ GITHUB_TOKEN ใน ~/.zshrc และการเชื่อมต่ออินเทอร์เน็ต'}), 503

    # 2) ค้น vector cache ด้วย matrix cosine (เร็วกว่า scroll แบบ per-request ~80x)
    try:
        import numpy as np
        V_unit, all_payloads = _get_vec_cache()
        if V_unit is None:
            return jsonify({'error': 'ไม่สามารถโหลด vector index ได้'}), 500

        q_arr = np.array(vec, dtype=np.float32)
        q_unit = q_arr / (np.linalg.norm(q_arr) or 1.0)
        scores = V_unit @ q_unit  # shape (N,)

        if doc_type:
            mask = np.array([p.get('type', '') == doc_type for p in all_payloads])
            scores = np.where(mask, scores, -2.0)

        top_idx = np.argsort(-scores)[:top_n]
        top = [(float(scores[i]), all_payloads[i]) for i in top_idx if scores[i] > -1.5]

    except Exception as e:
        return jsonify({'error': f'Vector error: {e}'}), 500

    if not top:
        return jsonify({'results': [], 'total': 0})

    # 3) ดึง meta จาก SQLite โดยใช้ doc_id จาก payload
    doc_ids   = [payload.get('doc_id', '') for _, payload in top]
    score_map = {payload.get('doc_id', ''): round(score * 100, 2) for score, payload in top}

    db = get_db()
    ph = ','.join('?' * len(doc_ids))
    meta_rows = {
        r['id']: r
        for r in db.execute(
            f'SELECT id, ref_number, title, year, tax_type, date, source_url, doc_type, repealed '
            f'FROM meta WHERE id IN ({ph})',
            doc_ids,
        ).fetchall()
    }
    db.close()

    results = []
    for doc_id in doc_ids:
        r = meta_rows.get(doc_id)
        score = score_map.get(doc_id, 0)
        if r:
            tax_raw = r['tax_type'] or ''
            results.append({
                'id':           r['id'],
                'ref_number':   r['ref_number'] or '',
                'title':        r['title'] or '',
                'year':         r['year'],
                'tax_type':     tax_raw.split(',') if tax_raw else [],
                'date':         r['date'] or '',
                'source_url':   r['source_url'] or '',
                'doc_type':     r['doc_type'] or '',
                'repealed':     bool(r['repealed']),
                'score':        score,
                'snippet':      '',
            })
        else:
            # doc อยู่ใน Qdrant แต่ยังไม่ได้ index ใน SQLite (ไฟล์ใหม่ก่อน rebuild)
            hit_payload = next((p for _, p in top if p.get('doc_id') == doc_id), {})
            results.append({
                'id':         doc_id,
                'ref_number': hit_payload.get('ref_number', ''),
                'title':      hit_payload.get('title', doc_id),
                'year':       0,
                'tax_type':   [],
                'date':       hit_payload.get('date', ''),
                'source_url': hit_payload.get('source_url', ''),
                'doc_type':   hit_payload.get('type', ''),
                'repealed':   False,
                'score':      score,
                'snippet':    hit_payload.get('plain_summary', '')[:200],
            })

    return jsonify({'results': results, 'total': len(results), 'query': q})


@app.route('/api/hybrid_search', methods=['POST'])
def hybrid_search():
    """Hybrid search: RRF fusion ของ BM25 + Vector — ต้องการ Ollama รัน"""
    data     = request.get_json(force=True) or {}
    q        = data.get('q', '').strip()
    top_n    = min(int(data.get('top_n', 10)), 30)
    doc_type = data.get('doc_type', '').strip()

    if not q:
        return jsonify({'error': 'กรุณาระบุคำค้นหา'}), 400

    import numpy as np
    CAND = top_n * 5

    # 1) BM25 candidates
    bm25_ids = _bm25_search(q, doc_type=doc_type, limit=CAND)

    # 2) Vector candidates
    vec = _embed_query(q)
    vec_ids: list[str] = []
    vec_score_map: dict[str, float] = {}
    V_unit, all_payloads = _get_vec_cache()
    if V_unit is not None and vec is not None:
        q_arr = np.array(vec, dtype=np.float32)
        q_unit = q_arr / (np.linalg.norm(q_arr) or 1.0)
        scores = V_unit @ q_unit
        if doc_type:
            mask = np.array([p.get('type', '') == doc_type for p in all_payloads])
            scores = np.where(mask, scores, -2.0)
        for i in np.argsort(-scores)[:CAND]:
            if scores[i] > -1.5:
                did = all_payloads[i].get('doc_id', '')
                if did:
                    vec_ids.append(did)
                    vec_score_map[did] = float(scores[i])

    if not bm25_ids and not vec_ids:
        return jsonify({'results': [], 'total': 0, 'query': q})

    # 3) Reciprocal Rank Fusion (k=60) — BM25 weight=2 เพื่อให้คำตรงกับกฎหมายชนะ
    K = 60
    BM25_W = 2.0  # exact legal term matching สำคัญกว่า semantic
    VEC_W  = 1.0
    bm25_rank = {did: i + 1 for i, did in enumerate(bm25_ids)}
    vec_rank  = {did: i + 1 for i, did in enumerate(vec_ids)}
    all_ids   = list(dict.fromkeys(bm25_ids + vec_ids))
    rrf: dict[str, float] = {}
    for did in all_ids:
        s = 0.0
        if did in bm25_rank: s += BM25_W / (K + bm25_rank[did])
        if did in vec_rank:  s += VEC_W  / (K + vec_rank[did])
        rrf[did] = s
    top_ids = sorted(rrf, key=lambda x: -rrf[x])[:top_n]

    # 4) Fetch metadata + snippets
    db = get_db()
    ph = ','.join('?' * len(top_ids))
    meta_rows = {r['id']: r for r in db.execute(
        f'SELECT id, ref_number, title, year, tax_type, date, source_url, doc_type, repealed,'
        f' summary, facts, ruling_text FROM meta WHERE id IN ({ph})',
        top_ids,
    ).fetchall()}
    db.close()

    tokens = tokenize_query(q)
    results = []
    for did in top_ids:
        r = meta_rows.get(did)
        if not r:
            continue
        snippet_src = r['ruling_text'] or r['facts'] or r['summary'] or ''
        results.append({
            'id':         r['id'],
            'ref_number': r['ref_number'] or '',
            'title':      r['title'] or '',
            'year':       r['year'],
            'tax_type':   r['tax_type'].split(',') if r['tax_type'] else [],
            'date':       r['date'] or '',
            'source_url': r['source_url'] or '',
            'doc_type':   r['doc_type'] or '',
            'repealed':   bool(r['repealed']),
            'snippet':    highlight_snippet(snippet_src, tokens),
            'rrf_score':  round(rrf.get(did, 0) * 1000, 2),
            'vec_score':  round(vec_score_map.get(did, 0) * 100, 1),
            'in_bm25':    did in bm25_rank,
            'in_vec':     did in vec_rank,
        })

    return jsonify({'results': results, 'total': len(results), 'query': q})


MODELS = {
    # ── DeepSeek (updated May 2026) ───────────────────────────────
    'deepseek-v4-flash': {'provider': 'deepseek', 'label': 'DeepSeek V4 Flash', 'base_url': 'https://api.deepseek.com', 'no_json_mode': True, 'max_tokens': 8000},
    'deepseek-v4-pro':   {'provider': 'deepseek', 'label': 'DeepSeek V4 Pro',   'base_url': 'https://api.deepseek.com', 'no_json_mode': True, 'max_tokens': 8000},
    # ── Google Gemini (updated May 2026) ─────────────────────────
    'gemini-3.1-flash-lite': {'provider': 'google', 'label': 'Gemini 3.1 Flash Lite (ฟรี)', 'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai', 'no_json_mode': True},
    'gemini-2.5-flash':      {'provider': 'google', 'label': 'Gemini 2.5 Flash',            'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai', 'no_json_mode': True},
    'gemini-2.5-pro':        {'provider': 'google', 'label': 'Gemini 2.5 Pro',              'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai', 'no_json_mode': True},
    'gemini-3.1-pro-preview':{'provider': 'google', 'label': 'Gemini 3.1 Pro Preview',      'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai', 'no_json_mode': True},
    # ── Groq (updated May 2026) ───────────────────────────────────
    'meta-llama/llama-4-scout-17b-16e-instruct': {'provider': 'groq', 'label': 'Llama 4 Scout 17B (Groq ฟรี)', 'base_url': 'https://api.groq.com/openai/v1'},
    'llama-3.3-70b-versatile':                   {'provider': 'groq', 'label': 'Llama 3.3 70B (Groq ฟรี)',     'base_url': 'https://api.groq.com/openai/v1'},
    'llama-3.1-8b-instant':                      {'provider': 'groq', 'label': 'Llama 3.1 8B (Groq ฟรี)',      'base_url': 'https://api.groq.com/openai/v1'},
    # ── Mistral AI (updated May 2026) ─────────────────────────────
    'mistral-small-2506':   {'provider': 'mistral', 'label': 'Mistral Small',          'base_url': 'https://api.mistral.ai'},
    'mistral-large-2411':   {'provider': 'mistral', 'label': 'Mistral Large',          'base_url': 'https://api.mistral.ai'},
    'magistral-small-2506': {'provider': 'mistral', 'label': 'Magistral Small',        'base_url': 'https://api.mistral.ai'},
    'magistral-medium-2506':{'provider': 'mistral', 'label': 'Magistral Medium',       'base_url': 'https://api.mistral.ai'},
    # ── Anthropic Claude (updated May 2026) ──────────────────────
    'claude-haiku-4-5-20251001': {'provider': 'anthropic', 'label': 'Claude Haiku 4.5',  'base_url': None},
    'claude-sonnet-4-6':         {'provider': 'anthropic', 'label': 'Claude Sonnet 4.6', 'base_url': None},
    'claude-opus-4-7':           {'provider': 'anthropic', 'label': 'Claude Opus 4.7',   'base_url': None},
    # ── Ollama (local, no API key) ────────────────────────────────
    'gemma3:4b':  {'provider': 'ollama', 'label': 'Gemma 3 4B (Local, เร็ว)',  'base_url': 'http://localhost:11434/v1', 'no_json_mode': True},
    'qwen3:4b':   {'provider': 'ollama', 'label': 'Qwen 3 4B (Local, ช้า)',   'base_url': 'http://localhost:11434/v1', 'no_json_mode': True},
}

OLLAMA_BASE_URL = 'http://localhost:11434/v1'
OLLAMA_EXPAND_MODEL = 'gemma3:4b'


def check_ollama() -> bool:
    """ตรวจว่า Ollama รันอยู่หรือไม่"""
    try:
        req = urllib.request.Request('http://localhost:11434/api/tags', method='GET')
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def call_openai_compat(api_key, model, messages, base_url, json_mode=False, max_tokens=1500):
    """Generic OpenAI-compatible API (DeepSeek, Google Gemini, Groq, Mistral)"""
    model_cfg = MODELS.get(model, {})
    use_json_mode = json_mode and not model_cfg.get('no_json_mode', False)
    effective_max_tokens = model_cfg.get('max_tokens', max_tokens)

    payload_dict = {
        "model": model,
        "messages": messages,
        "temperature": 0.1 if json_mode else 0.3,
        "max_tokens": effective_max_tokens,
    }
    if use_json_mode:
        payload_dict["response_format"] = {"type": "json_object"}
    payload = json.dumps(payload_dict).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        },
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw_bytes = resp.read()
    data = json.loads(raw_bytes.decode('utf-8'))
    msg = data['choices'][0]['message']
    # Reasoning models put answer in reasoning_content when content is empty
    content = msg.get('content') or msg.get('reasoning_content') or ''
    if not content:
        import sys
        print(f'[DEBUG] {model} returned empty content. msg keys={list(msg.keys())} data={json.dumps(data, ensure_ascii=False)[:600]}', file=sys.stderr)
        raise ValueError(f'API returned empty content. Keys in message: {list(msg.keys())}. Raw: {raw_bytes.decode("utf-8","ignore")[:400]}')
    return content


def call_anthropic(api_key, model, prompt):
    """Call Anthropic Claude API"""
    # Append JSON instruction for Claude
    full_prompt = prompt + "\n\nสำคัญ: ตอบเป็น JSON เท่านั้น ห้ามมีข้อความอื่นนอก JSON"

    payload = json.dumps({
        "model": model,
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": full_prompt}],
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
        },
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return data['content'][0]['text']




def call_anthropic_chat(api_key, model, messages, system='', max_tokens=2000):
    """Call Anthropic API with messages array and system prompt (multi-turn)"""
    payload_dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload_dict["system"] = system
    payload = json.dumps(payload_dict).encode('utf-8')
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
        },
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return data['content'][0]['text']


def call_openai_compat_stream(api_key, model, messages, base_url):
    """Generator: stream text chunks from OpenAI-compatible API"""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 2000,
        "stream": True,
    }).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        },
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw_line in resp:
            line = raw_line.decode('utf-8').strip()
            if not line.startswith('data: '):
                continue
            data_str = line[6:]
            if data_str == '[DONE]':
                return
            try:
                chunk = json.loads(data_str)
                content = chunk['choices'][0]['delta'].get('content', '')
                if content:
                    yield content
            except Exception:
                pass


def call_anthropic_stream(api_key, model, messages, system='', max_tokens=2000):
    """Generator: stream text chunks from Anthropic API"""
    payload_dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "stream": True,
    }
    if system:
        payload_dict["system"] = system
    payload = json.dumps(payload_dict).encode('utf-8')
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
        },
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw_line in resp:
            line = raw_line.decode('utf-8').strip()
            if not line.startswith('data: '):
                continue
            data_str = line[6:]
            try:
                event = json.loads(data_str)
                if event.get('type') == 'content_block_delta':
                    text = event.get('delta', {}).get('text', '')
                    if text:
                        yield text
            except Exception:
                pass


def search_tavily(api_key, query, max_results=5):
    """Search using Tavily API"""
    payload = json.dumps({
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.tavily.com/search',
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    results = []
    for r in data.get('results', []):
        results.append({
            'title':   r.get('title', ''),
            'url':     r.get('url', ''),
            'snippet': (r.get('content', '') or '')[:500],
        })
    return results


def search_ddg(query, max_results=5):
    """Search using DuckDuckGo (no API key required)"""
    if not HAS_DDG:
        return []
    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    'title':   r.get('title', ''),
                    'url':     r.get('href', ''),
                    'snippet': (r.get('body', '') or '')[:500],
                })
        return results
    except Exception:
        return []


@app.route('/api/models')
def get_models():
    return jsonify(MODELS)


@app.route('/api/ollama_status')
def ollama_status():
    running = check_ollama()
    return jsonify({'running': running, 'model': OLLAMA_EXPAND_MODEL})


@app.route('/api/expand_query', methods=['POST'])
def expand_query():
    """Ollama: สกัด keywords กฎหมายภาษีจากคำถามภาษาธรรมชาติ เพื่อใช้ค้น BM25"""
    data     = request.get_json(force=True) or {}
    question = data.get('q', '').strip()
    model    = data.get('model', OLLAMA_EXPAND_MODEL).strip()

    if not question:
        return jsonify({'error': 'ต้องระบุคำถาม'}), 400

    if not check_ollama():
        return jsonify({'error': 'Ollama ไม่ได้รัน — เปิด Ollama.app ก่อน'}), 503

    prompt = (
        f'สกัดคำกฎหมายภาษีไทย ตัดคำทั่วไปออก เน้นคำที่ค้นฐานข้อมูลได้\n\n'
        f'ตัวอย่าง:\n'
        f'คำถาม: บริษัทจ่ายค่าเช่าต้องหักภาษีไหม\n'
        f'JSON: {{"keywords":["ค่าเช่า","หัก ณ ที่จ่าย","ผู้เช่า"],"tax_type":"WHT"}}\n\n'
        f'คำถาม: ออกใบกำกับภาษีเกินกำหนดมีโทษอย่างไร\n'
        f'JSON: {{"keywords":["ใบกำกับภาษี","เกินกำหนด","โทษ"],"tax_type":"VAT"}}\n\n'
        f'คำถาม: {question}\n'
        f'JSON (ตอบ JSON เท่านั้น สกัด 3-6 คำ):'
    )

    try:
        raw = call_openai_compat(
            api_key='ollama',
            model=model,
            messages=[{'role': 'user', 'content': prompt}],
            base_url=OLLAMA_BASE_URL,
            json_mode=False,
            max_tokens=120,
        )
        parsed    = extract_json(raw)
        keywords  = [k.strip() for k in parsed.get('keywords', []) if k.strip()]
        tax_type  = parsed.get('tax_type', '').strip()
        # ล้าง tax_type ที่ไม่ใช่ค่าที่ถูก
        valid_tax = {'VAT','PIT','CIT','WHT','SBT','Stamp'}
        if tax_type not in valid_tax:
            tax_type = ''
        return jsonify({'keywords': keywords, 'tax_type': tax_type, 'original': question})
    except Exception as e:
        return jsonify({'error': f'Ollama ตอบไม่ได้: {e}'}), 500



@app.route('/api/ai_search', methods=['POST'])
def ai_search():
    """AI Mode: BM25 pre-filter top 30 → AI re-rank + explain"""
    data     = request.get_json(force=True) or {}
    q        = data.get('q', '').strip()
    api_key  = data.get('api_key', '').strip()
    model    = data.get('model', 'deepseek-chat').strip()
    tax      = data.get('tax', '').strip()
    year_min = data.get('year_min', '')
    year_max = data.get('year_max', '')
    doc_type = data.get('doc_type', '').strip()

    if not q:
        return jsonify({'error': 'กรุณาระบุคำค้นหา'}), 400
    if not api_key:
        return jsonify({'error': 'กรุณาระบุ API Key'}), 400
    if model not in MODELS:
        return jsonify({'error': f'ไม่รู้จักโมเดล: {model}'}), 400

    # ── Cache check ──────────────────────────────────────────────
    ck = _cache_key('ai_search', q, model, tax, year_min, year_max, doc_type)
    cached = _cache_get(ck)
    if cached:
        return jsonify(cached)

    provider = MODELS[model]['provider']

    # ── Step 1: BM25 pre-filter top 30 ──────────────────────────
    tokens = tokenize_query(q)
    if not tokens:
        return jsonify({'error': 'ไม่สามารถแยกคำค้นหาได้'}), 400

    db = get_db()
    used_or_fallback = False

    def run_fts_ai(tok_list, mode, limit=30):
        fts_q = build_fts_query(tok_list, mode)
        sql = '''
            SELECT m.id, m.ref_number, m.title, m.year, m.tax_type,
                   m.date, m.source_url, m.summary, m.facts, m.ruling_text,
                   m.doc_type, MIN(fts.rank) as rank
            FROM fts
            JOIN meta m ON fts.doc_id = m.id
            WHERE fts MATCH ?
            GROUP BY m.id
        '''
        params = [fts_q]
        sql, params = _apply_filters(sql, params, tax, year_min, year_max, doc_type)
        order_by = _HIERARCHY_ORDER.replace('fts.rank', 'rank')
        sql += f' ORDER BY {order_by} LIMIT ?'
        params.append(limit)
        return db.execute(sql, params).fetchall()

    try:
        candidates = run_fts_ai(tokens, 'AND', 30)
        if len(candidates) < 5:
            # OR fallback with synonym expansion
            expanded = tokens + get_synonym_extras(tokens)
            candidates = run_fts_ai(expanded, 'OR', 30)
            used_or_fallback = True
    except Exception:
        expanded = tokens + get_synonym_extras(tokens)
        candidates = run_fts_ai(expanded, 'OR', 30)
        used_or_fallback = True

    db.close()

    if not candidates:
        return jsonify({'results': [], 'total': 0, 'ai_summary': 'ไม่พบข้อหารือที่เกี่ยวข้องจากการค้นหาเบื้องต้น',
                        'used_or_fallback': used_or_fallback})

    # ── Step 2: Build prompt ─────────────────────────────────────
    # Build ref_number → candidate lookup map
    ref_map = {r['ref_number']: r for r in candidates}

    rulings_text = ''
    for r in candidates:
        snippet = r['ruling_text'] or r['facts'] or r['summary'] or ''
        snippet = snippet[:500].replace('\n', ' ')
        rulings_text += (
            f"\nref_number: {r['ref_number']} | ปี: {r['year']}\n"
            f"   เรื่อง: {r['title']}\n"
            f"   เนื้อหา: {snippet}\n"
        )

    prompt = f"""คุณเป็นผู้เชี่ยวชาญด้านภาษีอากรของประเทศไทย

คำถามของผู้ใช้: "{q}"

ต่อไปนี้คือข้อหารือภาษีอากรที่ค้นพบจากระบบ จำนวน {len(candidates)} รายการ:
{rulings_text}

กรุณาวิเคราะห์และเลือกเฉพาะข้อหารือที่เกี่ยวข้องกับคำถามโดยตรง (ไม่จำเป็นต้องครบ 5-8 ถ้าไม่มีที่เกี่ยวข้อง)
— ให้คะแนนความเกี่ยวข้อง 1-10 (10 = เกี่ยวข้องมากที่สุด)
— อธิบายสั้นๆ (1-2 ประโยค) เป็นภาษาไทย ว่าแต่ละข้อหารือเกี่ยวข้องกับคำถามอย่างไร
— ถ้าไม่มีข้อหารือใดเกี่ยวข้องโดยตรง ให้ results เป็น []

ตอบเฉพาะ JSON format ดังนี้ ห้ามมีข้อความอื่นนอก JSON:
{{
  "results": [
    {{"ref_number": "กค 0702/1234", "relevance_score": 9, "relevance": "เหตุผลที่เกี่ยวข้อง..."}},
    {{"ref_number": "กค 0811/5678", "relevance_score": 7, "relevance": "เหตุผลที่เกี่ยวข้อง..."}}
  ],
  "summary": "สรุปภาพรวมของข้อหารือที่เกี่ยวข้อง 1-2 ประโยค"
}}"""

    # ── Step 3: Call AI API ──────────────────────────────────────
    try:
        if provider == 'anthropic':
            ai_content = call_anthropic(api_key, model, prompt)
        else:
            base_url = MODELS[model]['base_url']
            ai_content = call_openai_compat(
                api_key, model,
                [{"role": "user", "content": prompt}],
                base_url, json_mode=True,
            )
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        if e.code == 401:
            label = MODELS[model]['label']
            return jsonify({'error': f'API Key ไม่ถูกต้องสำหรับ {label} กรุณาตรวจสอบ'}), 401
        return jsonify({'error': f'API Error {e.code}: {body[:300]}'}), 500
    except Exception as e:
        return jsonify({'error': f'ไม่สามารถเชื่อมต่อ AI ได้: {str(e)}'}), 500

    # ── Step 4: Parse AI response ────────────────────────────────
    try:
        ai_data    = extract_json(ai_content)
        ai_results = ai_data.get('results', [])
        ai_summary = ai_data.get('summary', '')
    except Exception as ex:
        return jsonify({
            'error': f'AI ตอบผิดรูปแบบ — อาจชื่อ model ไม่ถูกต้อง หรือ model ไม่รองรับ JSON mode\nรายละเอียด: {ex}',
            'raw': ai_content[:800],
        }), 500

    # ── Step 5: Build final results (lookup by ref_number) ───────
    results = []
    for item in ai_results:
        score = item.get('relevance_score', 0)
        if score < 5:
            continue  # กรองรายการที่ AI ให้คะแนนต่ำออก
        ref = item.get('ref_number', '')
        r = ref_map.get(ref)
        if r is None:
            # compat: ถ้า AI ยังส่ง index มาให้ fallback
            idx = item.get('index', 0) - 1
            if 0 <= idx < len(candidates):
                r = candidates[idx]
        if r is None:
            continue
        snippet_src = r['ruling_text'] or r['facts'] or r['summary'] or ''
        results.append({
            'id':               r['id'],
            'ref_number':       r['ref_number'],
            'title':            r['title'],
            'year':             r['year'],
            'tax_type':         r['tax_type'].split(',') if r['tax_type'] else [],
            'date':             r['date'],
            'source_url':       r['source_url'],
            'snippet':          snippet_src[:400],
            'relevance':        item.get('relevance', ''),
            'relevance_score':  score,
        })

    payload = {
        'results':          results,
        'total':            len(results),
        'ai_summary':       ai_summary,
        'tokens':           tokens,
        'used_or_fallback': used_or_fallback,
    }
    _cache_set(ck, payload)
    return jsonify(payload)


@app.route('/api/ai_answer', methods=['POST'])
def ai_answer():
    """AI Answer Mode: BM25 pre-filter → AI reads full content → answers + cites rulings"""
    data     = request.get_json(force=True) or {}
    q        = data.get('q', '').strip()
    api_key  = data.get('api_key', '').strip()
    model    = data.get('model', 'deepseek-chat').strip()
    tax      = data.get('tax', '').strip()
    year_min = data.get('year_min', '')
    year_max = data.get('year_max', '')
    doc_type = data.get('doc_type', '').strip()

    if not q:
        return jsonify({'error': 'กรุณาระบุคำถาม'}), 400
    if not api_key:
        return jsonify({'error': 'กรุณาระบุ API Key'}), 400
    if model not in MODELS:
        return jsonify({'error': f'ไม่รู้จักโมเดล: {model}'}), 400

    # ── Cache check ──────────────────────────────────────────────
    ck = _cache_key('ai_answer', q, model, tax, year_min, year_max, doc_type)
    cached = _cache_get(ck)
    if cached:
        return jsonify(cached)

    provider = MODELS[model]['provider']

    # ── Step 1: BM25 pre-filter top 20 (ส่ง content เต็มขึ้น) ───
    tokens = tokenize_query(q)
    if not tokens:
        return jsonify({'error': 'ไม่สามารถแยกคำค้นหาได้'}), 400

    db = get_db()
    used_or_fallback = False

    def run_fts_ans(tok_list, mode, limit=20):
        fts_q = build_fts_query(tok_list, mode)
        sql = '''
            SELECT m.id, m.ref_number, m.title, m.year, m.tax_type,
                   m.date, m.source_url, m.facts, m.ruling_text,
                   m.doc_type, MIN(fts.rank) as rank
            FROM fts
            JOIN meta m ON fts.doc_id = m.id
            WHERE fts MATCH ?
            GROUP BY m.id
        '''
        params = [fts_q]
        sql, params = _apply_filters(sql, params, tax, year_min, year_max, doc_type)
        order_by = _HIERARCHY_ORDER.replace('fts.rank', 'rank')
        sql += f' ORDER BY {order_by} LIMIT ?'
        params.append(limit)
        return db.execute(sql, params).fetchall()

    try:
        candidates = run_fts_ans(tokens, 'AND', 20)
        if len(candidates) < 3:
            expanded = tokens + get_synonym_extras(tokens)
            candidates = run_fts_ans(expanded, 'OR', 20)
            used_or_fallback = True
    except Exception:
        expanded = tokens + get_synonym_extras(tokens)
        candidates = run_fts_ans(expanded, 'OR', 20)
        used_or_fallback = True

    db.close()

    if not candidates:
        return jsonify({'answer': 'ไม่พบข้อหารือที่เกี่ยวข้องกับคำถามนี้ในฐานข้อมูล',
                        'cited_rulings': [], 'all_rulings': [], 'used_or_fallback': used_or_fallback})

    # Enrich candidates with full content from JSON (แทนที่ค่าที่ถูก truncate ใน meta)
    candidates = [_enrich_from_json(dict(r)) for r in candidates]

    # ── Step 2: Build prompt จัดกลุ่มตามลำดับศักดิ์กฎหมาย ─────
    # เรียงเอกสารตามลำดับศักดิ์: ประมวล → กฎลูก → ข้อหารือ
    sorted_cands = sorted(candidates, key=lambda r: doc_hierarchy_level(r.get('doc_type', 'ruling')))

    # จัดกลุ่มเป็น section ตามลำดับศักดิ์
    groups: dict[int, list] = {}
    for r in sorted_cands:
        lvl = doc_hierarchy_level(r['doc_type'])
        groups.setdefault(lvl, []).append(r)

    group_titles = {
        1: '📘 กฎหมายแม่ — ประมวลรัษฎากร (มาตรา)',
        2: '📗 กฎหมายลูก ลำดับ 2 — พระราชกฤษฎีกา',
        3: '📗 กฎหมายลูก ลำดับ 3 — กฎกระทรวง',
        4: '📗 กฎหมายลูก ลำดับ 4 — ประกาศกระทรวงการคลัง / คำสั่งกระทรวง',
        5: '📗 กฎหมายลูก ลำดับ 5 — ประกาศอธิบดี / คำสั่งกรมสรรพากร',
        6: '📙 แนวปฏิบัติ — คำวินิจฉัยคณะกรรมการ (มีผลผูกพัน)',
        7: '📒 แนวปฏิบัติ — ข้อหารือกรมสรรพากร (เกิดเมื่อกฎหมายไม่ชัดเจน)',
        8: '⚖️  คำพิพากษาศาลภาษีอากร',
    }

    doc_context = ''
    doc_index   = 1
    for lvl in sorted(groups.keys()):
        doc_context += f'\n\n{group_titles.get(lvl, f"ระดับ {lvl}")}\n'
        doc_context += '─' * 60 + '\n'
        for r in groups[lvl]:
            _facts  = (r.get('facts')       or '')[:600].replace('\n', ' ')
            _ruling = (r.get('ruling_text') or '')[:1500].replace('\n', ' ')
            doc_context += (
                f"[{doc_index}] {doc_type_label(r.get('doc_type','ruling'))} | {r.get('ref_number','')} | ID: {r.get('id','')}\n"
                f"    เรื่อง: {r.get('title','')}\n"
                f"    อ้างอำนาจ: {_facts[:300]}\n"
                f"    เนื้อหา: {_ruling}\n\n"
            )
            doc_index += 1

    prompt = f"""คุณเป็นผู้เชี่ยวชาญด้านกฎหมายภาษีอากรของประเทศไทย

หลักการลำดับศักดิ์กฎหมายภาษีไทย:
- ประมวลรัษฎากร คือกฎหมายแม่ให้อำนาจทุกอย่าง
- พระราชกฤษฎีกา → กฎกระทรวง → ประกาศอธิบดี คือกฎหมายลูกที่ออกตามอำนาจของกฎหมายแม่
- ข้อหารือ/คำสั่ง ป. เกิดขึ้นเมื่อกฎหมายแม่และกฎหมายลูกยังไม่ชัดเจน ประชาชนจึงถามกรม กรมตอบเป็นแนวทาง
- กฎหมายหลายฉบับอาจ "ประกอบกัน" เพื่อให้สิทธิ์ครบ เช่น ม.47 + ม.42 + กฎกระทรวง 126 = สิทธิ์ลดหย่อนประกันชีวิตรวม 100,000 บาท

คำถาม: "{q}"

เอกสารจากคลังกฎหมาย (จัดเรียงตามลำดับศักดิ์):
{doc_context}

คำแนะนำในการตอบ:
1. **อ้างอิงตามลำดับศักดิ์**: เริ่มจากมาตราในประมวลรัษฎากรก่อน → กฎหมายลูก → ข้อหารือ
2. **อธิบายความเชื่อมโยง**: ชี้ให้เห็นว่าแต่ละฉบับออกตามอำนาจของฉบับใด และทำงานร่วมกันอย่างไร
3. **กรณีหลายฉบับประกอบกัน**: อธิบายว่าแต่ละฉบับให้สิทธิ์/หน้าที่อะไร และรวมกันได้เท่าใด
4. **อ้างอิงในวงเล็บ**: เช่น (มาตรา 47) (กฎกระทรวง ฉบับที่ 126) (กค 0702/1234)
5. **ข้อหารือ**: อธิบายว่าเกิดขึ้นเพราะกฎหมายข้อใดไม่ชัดเจน และกรมสรรพากรตอบอย่างไร
6. อ้างเฉพาะเอกสารที่เกี่ยวข้องจริงๆ ไม่ต้องอ้างทุกฉบับ
7. ถ้าไม่มีเอกสารใดตอบได้ตรง ให้ cited_ids เป็น [] และ confidence เป็น "low"

ตอบเป็น JSON เท่านั้น ห้ามมีข้อความอื่น:
{{
  "answer": "คำตอบฉบับเต็ม แสดงสายกฎหมายต่อเนื่อง (ใช้ \\n สำหรับขึ้นบรรทัดใหม่)",
  "law_chain_summary": "สรุปสายกฎหมายที่เกี่ยวข้อง เช่น มาตรา X → กฎกระทรวง Y → ประกาศอธิบดี Z",
  "summary": "สรุปคำตอบสั้นๆ 1-2 ประโยค",
  "cited_ids": ["id ของเอกสารที่อ้างอิงโดยตรง"],
  "confidence": "high|medium|low",
  "note": "ข้อควรระวัง เช่น ฉบับที่ถูกยกเลิกแล้ว หรือต้องตรวจสอบเพิ่มเติม (ถ้าไม่มีให้ใส่ empty string)"
}}"""

    # ── Step 3: Call AI ──────────────────────────────────────────
    try:
        if provider == 'anthropic':
            ai_content = call_anthropic(api_key, model, prompt)
        else:
            base_url = MODELS[model]['base_url']
            ai_content = call_openai_compat(
                api_key, model,
                [{"role": "user", "content": prompt}],
                base_url, json_mode=True,
            )
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        if e.code == 401:
            return jsonify({'error': f'API Key ไม่ถูกต้อง กรุณาตรวจสอบ'}), 401
        if e.code == 402:
            return jsonify({'error': 'เครดิต API หมด กรุณาเติมเงินในบัญชี'}), 402
        return jsonify({'error': f'API Error {e.code}: {body[:300]}'}), 500
    except Exception as e:
        return jsonify({'error': f'ไม่สามารถเชื่อมต่อ AI ได้: {str(e)}'}), 500

    # ── Step 4: Parse response ───────────────────────────────────
    try:
        ai_data          = extract_json(ai_content)
        answer           = ai_data.get('answer', '')
        summary          = ai_data.get('summary', '')
        law_chain_summary = ai_data.get('law_chain_summary', '')
        cited_ids        = ai_data.get('cited_ids', [])
        confidence       = ai_data.get('confidence', 'medium')
        note             = ai_data.get('note', '')
    except Exception as ex:
        return jsonify({
            'error': f'AI ตอบผิดรูปแบบ — อาจชื่อ model ไม่ถูกต้อง หรือ model ไม่รองรับ JSON mode\nรายละเอียด: {ex}',
            'raw': ai_content[:800],
        }), 500

    # ── Step 5: Build cited rulings list ─────────────────────────
    cand_map = {r.get('id'): r for r in candidates}
    cited_rulings = []
    seen = set()
    for cid in cited_ids:
        if cid in cand_map and cid not in seen:
            seen.add(cid)
            r = cand_map[cid]
            tax_raw = r.get('tax_type', '') or ''
            cited_rulings.append({
                'id':         r.get('id'),
                'ref_number': r.get('ref_number'),
                'title':      r.get('title'),
                'year':       r.get('year'),
                'tax_type':   tax_raw.split(',') if tax_raw else [],
                'date':       r.get('date'),
                'source_url': r.get('source_url'),
                'snippet':    (r.get('ruling_text') or r.get('facts') or '')[:400],
            })

    # All candidates (for reference section)
    all_rulings = []
    for r in candidates:
        tax_raw = r.get('tax_type', '') or ''
        all_rulings.append({
            'id':         r.get('id'),
            'ref_number': r.get('ref_number'),
            'title':      r.get('title'),
            'year':       r.get('year'),
            'tax_type':   tax_raw.split(',') if tax_raw else [],
            'cited':      r.get('id') in seen,
        })

    ans_payload = {
        'answer':             answer,
        'law_chain_summary':  law_chain_summary,
        'summary':            summary,
        'confidence':         confidence,
        'note':               note,
        'cited_rulings':      cited_rulings,
        'all_rulings':        all_rulings,
        'tokens':             tokens,
        'model_label':        MODELS[model]['label'],
        'used_or_fallback':   used_or_fallback,
    }
    _cache_set(ck, ans_payload)
    return jsonify(ans_payload)


def _build_chat_context(q: str, tavily_key: str, history: list) -> dict:
    """รวม BM25 + web search + build system prompt — ใช้ร่วมกันระหว่าง chat และ chat_stream"""
    tokens = tokenize_query(q)
    db_results = []
    related_rulings: list[dict] = []
    if tokens:
        try:
            db = get_db()
            fts_q = build_fts_query(tokens, 'AND')
            rows = db.execute(
                f'SELECT m.id, m.ref_number, m.title, m.year,'
                f' m.facts, m.ruling_text, m.doc_type, fts.rank'
                f' FROM fts JOIN meta m ON fts.doc_id = m.id'
                f' WHERE fts MATCH ? ORDER BY {_HIERARCHY_ORDER} LIMIT 8',
                [fts_q]).fetchall()
            if len(rows) < 3 and len(tokens) > 1:
                fts_q = build_fts_query(tokens + get_synonym_extras(tokens), 'OR')
                rows = db.execute(
                    f'SELECT m.id, m.ref_number, m.title, m.year,'
                    f' m.facts, m.ruling_text, m.doc_type, fts.rank'
                    f' FROM fts JOIN meta m ON fts.doc_id = m.id'
                    f' WHERE fts MATCH ? ORDER BY {_HIERARCHY_ORDER} LIMIT 8',
                    [fts_q]).fetchall()
            for r in rows:
                content = ' '.join(filter(None, [
                    (r['facts']       or '')[:500],
                    (r['ruling_text'] or '')[:600],
                ]))
                db_results.append({
                    'id':       r['id'],
                    'ref':      r['ref_number'],
                    'title':    r['title'],
                    'year':     r['year'],
                    'doc_type': r['doc_type'] or 'ruling',
                    'content':  content.strip(),
                })

            # แยก search เฉพาะ ข้อหารือ (ruling) ด้วย OR + expanded query
            related_rulings = find_related_rulings(q, db, limit=5)
            db.close()
        except Exception:
            pass

    web_results = []
    web_source  = 'none'
    web_query   = q + ' ภาษีอากร กรมสรรพากร ประมวลรัษฎากร'

    if tavily_key:
        try:
            web_results = search_tavily(tavily_key, web_query)
            if web_results:
                web_source = 'tavily'
        except Exception:
            pass

    if not web_results:
        web_results = search_ddg(web_query)
        web_source  = 'duckduckgo' if web_results else 'none'

    db_context = ''
    if db_results:
        db_context = '\n\n[เอกสารจากคลังกฎหมายกรมสรรพากรที่เกี่ยวข้อง]\n'
        for r in db_results:
            dtype = r.get('doc_type', 'ruling')
            label = doc_type_label(dtype)
            db_context += (
                f"• [{label}] {r['ref']} ({r['year']} พ.ศ.) — {r['title']}\n"
                f"  {r['content'][:250]}\n"
            )

    web_context = ''
    if web_results:
        web_context = '\n\n[ข้อมูลเพิ่มเติมจากอินเทอร์เน็ต]\n'
        for r in web_results[:4]:
            web_context += (
                f"• {r['title']}\n"
                f"  {r['snippet'][:250]}\n"
                f"  URL: {r['url']}\n"
            )

    system = (
        "คุณเป็นผู้เชี่ยวชาญด้านกฎหมายภาษีอากรของประเทศไทย\n\n"
        "หลักการตอบ — อ้างอิงตามลำดับศักดิ์กฎหมาย:\n"
        "1. มาตราประมวลรัษฎากร (กฎหมายแม่) — อ้างก่อนเสมอ\n"
        "2. กฎหมายลูก (พระราชกฤษฎีกา → กฎกระทรวง → ประกาศอธิบดี) — ออกตามอำนาจมาตรานั้น\n"
        "3. ข้อหารือ/คำสั่ง ป. — เกิดเมื่อกฎหมายไม่ชัดเจน กรมตอบเป็นแนวทาง\n"
        "กฎหมายหลายฉบับอาจ 'ประกอบกัน' ให้สิทธิ์ครบ ต้องอธิบายความเชื่อมโยง\n"
        "อ้างอิงในวงเล็บ เช่น (มาตรา 47) (กฎกระทรวง ฉบับที่ 126) (กค 0702/1234)\n"
        "ตอบภาษาไทย ชัดเจน ถ้ามีหลายกรณีแยกเป็นข้อ\n"
        + db_context + web_context
    )

    trimmed  = list(history)[-10:] if history else []
    messages = trimmed + [{"role": "user", "content": q}]

    return {
        'db_results':      db_results,
        'web_results':     web_results,
        'web_source':      web_source,
        'related_rulings': related_rulings,
        'system':          system,
        'messages':        messages,
    }


@app.route('/api/chat', methods=['POST'])
def chat():
    """Chat mode: BM25 local DB + Web search → AI answer with optional history"""
    data       = request.get_json(force=True) or {}
    q          = data.get('q', '').strip()
    api_key    = data.get('api_key', '').strip()
    model      = data.get('model', 'deepseek-chat').strip()
    history    = data.get('history', [])
    tavily_key = data.get('tavily_key', '').strip()

    if not q:
        return jsonify({'error': 'กรุณาระบุคำถาม'}), 400
    if not api_key:
        return jsonify({'error': 'กรุณาระบุ API Key'}), 400
    if model not in MODELS:
        return jsonify({'error': f'ไม่รู้จักโมเดล: {model}'}), 400

    provider = MODELS[model]['provider']
    ctx      = _build_chat_context(q, tavily_key, history)

    try:
        if provider == 'anthropic':
            answer = call_anthropic_chat(api_key, model, ctx['messages'], system=ctx['system'])
        else:
            base_url  = MODELS[model]['base_url']
            full_msgs = [{"role": "system", "content": ctx['system']}] + ctx['messages']
            answer    = call_openai_compat(api_key, model, full_msgs, base_url,
                                           json_mode=False, max_tokens=2000)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        if e.code == 401:
            return jsonify({'error': 'API Key ไม่ถูกต้อง กรุณาตรวจสอบ'}), 401
        if e.code == 402:
            return jsonify({'error': 'เครดิต API หมด กรุณาเติมเงิน'}), 402
        return jsonify({'error': f'API Error {e.code}: {body[:200]}'}), 500
    except Exception as e:
        return jsonify({'error': f'ไม่สามารถเชื่อมต่อ AI ได้: {str(e)}'}), 500

    db_r  = ctx['db_results']
    web_r = ctx['web_results']
    return jsonify({
        'answer':          answer,
        'db_results':      [{'id': r['id'], 'ref': r['ref'], 'title': r['title'], 'year': r['year']}
                            for r in db_r],
        'web_results':     [{'title': r['title'], 'url': r['url']} for r in web_r[:4]],
        'web_source':      ctx['web_source'],
        'related_rulings': ctx['related_rulings'],
        'model_label':     MODELS[model]['label'],
    })


@app.route('/api/chat_stream', methods=['POST'])
def chat_stream():
    """Streaming version of chat — ส่งข้อความทีละ chunk ผ่าน SSE"""
    data       = request.get_json(force=True) or {}
    q          = data.get('q', '').strip()
    api_key    = data.get('api_key', '').strip()
    model      = data.get('model', 'deepseek-chat').strip()
    history    = data.get('history', [])
    tavily_key = data.get('tavily_key', '').strip()

    if not q:
        return jsonify({'error': 'กรุณาระบุคำถาม'}), 400
    if not api_key:
        return jsonify({'error': 'กรุณาระบุ API Key'}), 400
    if model not in MODELS:
        return jsonify({'error': f'ไม่รู้จักโมเดล: {model}'}), 400

    provider = MODELS[model]['provider']
    ctx      = _build_chat_context(q, tavily_key, history)

    db_r  = ctx['db_results']
    web_r = ctx['web_results']
    meta  = {
        'type':            'meta',
        'db_results':      [{'id': r['id'], 'ref': r['ref'], 'title': r['title'], 'year': r['year']}
                            for r in db_r],
        'web_results':     [{'title': r['title'], 'url': r['url']} for r in web_r[:4]],
        'web_source':      ctx['web_source'],
        'related_rulings': ctx['related_rulings'],
        'model_label':     MODELS[model]['label'],
    }

    def generate():
        yield f'data: {json.dumps(meta, ensure_ascii=False)}\n\n'
        try:
            if provider == 'anthropic':
                for chunk in call_anthropic_stream(api_key, model, ctx['messages'],
                                                    system=ctx['system']):
                    yield f'data: {json.dumps({"type": "text", "content": chunk}, ensure_ascii=False)}\n\n'
            else:
                base_url  = MODELS[model]['base_url']
                full_msgs = [{"role": "system", "content": ctx['system']}] + ctx['messages']
                for chunk in call_openai_compat_stream(api_key, model, full_msgs, base_url):
                    yield f'data: {json.dumps({"type": "text", "content": chunk}, ensure_ascii=False)}\n\n'
            yield 'data: {"type":"done"}\n\n'
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='ignore')
            msg  = 'API Key ไม่ถูกต้อง' if e.code == 401 else f'API Error {e.code}: {body[:200]}'
            yield f'data: {json.dumps({"type": "error", "error": msg}, ensure_ascii=False)}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'},
    )


DOC_TYPE_LABELS = {
    'ruling':         'ข้อหารือ',
    'regulation':     'ระเบียบ/แนวปฏิบัติ',
    'notification':   'ประกาศ/คำสั่ง/กฎกระทรวง',
    'court_judgment': 'คำพิพากษาศาล',
    'training':       'Q&A กรมสรรพากร',
    'social_media':   'สื่อออนไลน์',
    'personal_note':  'บันทึกส่วนตัว',
    'royal_decree':   'พระราชกฤษฎีกา',
}

# ── Auto-update watcher ───────────────────────────────────────────────────────
_watcher_status = {
    'last_check':  None,
    'last_update': None,
    'last_added':  0,
    'running':     False,
}
_WATCH_INTERVAL = 30  # วินาที

def _auto_update_worker():
    global _DIKA_INDEX
    sys.path.insert(0, BASE_DIR)
    import build_index as _bi

    _watcher_status['running'] = True
    while True:
        time.sleep(_WATCH_INTERVAL)
        try:
            if not os.path.exists(_bi.DB_PATH):
                print('[auto-update] ไม่พบ DB → rebuild index...', flush=True)
                _bi.build()
                _DIKA_INDEX = None  # invalidate tree index
                _watcher_status['last_update'] = time.time()
                _watcher_status['last_check']  = time.time()
                continue

            db_mtime = os.path.getmtime(_bi.DB_PATH)
            has_newer = any(
                os.path.getmtime(fp) > db_mtime
                for d in _bi.JSON_DIRS if os.path.isdir(d)
                for fp in glob.glob(os.path.join(d, '*.json'))
            )
            _watcher_status['last_check'] = time.time()
            if has_newer:
                print('[auto-update] พบไฟล์ใหม่กว่า DB → rebuild index...', flush=True)
                _bi.build()
                _DIKA_INDEX = None  # invalidate tree index หลัง rebuild
                _watcher_status['last_update'] = time.time()
                _watcher_status['last_added']  = 0
                print('[auto-update] rebuild เสร็จแล้ว', flush=True)
            # ถ้าไม่มีไฟล์ใหม่ → ไม่ต้องทำอะไร (ประหยัด I/O 7,500+ file reads ทุก 30s)
        except Exception as e:
            print(f'[auto-update] error: {e}', flush=True)


@app.route('/api/index_status')
def index_status():
    try:
        db = get_db()
        total = db.execute('SELECT COUNT(*) FROM meta').fetchone()[0]
        type_rows = db.execute(
            'SELECT doc_type, COUNT(*) FROM meta GROUP BY doc_type ORDER BY COUNT(*) DESC'
        ).fetchall()
        db.close()
        return jsonify({
            'total':           total,
            'by_type':         {r[0]: r[1] for r in type_rows},
            'watcher_running': _watcher_status['running'],
            'last_check':      _watcher_status['last_check'],
            'last_update':     _watcher_status['last_update'],
            'last_added':      _watcher_status['last_added'],
            'watch_interval':  _WATCH_INTERVAL,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/tree')
def tree_page():
    return render_template('tree.html')


# ── Dika Section Index (lazy, built once on first request) ────────────────────
_DIKA_INDEX: dict[str, list[dict]] | None = None
_DIKA_LOCK = threading.Lock()

def _get_dika_index() -> dict[str, list[dict]]:
    global _DIKA_INDEX
    if _DIKA_INDEX is not None:
        return _DIKA_INDEX
    with _DIKA_LOCK:
        if _DIKA_INDEX is not None:
            return _DIKA_INDEX
        idx: dict[str, list[dict]] = {}
        dika_dir = os.path.join(BASE_DIR, '..', 'คำพิพากษาฎีกา')
        for fp in glob.glob(os.path.join(dika_dir, '*.json')):
            try:
                with open(fp, encoding='utf-8') as f:
                    d = json.load(f)
                secs = d.get('related_sections') or []
                for sec in secs:
                    # normalize: "ประมวลรัษฎากร มาตรา 40" → "มาตรา 40"
                    norm = re.sub(r'^ประมวลรัษฎากร\s*', '', str(sec)).strip()
                    # also strip sub-item: "มาตรา 40(1)" → "มาตรา 40"
                    base = re.sub(r'\s*\(.*', '', norm).strip()
                    for key in {norm, base}:
                        if key and key.startswith('มาตรา'):
                            idx.setdefault(key, []).append({
                                'id':    d.get('id', ''),
                                'ref':   d.get('ref_number', '') or d.get('id', ''),
                                'title': (d.get('title') or d.get('key_principle') or '')[:80],
                                'year':  d.get('year') or 0,
                            })
            except Exception:
                pass
        _DIKA_INDEX = idx
        return idx


_SEC_SUFFIX_ORDER = {
    'ทวิ': 2, 'ตรี': 3, 'จัตวา': 4, 'เบญจ': 5,
    'ฉ': 6, 'สัตต': 7, 'อัฏฐ': 8, 'นว': 9, 'ทศ': 10,
    'เอกาทศ': 11, 'ทวาทศ': 12, 'เตรส': 13, 'จตุทศ': 14,
    'ปัณรส': 15, 'โสฬส': 16, 'สัตตรส': 17, 'อัฏฐารส': 18,
}


def _sec_sort_key(row):
    """เรียงมาตราแบบตัวเลข: 1,2,...,10,10-ทวิ,11,..."""
    s = row['id'].replace('section-', '')
    parts = s.split('-')
    try:
        num = int(parts[0])
    except ValueError:
        return (9999, 0, s)
    sub = _SEC_SUFFIX_ORDER.get(parts[1], 0) if len(parts) > 1 else 0
    return (num, sub, s)


@app.route('/api/sections_list')
def sections_list():
    """คืนรายการมาตราประมวลรัษฎากรทั้งหมด เรียงตามลำดับตัวเลข พร้อม chapter/part"""
    db = get_db()
    rows = db.execute(
        "SELECT id, ref_number, title, chapter, part FROM meta WHERE doc_type='law_section'"
    ).fetchall()
    db.close()
    rows = sorted(rows, key=_sec_sort_key)
    return jsonify([{
        'id': r['id'],
        'ref': r['ref_number'],
        'title': r['title'],
        'chapter': r['chapter'] or '',
        'part': r['part'] or '',
    } for r in rows])


def _annotate_child_docs(db, docs):
    """เพิ่ม amended (bool) + repealed_by_ref (str|None) ให้ list ของ child law docs
    (แต่ละ dict ต้องมี id, ref, repealed, และ '_full_ref' — ref_number เต็มไม่ตัด — สำหรับ regex)
    โดย query doc_relations แบบ bulk (ไม่วน query ต่อแถว)"""
    if not docs:
        return docs

    ids = [d['id'] for d in docs]
    doc_nums: dict[str, int] = {}
    for d in docs:
        # ใช้ ref_number เต็ม (ไม่ตัด) เพื่อไม่ให้ "(ฉบับที่ N)" หลุดไปถ้าชื่อยาว
        m = re.search(r'ฉบับที่\s*(\d+)', d.get('_full_ref') or d.get('ref') or '')
        if m:
            doc_nums[d['id']] = int(m.group(1))

    amended_ids: set = set()
    ph = ','.join('?' * len(ids))
    for row in db.execute(
        f"SELECT DISTINCT doc_id FROM doc_relations WHERE doc_id IN ({ph}) AND relation='amended_by'", ids
    ):
        amended_ids.add(row[0])

    if doc_nums:
        num_to_ids: dict[int, list] = {}
        for _id, n in doc_nums.items():
            num_to_ids.setdefault(n, []).append(_id)
        nums = list(num_to_ids.keys())
        phn = ','.join('?' * len(nums))
        for (n,) in db.execute(
            f"SELECT DISTINCT target_num FROM doc_relations WHERE target_num IN ({phn}) AND relation='amends'", nums
        ):
            for _id in num_to_ids.get(n, []):
                amended_ids.add(_id)

    repealed_by_map: dict[str, str] = {}
    rep_ids = [d['id'] for d in docs if d.get('repealed') and d['id'] in doc_nums]
    if rep_ids:
        num_to_ids2: dict[int, list] = {}
        for _id in rep_ids:
            num_to_ids2.setdefault(doc_nums[_id], []).append(_id)
        rep_nums = list(num_to_ids2.keys())
        phr = ','.join('?' * len(rep_nums))
        rows = db.execute(f"""
            SELECT dr.target_num, m.ref_number
            FROM doc_relations dr JOIN meta m ON dr.doc_id = m.id
            WHERE dr.target_num IN ({phr}) AND dr.relation='repeals'
        """, rep_nums).fetchall()
        for tn, ref in rows:
            for _id in num_to_ids2.get(tn, []):
                repealed_by_map.setdefault(_id, ref)

    for d in docs:
        d['amended'] = d['id'] in amended_ids
        d['repealed_by_ref'] = repealed_by_map.get(d['id'])
    return docs


@app.route('/api/section_tree/<path:section_id>')
def section_tree(section_id):
    """คืน law tree สำหรับมาตราหนึ่ง — กฎหมายลูก + ฎีกา + ข้อหารือ"""
    section_id = re.sub(r'[^a-zA-Z0-9฀-๿\-\._]', '', section_id)
    db = get_db()

    # ข้อมูลมาตรา
    sec_row = db.execute(
        "SELECT id, ref_number, title, summary, ruling_text, source_url FROM meta WHERE id=? AND doc_type='law_section'",
        (section_id,)
    ).fetchone()
    if not sec_row:
        db.close()
        return jsonify({'error': 'ไม่พบมาตรานี้'}), 404

    sec_ref = sec_row['ref_number'] or section_id  # เช่น "มาตรา 40"

    # กฎหมายลูก จาก law_links
    TYPE_ORDER = {
        'royal_decree': 1, 'ministerial_regulation': 2,
        'ministry_notification': 3, 'ministry_order': 3,
        'director_general_notification': 4, 'department_order': 4,
        'department_notification': 4, 'committee_ruling': 5,
    }
    child_rows = db.execute("""
        SELECT DISTINCT m.id, m.ref_number, m.title, m.doc_type, m.year, m.repealed
        FROM law_links ll JOIN meta m ON ll.doc_id = m.id
        WHERE ll.sections LIKE ?
          AND m.doc_type NOT IN ('ruling','court_judgment','law_section','act',
                                  'tax_benefit_summary','personal_note')
        ORDER BY m.year DESC
    """, (f'%{sec_ref}%',)).fetchall()

    # จัดกลุ่มตาม doc_type (ไม่จำกัดจำนวน — ต้องแสดงกฎหมายลูกให้ครบทุกฉบับ)
    all_child_docs = [{
        'id':       r['id'],
        'ref':      (r['ref_number'] or r['id'])[:60],
        '_full_ref': r['ref_number'] or r['id'],  # ใช้ภายในสำหรับ regex เท่านั้น — ตัดออกก่อนส่ง response
        'title':    (r['title'] or '')[:70],
        'year':     r['year'] or 0,
        'repealed': bool(r['repealed']),
        'doc_type': r['doc_type'] or 'other',
    } for r in child_rows]
    _annotate_child_docs(db, all_child_docs)  # เพิ่ม amended / repealed_by_ref
    for d in all_child_docs:
        del d['_full_ref']

    groups: dict[str, list] = {}
    for d in all_child_docs:
        groups.setdefault(d['doc_type'], []).append(d)

    children = []
    TYPE_LABELS = {
        'royal_decree':                  ('📜', 'พระราชกฤษฎีกา'),
        'ministerial_regulation':        ('📋', 'กฎกระทรวง'),
        'ministry_notification':         ('🏛️', 'ประกาศกระทรวงการคลัง'),
        'ministry_order':                ('🏛️', 'คำสั่งกระทรวงการคลัง'),
        'director_general_notification': ('📢', 'ประกาศอธิบดีกรมสรรพากร'),
        'department_order':              ('📌', 'คำสั่งกรมสรรพากร'),
        'department_notification':       ('📌', 'ประกาศกรมสรรพากร'),
        'committee_ruling':              ('🔏', 'คำวินิจฉัยคณะกรรมการ'),
        'ministry_regulation_order':     ('📋', 'ระเบียบกระทรวงการคลัง'),
    }
    for dt, docs in sorted(groups.items(), key=lambda x: TYPE_ORDER.get(x[0], 9)):
        icon, label = TYPE_LABELS.get(dt, ('📄', dt))
        children.append({'doc_type': dt, 'icon': icon, 'label': label, 'docs': docs})

    # ข้อหารือ — คืนทั้งหมด (หน้าเว็บจัดการแสดง/ย่อเอง — ห้ามตัดข้อมูลที่ backend)
    ruling_rows = db.execute("""
        SELECT DISTINCT m.id, m.ref_number, m.title, m.year
        FROM law_links ll JOIN meta m ON ll.doc_id = m.id
        WHERE ll.sections LIKE ?
          AND m.doc_type = 'ruling'
        ORDER BY m.year DESC
    """, (f'%{sec_ref}%',)).fetchall()
    ruling_count_row = db.execute("""
        SELECT COUNT(DISTINCT m.id)
        FROM law_links ll JOIN meta m ON ll.doc_id = m.id
        WHERE ll.sections LIKE ? AND m.doc_type = 'ruling'
    """, (f'%{sec_ref}%',)).fetchone()

    db.close()

    rulings = [{'id': r['id'], 'ref': (r['ref_number'] or r['id'])[:60],
                'title': (r['title'] or '')[:70], 'year': r['year'] or 0}
               for r in ruling_rows]

    # ฎีกา จาก in-memory index
    dika_idx = _get_dika_index()
    dika_all = dika_idx.get(sec_ref, [])
    # deduplicate by id
    seen_ids: set = set()
    dika_dedup = []
    for d in dika_all:
        if d['id'] not in seen_ids:
            seen_ids.add(d['id'])
            dika_dedup.append(d)
    dika = sorted(dika_dedup, key=lambda x: -x['year'])  # คืนทั้งหมด ไม่ตัด

    return jsonify({
        'section': {
            'id':         sec_row['id'],
            'ref':        sec_row['ref_number'],
            'title':      sec_row['title'],
            'summary':    sec_row['summary'] or '',
            'text':       (sec_row['ruling_text'] or '')[:10000],
            'source_url': sec_row['source_url'] or '',
        },
        'children':      children,
        'rulings':       rulings,
        'ruling_count':  ruling_count_row[0] if ruling_count_row else 0,
        'dika':          dika,
        'dika_count':    len(dika_dedup),
    })


@app.route('/api/stats')
def stats():
    try:
        db = get_db()
        total = db.execute('SELECT COUNT(*) FROM meta').fetchone()[0]
        years = db.execute(
            'SELECT year, COUNT(*) as cnt FROM meta GROUP BY year ORDER BY year DESC'
        ).fetchall()
        tax_raw  = db.execute('SELECT tax_type FROM meta').fetchall()
        type_raw = db.execute(
            'SELECT doc_type, COUNT(*) as cnt FROM meta GROUP BY doc_type ORDER BY cnt DESC'
        ).fetchall()
        db.close()

        tax_counts: dict = {}
        for row in tax_raw:
            for t in (row['tax_type'] or '').split(','):
                t = t.strip()
                if t:
                    tax_counts[t] = tax_counts.get(t, 0) + 1

        doc_type_counts = [
            {
                'type':  r['doc_type'] or 'ruling',
                'label': DOC_TYPE_LABELS.get(r['doc_type'] or 'ruling', r['doc_type'] or 'ruling'),
                'count': r['cnt'],
            }
            for r in type_raw
        ]

        return jsonify({
            'total':      total,
            'years':      [{'year': r['year'], 'count': r['cnt']} for r in years],
            'tax_types':  tax_counts,
            'doc_types':  doc_type_counts,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    if not HAS_THAI:
        print("⚠ PyThaiNLP ไม่ได้ติดตั้ง — การตัดคำภาษาไทยจะไม่ทำงาน!")
        print("  ติดตั้งด้วย: pip install pythainlp")
    if not os.path.exists(DB_PATH):
        print("⚠ ยังไม่มี index! รัน build_index.py ก่อน")
        print("  python build_index.py")
    else:
        size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
        print(f"✓ พบ database ({size_mb:.1f} MB)")

    t = threading.Thread(target=_auto_update_worker, daemon=True, name='auto-updater')
    t.start()
    print(f"✓ auto-update watcher เริ่มทำงาน (ตรวจทุก {_WATCH_INTERVAL}s)")

    print("\n🚀 เปิด http://127.0.0.1:5001 ในเบราว์เซอร์")
    app.run(debug=False, host='127.0.0.1', port=5001)
