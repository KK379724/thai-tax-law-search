"""
build_index.py — สร้าง SQLite FTS5 database จาก JSON ทั้งหมด
รันครั้งเดียวก่อนใช้งาน app.py
"""
import sqlite3, json, glob, os, re, sys, time

# ── Thai numeral → Arabic ───────────────────────────────────────────────────
_THAI_DIGITS = str.maketrans('๐๑๒๓๔๕๖๗๘๙', '0123456789')

def _th2ar(s: str) -> str:
    return s.translate(_THAI_DIGITS)

# regex สำหรับ extract เลขฉบับจาก "ฉบับที่ N" (รองรับ Thai + Arabic + space variants)
_ISSUE_RE = re.compile(r'ฉบับที่\s*([๐-๙\d]+)')

# แก้ไขเพิ่มเติมโดย...ฉบับที่ N — อยู่ใน doc ที่ถูกแก้ไข (ใน parentheses เป็นหลัก)
_AMENDED_BY_RE = re.compile(
    r'\((?:ซึ่ง)?แก้ไขเพิ่มเติมโดยประกาศอธิบดี[^)]*?ฉบับที่\s*([๐-๙\d]+)'
)
# ให้ยกเลิก...ฉบับที่ N — อยู่ใน doc ที่ทำการยกเลิก
_REPEALS_RE = re.compile(
    r'ให้ยกเลิกประกาศ(?:อธิบดี|กรมสรรพากร)[^.。\n]*?ฉบับที่\s*([๐-๙\d]+)'
)
# ให้ยกเลิกความใน...ฉบับที่ N  (partial amendment — ยังบังคับใช้)
_AMENDS_RE = re.compile(
    r'ให้(?:ยกเลิกความใน|แก้ไขเพิ่มเติม(?!โดย))(?:ประกาศ(?:อธิบดี|กรมสรรพากร))?[^.。\n]*?ฉบับที่\s*([๐-๙\d]+)'
)

try:
    from pythainlp.tokenize import word_tokenize
    HAS_THAI = True
    print("✓ pythainlp พร้อมใช้งาน (Thai word segmentation)")
except ImportError:
    HAS_THAI = False
    print("⚠ ไม่พบ pythainlp — ใช้การค้นหาแบบ character-based แทน")

DB_PATH = os.getenv('LAW_DB_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rulings.db')
_BASE   = os.path.abspath(os.getenv('LAW_DATA_ROOT') or os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

# อ่านทุกโฟลเดอร์ใน _BASE (ยกเว้นโฟลเดอร์ระบบและโฟลเดอร์ปี ค.ศ./พ.ศ. 4 หลัก)
# โฟลเดอร์ปี เช่น 2540-2569 มีไฟล์ซ้ำกับ "ข้อหารือ 2569-2540" จึงต้องข้าม
_EXCLUDE = {'__pycache__', 'ข้อหารือ-search', 'ตัวจัดระเบียบไฟล json',
            'url30662'}   # ซ้ำกับ ข้อหารือ 2569-2540/
_YEAR_RE = re.compile(r'^\d{4}$')
JSON_DIRS = [
    os.path.join(_BASE, d)
    for d in sorted(os.listdir(_BASE))
    if (os.path.isdir(os.path.join(_BASE, d))
        and d not in _EXCLUDE
        and not d.startswith('.')
        and not _YEAR_RE.match(d))
]

# Label สำหรับแสดงผล
DOC_TYPE_LABELS = {
    'ruling':                          'ข้อหารือ',
    'committee_ruling':                'คำวินิจฉัยคณะกรรมการ',
    'law_section':                     'ประมวลรัษฎากร',
    'regulation':                      'ระเบียบ/แนวปฏิบัติ',
    'notification':                    'ประกาศอธิบดี',
    'director_general_notification':   'ประกาศอธิบดี',
    'department_notification':         'ประกาศกรมสรรพากร',
    'department_order':                'คำสั่งกรมสรรพากร',
    'ministry_notification':           'ประกาศกระทรวงการคลัง',
    'ministry_order':                  'คำสั่งกระทรวงการคลัง',
    'court_judgment':                  'คำพิพากษาศาล',
    'training':                        'เอกสารอบรม',
    'social_media':                    'สื่อออนไลน์',
    'personal_note':                   'บันทึกส่วนตัว',
    'royal_decree':                    'พระราชกฤษฎีกา',
    'ministerial_regulation':          'กฎกระทรวง',
    'act':                             'พระราชบัญญัติ',
}

# ลำดับศักดิ์กฎหมาย (ใช้ใน search ranking และ AI prompt)
DOC_HIERARCHY_LEVEL = {
    'law_section':                     1,  # ประมวลรัษฎากร (กฎหมายแม่)
    'act':                             1,  # พระราชบัญญัติ (กฎหมายแม่ระดับเดียวกัน)
    'royal_decree':                    2,  # พระราชกฤษฎีกา
    'ministerial_regulation':          3,  # กฎกระทรวง
    'ministry_notification':           4,  # ประกาศกระทรวงการคลัง
    'ministry_order':                  4,  # คำสั่งกระทรวงการคลัง
    'director_general_notification':   5,  # ประกาศอธิบดี
    'notification':                    5,  # ประกาศอธิบดี (เก่า)
    'department_notification':         5,  # ประกาศกรมสรรพากร
    'department_order':                5,  # คำสั่งกรมสรรพากร
    'committee_ruling':                6,  # คำวินิจฉัยคณะกรรมการ (ผูกพัน)
    'court_judgment':                  7,  # คำพิพากษาศาล (ตีความกฎหมาย > ข้อหารือ)
    'ruling':                          8,  # ข้อหารือ (แนวปฏิบัติ)
    'regulation':                      6,
    'training':                        9,
    'social_media':                    9,
    'personal_note':                   9,
}


# ── Tax type normalization ──────────────────────────────────────────────────
_TAX_NORM: dict[str, str] = {
    # Thai labels → standard codes
    'ภาษีมูลค่าเพิ่ม': 'VAT',
    'ภาษีเงินได้นิติบุคคล': 'CIT',
    'ภาษีเงินได้บุคคลธรรมดา': 'PIT',
    'ภาษีธุรกิจเฉพาะ': 'SBT',
    'ภาษีเงินได้': 'CIT',
    'อากรแสตมป์': 'Stamp',
    'ภาษีเงินได้หัก ณ ที่จ่าย': 'WHT',
    'ภาษีหัก ณ ที่จ่าย': 'WHT',
    'ภาษีเงินได้ ณ ที่จ่าย': 'WHT',
    'ภาษีการรับมรดก': 'Inheritance',
    'ภาษีเงินได้บริษัท': 'CIT',
    'ภาษีเงินได้บริษัท/ห้างหุ้นส่วน': 'CIT',
    'ภาษีนิติบุคคล': 'CIT',
    'ภาษีเงินได นิติบุคคล': 'CIT',
    'ภาษีธุรกิจขนาดใหญ่': 'SBT',
    'ธุรกิจเฉพาะ': 'SBT',
    'ภาษีธุรกรรมลักษณะเฉพาะ': 'SBT',
    'ภาษีจากเงินได้พึงประเมิน': 'PIT',
    'ภาษีเงินได้จากการขายอสังหาริมทรัพย์': 'PIT',
    'ภาษีเงินได้ปิโตรเลียม': 'PIT',
    'บัญชีพิเศษของอิเล็กทรอนิกส์แพลตฟอร์ม': 'VAT',
    'ภาษีการค้า': 'SBT',
    # English verbose → standard codes
    'Corporate Income Tax': 'CIT',
    'Personal Income Tax': 'PIT',
    'Value Added Tax': 'VAT',
    'vat': 'VAT',
    'Specific Business Tax': 'SBT',
    'Withholding Tax': 'WHT',
    'withholding_tax': 'WHT',
    'income_tax': 'CIT',
    'corporate_income_tax': 'CIT',
    'personal_income_tax': 'PIT',
    'stamp_duty': 'Stamp',
    'Income Tax': 'CIT',
    'Corporate Tax': 'CIT',
    'InheritanceTax': 'Inheritance',
    'Partnership Income Tax': 'CIT',
    'Country-by-Country Reporting': 'CIT',
}
_TAX_REMOVE = {
    'ภาษีอากร', 'ภาษี', 'ภาษีอากรทั่วไป', 'ภาษีอากรตามประมวลรัษฎากร',
    'ภาษีสรรพากร', 'ภาษีสรรพสามิต', 'ศุลกากร', 'อากรศุลกากร', 'ภาษีศุลกากร',
    'ทั่วไป', 'อื่นๆ', 'ประมวลรัษฎากร', 'วิสาหกิจเพื่อสังคม', 'เบี้ยปรับ',
    'การประเมิน', 'Excise', 'Electronic Invoicing', 'Depreciation',
    'Tax Inspection', 'International Cooperation',
    'T', 'I', 'P', 'C', 'V', 'A', 'S', 'B',
}
_TAX_STANDARD = {'PIT', 'CIT', 'VAT', 'SBT', 'WHT', 'Stamp', 'Inheritance', 'SD'}


def normalize_tax_type(raw: list | None) -> str:
    """แปลง tax_type list → comma-separated standard codes"""
    if not raw:
        return ''
    seen: list[str] = []
    for item in raw:
        item = str(item).strip()
        if not item or item in _TAX_REMOVE:
            continue
        if item in _TAX_STANDARD:
            code = item
        else:
            code = _TAX_NORM.get(item, item)
            if code in _TAX_REMOVE or not code:
                continue
            if code not in _TAX_STANDARD:
                code = item  # keep original if still unknown
        if code and code not in seen:
            seen.append(code)
    return ','.join(seen)


def tokenize_thai(text: str) -> str:
    """แปลงข้อความไทยเป็น token โดยใช้ pythainlp หรือ fallback เป็น character"""
    if not text:
        return ''
    if HAS_THAI:
        tokens = word_tokenize(text, engine='newmm', keep_whitespace=False)
        return ' '.join(t for t in tokens if t and t.strip())
    return text  # sqlite unicode61 จะ handle เอง


def extract_facts_ruling(doc_type: str, content: dict) -> tuple[str, str]:
    """
    คืน (facts, ruling_text) จาก content dict โดยรองรับทุก doc_type
    - ruling: ข้อเท็จจริง → facts, แนววินิจฉัย → ruling_text
    - regulation: วัตถุประสงค์+ขอบเขต → facts, บทบัญญัติ → ruling_text
    - notification: อาศัยอำนาจตาม → facts, บทบัญญัติ → ruling_text
    - court_judgment: ข้อเท็จจริง → facts, คำวินิจฉัย → ruling_text
    - อื่นๆ: ใช้ค่า content แรก → facts, ค่า content สุดท้าย → ruling_text
    """
    def _str(v):
        if isinstance(v, dict):
            return ' '.join(str(x) for x in v.values() if x)
        return str(v) if v else ''

    if doc_type == 'ruling':
        facts      = _str(content.get('ข้อเท็จจริง', ''))
        issue_txt  = _str(content.get('ประเด็นที่หารือ', ''))
        ruling_txt = _str(content.get('แนววินิจฉัย', ''))
        # template 01 ใช้ชื่อต่างกัน
        if not facts:
            facts = _str(content.get('ข้อหารือ', ''))
        if not facts:
            facts = _str(content.get('ข้อกฎหมาย', ''))
        return facts + ' ' + issue_txt, ruling_txt

    elif doc_type == 'regulation':
        facts = ' '.join(filter(None, [
            _str(content.get('วัตถุประสงค์', '')),
            _str(content.get('ขอบเขตการใช้บังคับ', '')),
            _str(content.get('ผู้มีหน้าที่ปฏิบัติ', '')),
        ]))
        ruling_txt = _str(content.get('บทบัญญัติ', ''))
        return facts, ruling_txt

    elif doc_type in ('notification', 'director_general_notification',
                      'department_notification', 'department_order',
                      'ministry_notification', 'ministry_order'):
        facts      = _str(content.get('อาศัยอำนาจตาม', ''))
        # ใช้ full_text ก่อน (schema ใหม่) fallback ไป บทบัญญัติ (schema เก่า)
        ruling_txt = _str(content.get('full_text', '') or content.get('บทบัญญัติ', ''))
        return facts, ruling_txt

    elif doc_type in ('ministerial_regulation', 'royal_decree'):
        facts      = _str(content.get('อาศัยอำนาจตาม', ''))
        ruling_txt = _str(content.get('full_text', '') or content.get('บทบัญญัติ', ''))
        return facts, ruling_txt

    elif doc_type == 'committee_ruling':
        facts = ' '.join(filter(None, [
            _str(content.get('ข้อเท็จจริง', '')),
            _str(content.get('ประเด็น', '')),
        ]))
        ruling_txt = ' '.join(filter(None, [
            _str(content.get('คำวินิจฉัย', '')),
            _str(content.get('เหตุผล', '')),
        ]))
        return facts, ruling_txt

    elif doc_type == 'court_judgment':
        facts      = _str(content.get('ข้อเท็จจริง', ''))
        ruling_txt = _str(content.get('คำวินิจฉัย', '') or content.get('คำพิพากษา', ''))
        return facts, ruling_txt

    elif doc_type == 'law_section':
        # explanation ใช้เป็น facts เพื่อให้ค้นหาด้วยภาษาเข้าใจง่ายได้
        facts      = _str(content.get('explanation', ''))
        ruling_txt = _str(content.get('text', ''))
        return facts, ruling_txt

    elif doc_type == 'training':
        facts      = _str(content.get('วัตถุประสงค์', '') or content.get('บทนำ', ''))
        ruling_txt = _str(content.get('เนื้อหา', '') or content.get('สรุป', ''))
        # รองรับ Q&A format: qa_items + full_text
        qa_items = content.get('qa_items', [])
        if qa_items:
            qa_text = ' '.join(
                f"Q: {item.get('q','')} A: {item.get('a','')}"
                for item in qa_items if isinstance(item, dict)
            )
            facts      = facts or qa_text[:800]
            ruling_txt = ruling_txt or qa_text
        if not ruling_txt:
            ruling_txt = _str(content.get('full_text', ''))
        return facts, ruling_txt

    else:
        # Generic fallback: ค่าแรกเป็น facts, ค่าสุดท้ายเป็น ruling_text
        values = [_str(v) for v in content.values() if v and _str(v)]
        facts      = values[0] if values else ''
        ruling_txt = values[-1] if len(values) > 1 else ''
        return facts, ruling_txt


def _update_repealed_from_relations(db):
    """ทำเครื่องหมาย meta.repealed=1 สำหรับ doc ที่ถูกยกเลิกทั้งฉบับโดย doc อื่น"""
    # หาเลขฉบับของแต่ละ doc จาก ref_number (เช่น "ฉบับที่ 38" → 38)
    rows = db.execute("SELECT id, ref_number FROM meta WHERE doc_type NOT IN ('ruling','court_judgment')").fetchall()
    ref_num_map: dict[int, str] = {}   # เลขฉบับ → doc_id
    for doc_id, ref_num in rows:
        if ref_num:
            m = _ISSUE_RE.search(ref_num)
            if m:
                n = int(_th2ar(m.group(1)))
                ref_num_map[n] = doc_id
    # อัปเดต meta.repealed
    targets = db.execute("SELECT DISTINCT target_num FROM doc_relations WHERE relation='repeals'").fetchall()
    count = 0
    for (tnum,) in targets:
        doc_id = ref_num_map.get(tnum)
        if doc_id:
            db.execute("UPDATE meta SET repealed=1 WHERE id=?", (doc_id,))
            count += 1
    if count:
        db.commit()
        print(f"  อัปเดต repealed=1 สำหรับ {count} docs")


def build():
    t0 = time.time()
    import shutil

    # Build ไปที่ temp file ก่อน — running app ยังอ่าน DB เดิมได้ระหว่าง rebuild
    tmp_path = DB_PATH + '.new'
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    db = sqlite3.connect(tmp_path)
    db.execute('PRAGMA journal_mode=WAL')

    # ตารางเก็บ metadata ดิบ (สำหรับแสดงผล)
    db.execute('''
        CREATE TABLE meta (
            id            TEXT PRIMARY KEY,
            ref_number    TEXT,
            title         TEXT,
            year          INTEGER,
            tax_type      TEXT,
            date          TEXT,
            source_url    TEXT,
            summary       TEXT,
            facts         TEXT,
            ruling_text   TEXT,
            doc_type      TEXT DEFAULT 'ruling',
            repealed      INTEGER DEFAULT 0,
            plain_summary TEXT,
            chapter       TEXT,
            part          TEXT
        )
    ''')

    # FTS5 virtual table (BM25 ranking built-in)
    db.execute('''
        CREATE VIRTUAL TABLE fts USING fts5(
            doc_id,
            title_tok,
            content_tok,
            tokenize = 'trigram'
        )
    ''')

    # ── ตาราง law_links: เชื่อมโยงกฎหมายแม่ → กฎหมายลูก ─────────────────────
    # สร้างจาก authorizing_law_chain ที่มีอยู่ใน JSON (82% ครอบคลุม)
    db.execute('''
        CREATE TABLE law_links (
            doc_id       TEXT NOT NULL,
            parent_law   TEXT NOT NULL,
            sections     TEXT,
            relationship TEXT,
            PRIMARY KEY (doc_id, parent_law)
        )
    ''')
    db.execute('CREATE INDEX idx_law_links_parent ON law_links(parent_law)')

    # ── ตาราง doc_relations: การแก้ไข/ยกเลิกระหว่าง docs ──────────────────────
    # relation: 'amended_by' | 'amends' | 'repeals'
    # amended_by: ใน full_text ของ doc นี้ เจอ "(แก้ไขเพิ่มเติมโดย...ฉบับที่ N)"
    # repeals:    ใน full_text ของ doc นี้ เจอ "ให้ยกเลิก...ฉบับที่ N"
    # amends:     ใน full_text ของ doc นี้ เจอ "ให้แก้ไขเพิ่มเติม/ให้ยกเลิกความใน...ฉบับที่ N"
    db.execute('''
        CREATE TABLE doc_relations (
            doc_id      TEXT NOT NULL,
            target_num  INTEGER NOT NULL,
            relation    TEXT NOT NULL,
            PRIMARY KEY (doc_id, target_num, relation)
        )
    ''')
    db.execute('CREATE INDEX idx_dr_target ON doc_relations(target_num, relation)')

    files = sorted(
        fp
        for d in JSON_DIRS if os.path.isdir(d)
        for fp in glob.glob(os.path.join(d, '*.json'))
    )
    total = len(files)
    print(f"พบ {total} ไฟล์ JSON")

    batch_meta     = []
    batch_fts      = []
    batch_links    = []   # (doc_id, parent_law, sections, relationship)
    batch_relations = []  # (doc_id, target_num, relation)
    skipped        = 0
    type_counts: dict[str, int] = {}

    for i, fp in enumerate(files):
        try:
            with open(fp, encoding='utf-8') as f:
                d = json.load(f)

            if not d or not d.get('id'):
                skipped += 1
                continue

            # Normalize ฎีกา schema (type=supreme_court_judgment ไม่มี title/ref_number/content)
            if d.get('type') == 'supreme_court_judgment':
                d['type'] = 'court_judgment'
                if not d.get('title'):
                    d['title'] = d.get('subject', '')
                if not d.get('ref_number'):
                    d['ref_number'] = d.get('case_number', '')
                if not d.get('content'):
                    d['content'] = {
                        'ข้อเท็จจริง': d.get('facts', ''),
                        'คำวินิจฉัย':  d.get('ruling', ''),
                    }
                if not d.get('summary'):
                    d['summary'] = d.get('key_principle', '') or d.get('why_important', '')

            if not d.get('title') and d.get('ref_number'):
                d['title'] = d['ref_number']
            if not d.get('title'):
                skipped += 1
                continue

            doc_type   = d.get('type', 'ruling') or 'ruling'
            content    = d.get('content', {}) or {}
            title      = d.get('title', '') or ''
            ref_number = d.get('ref_number', '') or ''
            summary    = d.get('summary', '') or ''

            facts, ruling_txt = extract_facts_ruling(doc_type, content)

            # รวมเนื้อหาทั้งหมดเพื่อ FTS index
            keywords_txt  = ' '.join(d.get('keywords', []) or [])
            sections_txt  = ' '.join(d.get('related_sections', []) or [])
            why_issued    = d.get('why_issued', '') or ''
            key_principle = d.get('key_principle', '') or ''
            why_important = d.get('why_important', '') or ''
            chain = d.get('authorizing_law_chain', []) or []
            chain_txt = ' '.join(
                f"{c.get('law','')} {' '.join(c.get('sections',[]))}"
                for c in chain if isinstance(c, dict)
            )
            full_text_raw = d.get('full_text', '') or ''
            full_content = ' '.join(filter(None, [
                title, ref_number, summary,
                facts, ruling_txt, keywords_txt, sections_txt,
                why_issued, chain_txt, key_principle, why_important,
                full_text_raw,
            ]))

            title_tok   = tokenize_thai(title)
            content_tok = tokenize_thai(full_content)

            year_str = d.get('date', '')
            year = int(year_str[:4]) + 543 if year_str and len(year_str) >= 4 else 0
            if not year:
                if d.get('year_be'):
                    year = int(d['year_be'])
                else:
                    _m = re.search(r'ruling-(\d{4})-', d.get('id', ''))
                    if _m: year = int(_m.group(1))

            batch_meta.append((
                d['id'],
                ref_number,
                title,
                year,
                normalize_tax_type(d.get('tax_type') or []),
                d.get('date', ''),
                d.get('source_url', ''),
                summary[:600],
                facts[:800],
                ruling_txt[:10000] if doc_type == 'law_section' else ruling_txt[:800],
                doc_type,
                1 if d.get('repealed') else 0,
                (d.get('plain_summary') or '')[:400],
                (d.get('chapter') or '') or None,
                (d.get('part') or '') or None,
            ))
            batch_fts.append((d['id'], title_tok, content_tok))
            type_counts[doc_type] = type_counts.get(doc_type, 0) + 1

            # ── law_links: จาก authorizing_law_chain ────────────────────────
            for c in chain:
                if not isinstance(c, dict): continue
                plaw = (c.get('law') or '').strip()
                if not plaw: continue
                secs = ','.join(str(s) for s in (c.get('sections') or []) if s and str(s) != 'null')
                rel  = (c.get('relationship') or '').strip()
                batch_links.append((d['id'], plaw, secs, rel))

            # ── doc_relations: parse full_text ──────────────────────────────
            if doc_type not in ('ruling', 'court_judgment', 'supreme_court_judgment'):
                ft_text = (content.get('full_text') or '') if isinstance(content, dict) else ''
                if ft_text:
                    # (แก้ไขเพิ่มเติมโดย...ฉบับที่ N) → doc นี้ถูกแก้ไขโดย N
                    for m in _AMENDED_BY_RE.finditer(ft_text):
                        n = int(_th2ar(m.group(1)))
                        batch_relations.append((d['id'], n, 'amended_by'))
                    # ให้ยกเลิก...ฉบับที่ N → doc นี้ยกเลิก N (ทั้งฉบับ)
                    for m in _REPEALS_RE.finditer(ft_text):
                        n = int(_th2ar(m.group(1)))
                        batch_relations.append((d['id'], n, 'repeals'))
                    # ให้แก้ไข/ยกเลิกความใน...ฉบับที่ N → doc นี้แก้ไขบางส่วนของ N (N ยังบังคับใช้)
                    for m in _AMENDS_RE.finditer(ft_text):
                        n = int(_th2ar(m.group(1)))
                        batch_relations.append((d['id'], n, 'amends'))

        except Exception as e:
            print(f"  ข้ามไฟล์ {os.path.basename(fp)}: {e}")
            skipped += 1

        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            print(f"  {i+1}/{total}  ({elapsed:.1f}s)")

    # dedup batch_fts by doc_id (FTS5 ไม่ enforce uniqueness — INSERT OR IGNORE ไม่ทำงานกับ FTS)
    seen_fts_ids: set = set()
    dedup_fts = []
    for row in batch_fts:
        if row[0] not in seen_fts_ids:
            seen_fts_ids.add(row[0])
            dedup_fts.append(row)

    print(f"กำลัง INSERT {len(batch_meta)} records... (FTS: {len(dedup_fts)} หลัง dedup)")
    db.executemany('INSERT OR IGNORE INTO meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', batch_meta)
    db.executemany('INSERT INTO fts VALUES (?,?,?)', dedup_fts)

    # dedup batch_links (doc_id+parent_law unique)
    seen_links = set()
    dedup_links = []
    for row in batch_links:
        key = (row[0], row[1])
        if key not in seen_links:
            seen_links.add(key)
            dedup_links.append(row)
    db.executemany('INSERT OR IGNORE INTO law_links VALUES (?,?,?,?)', dedup_links)

    # dedup batch_relations
    seen_rels = set()
    dedup_rels = []
    for row in batch_relations:
        key = (row[0], row[1], row[2])
        if key not in seen_rels:
            seen_rels.add(key)
            dedup_rels.append(row)
    db.executemany('INSERT OR IGNORE INTO doc_relations VALUES (?,?,?)', dedup_rels)

    db.commit()

    # อัปเดต meta.repealed จาก doc_relations (repeal โดย doc อื่น)
    # หา doc ที่มีเลขฉบับตรงกับ target_num ใน doc_relations WHERE relation='repeals'
    _update_repealed_from_relations(db)

    # FTS dedup: ลบ row ซ้ำที่เกิดจาก concurrent build (เก็บ rowid น้อยสุดต่อ doc_id)
    fts_dup = db.execute('SELECT COUNT(*) FROM fts').fetchone()[0] - len(dedup_fts)
    if fts_dup > 0:
        db.execute('DELETE FROM fts WHERE rowid NOT IN (SELECT MIN(rowid) FROM fts GROUP BY doc_id)')
        db.commit()
        print(f"  FTS dedup: ลบ {fts_dup} rows ซ้ำออก")

    # Optimize FTS index
    db.execute("INSERT INTO fts(fts) VALUES('optimize')")
    db.commit()

    print(f"  law_links: {len(dedup_links)} rows")
    print(f"  doc_relations: {len(dedup_rels)} rows (amended_by/repeals/amends)")

    # ── ruling_related_laws: เชื่อม ข้อหารือ กับ กฎหมายลูก ผ่าน มาตราร่วม ──────
    try:
        import link_ruling_related
        link_ruling_related.populate(db)
    except Exception as _e:
        print(f'  [warning] link_ruling_related failed: {_e}')

    # Checkpoint WAL ก่อน close — รวม WAL เข้า main file เพื่อให้ tmp เป็น self-contained
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    db.close()
    # ลบ WAL files ของ tmp ที่อาจค้างอยู่
    for _ext in ('-shm', '-wal'):
        _f = tmp_path + _ext
        if os.path.exists(_f):
            os.remove(_f)

    # Atomic swap: สำรองเก่า แล้วแทนที่ด้วย tmp
    # ก่อน swap ต้องลบ WAL files ของ DB เก่าที่ running app มีอยู่ด้วย
    # เพราะถ้าปล่อยไว้ SQLite จะเอา WAL เก่ามา apply กับ DB ใหม่ → corruption
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, DB_PATH + '.bak')
    for _ext in ('-shm', '-wal'):
        _f = DB_PATH + _ext
        if os.path.exists(_f):
            os.remove(_f)
    os.replace(tmp_path, DB_PATH)
    print(f"สำรอง DB เก่าเป็น rulings.db.bak แล้ว (atomic swap)")

    elapsed = time.time() - t0
    print(f"\n✓ เสร็จแล้ว! {len(batch_meta)} records (ข้าม {skipped})")
    print(f"  เวลา: {elapsed:.1f}s")
    print(f"  ไฟล์ DB: {DB_PATH}")
    size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"  ขนาด: {size_mb:.1f} MB")
    print(f"\n  ประเภทเอกสาร:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        label = DOC_TYPE_LABELS.get(t, t)
        print(f"    {label:25s}: {c:,}")


def update_incremental() -> tuple[int, int]:
    """เพิ่มเฉพาะไฟล์ JSON ใหม่ที่ยังไม่มีใน DB — ไม่ต้อง rebuild ทั้งหมด
    คืน (added, skipped)
    """
    if not os.path.exists(DB_PATH):
        build()
        return 0, 0

    db = sqlite3.connect(DB_PATH)
    db.execute('PRAGMA journal_mode=WAL')

    existing_ids = {row[0] for row in db.execute('SELECT id FROM meta').fetchall()}

    files = sorted(
        fp
        for d in JSON_DIRS if os.path.isdir(d)
        for fp in glob.glob(os.path.join(d, '*.json'))
    )

    added          = 0
    skipped        = 0
    batch_meta     = []
    batch_fts      = []
    batch_links    = []
    batch_relations = []

    for fp in files:
        try:
            with open(fp, encoding='utf-8') as f:
                d = json.load(f)
            if not d or not d.get('id'):
                skipped += 1
                continue
            if d['id'] in existing_ids:
                skipped += 1
                continue
            if not d.get('title') and d.get('ref_number'):
                d['title'] = d['ref_number']
            if not d.get('title'):
                skipped += 1
                continue

            doc_type   = d.get('type', 'ruling') or 'ruling'
            content    = d.get('content', {}) or {}
            title      = d.get('title', '') or ''
            ref_number = d.get('ref_number', '') or ''
            summary    = d.get('summary', '') or ''

            facts, ruling_txt = extract_facts_ruling(doc_type, content)

            keywords_txt = ' '.join(d.get('keywords', []) or [])
            sections_txt = ' '.join(d.get('related_sections', []) or [])
            why_issued   = d.get('why_issued', '') or ''
            chain = d.get('authorizing_law_chain', []) or []
            chain_txt = ' '.join(
                f"{c.get('law','')} {' '.join(c.get('sections',[]))}"
                for c in chain if isinstance(c, dict)
            )
            key_principle = d.get('key_principle', '') or ''
            why_important = d.get('why_important', '') or ''
            full_text_raw = d.get('full_text', '') or ''
            full_content = ' '.join(filter(None, [
                title, ref_number, summary,
                facts, ruling_txt, keywords_txt, sections_txt,
                why_issued, chain_txt, key_principle, why_important,
                full_text_raw,
            ]))

            title_tok   = tokenize_thai(title)
            content_tok = tokenize_thai(full_content)

            year_str = d.get('date', '')
            year = int(year_str[:4]) + 543 if year_str and len(year_str) >= 4 else 0
            if not year:
                if d.get('year_be'):
                    year = int(d['year_be'])
                else:
                    _m = re.search(r'ruling-(\d{4})-', d.get('id', ''))
                    if _m: year = int(_m.group(1))

            batch_meta.append((
                d['id'], ref_number, title, year,
                normalize_tax_type(d.get('tax_type') or []),
                d.get('date', ''),
                d.get('source_url', ''),
                summary[:600],
                facts[:800],
                ruling_txt[:10000] if doc_type == 'law_section' else ruling_txt[:800],
                doc_type,
                1 if d.get('repealed') else 0,
                (d.get('plain_summary') or '')[:400],
                (d.get('chapter') or '') or None,
                (d.get('part') or '') or None,
            ))
            batch_fts.append((d['id'], title_tok, content_tok))
            added += 1

            # law_links: จาก authorizing_law_chain
            for c in chain:
                if not isinstance(c, dict): continue
                plaw = (c.get('law') or '').strip()
                if not plaw: continue
                secs = ','.join(str(s) for s in (c.get('sections') or []) if s and str(s) != 'null')
                rel  = (c.get('relationship') or '').strip()
                batch_links.append((d['id'], plaw, secs, rel))

            # doc_relations: parse full_text สำหรับ non-ruling docs
            if doc_type not in ('ruling', 'court_judgment', 'supreme_court_judgment'):
                ft_text = (content.get('full_text') or '') if isinstance(content, dict) else ''
                if ft_text:
                    for m in _AMENDED_BY_RE.finditer(ft_text):
                        batch_relations.append((d['id'], int(_th2ar(m.group(1))), 'amended_by'))
                    for m in _REPEALS_RE.finditer(ft_text):
                        batch_relations.append((d['id'], int(_th2ar(m.group(1))), 'repeals'))
                    for m in _AMENDS_RE.finditer(ft_text):
                        batch_relations.append((d['id'], int(_th2ar(m.group(1))), 'amends'))

        except Exception as e:
            print(f"  [incremental] ข้ามไฟล์ {os.path.basename(fp)}: {e}")
            skipped += 1

    if batch_meta:
        db.executemany('INSERT OR IGNORE INTO meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', batch_meta)
        db.executemany('INSERT OR IGNORE INTO fts VALUES (?,?,?)', batch_fts)

        # law_links dedup
        seen_links = set()
        dedup_links = []
        for row in batch_links:
            key = (row[0], row[1])
            if key not in seen_links:
                seen_links.add(key)
                dedup_links.append(row)
        if dedup_links:
            db.executemany('INSERT OR IGNORE INTO law_links VALUES (?,?,?,?)', dedup_links)

        # doc_relations dedup
        seen_rels = set()
        dedup_rels = []
        for row in batch_relations:
            key = (row[0], row[1], row[2])
            if key not in seen_rels:
                seen_rels.add(key)
                dedup_rels.append(row)
        if dedup_rels:
            db.executemany('INSERT OR IGNORE INTO doc_relations VALUES (?,?,?,?)', dedup_rels)

        db.commit()
        db.execute("INSERT INTO fts(fts) VALUES('optimize')")
        db.commit()

    db.close()
    return added, skipped


if __name__ == '__main__':
    build()
