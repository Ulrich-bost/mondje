import json
import re

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
AMOUNT_RE = re.compile(r"^[0-9]+$")
NEGATIVE_RE = re.compile(r"^-[0-9]+$")
MAX_AMOUNT = 9_000_000_000_000_000
MAX_LINE = 65536
BOM = b"\xef\xbb\xbf"
REQUIRED = ("id", "type", "amount", "description")


class Rejected(Exception):
    def __init__(self, reason, col=0):
        super().__init__(reason)
        self.reason = reason
        self.col = col


def check_id(raw, col):
    raw = raw.strip()
    if not UUID_RE.match(raw):
        raise Rejected(f"UUID mal formé : {raw[:60]!r}", col)
    return raw.lower()


def check_type(raw, col):
    if raw not in ("CREDIT", "DEBIT"):
        raise Rejected(f"type inconnu : {str(raw)[:40]!r} (CREDIT ou DEBIT attendu)", col)
    return raw


def check_amount_text(raw, col):
    raw = raw.strip()
    if NEGATIVE_RE.match(raw):
        raise Rejected(f"montant négatif : {raw}", col)
    if not AMOUNT_RE.match(raw):
        raise Rejected(f"montant invalide : {raw[:40]!r} (entier sans décimales attendu)", col)
    return check_amount_int(int(raw), col)


def check_amount_int(value, col):
    if value < 0:
        raise Rejected(f"montant négatif : {value}", col)
    if value == 0:
        raise Rejected("montant nul : le montant doit être strictement positif", col)
    if value > MAX_AMOUNT:
        raise Rejected("montant trop grand", col)
    return value


def check_description(raw, col):
    raw = raw.strip()
    if len(raw) > 255:
        raise Rejected(f"description trop longue : {len(raw)} caractères (255 maximum)", col)
    if "\x00" in raw:
        raise Rejected("description invalide : caractère nul interdit", col)
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise Rejected("description invalide : caractère Unicode non valide", col)
    return raw


def parse_txt(line):
    parts = line.split(";")
    offsets = []
    total = 0
    for part in parts:
        offsets.append(total)
        total += len(part) + 1
    if len(parts) < 4:
        raise Rejected(
            f"ligne incomplète : 4 champs attendus (id;type;montant;description), {len(parts)} trouvé(s)",
            len(line),
        )
    if len(parts) > 4:
        raise Rejected("trop de champs : la description ne peut pas contenir de point-virgule", offsets[4] - 1)
    return (
        check_id(parts[0], offsets[0]),
        check_type(parts[1], offsets[1]),
        check_amount_text(parts[2], offsets[2]),
        check_description(parts[3], offsets[3]),
    )


def parse_json(line):
    try:
        obj = json.loads(line)
    except ValueError as exc:
        raise Rejected(f"JSON invalide : {exc}", getattr(exc, "pos", 0) or 0)
    if not isinstance(obj, dict):
        raise Rejected("la ligne doit être un objet JSON complet")
    missing = [key for key in REQUIRED if key not in obj]
    if missing:
        raise Rejected("champs manquants : " + ", ".join(missing))
    if not isinstance(obj["id"], str):
        raise Rejected("UUID mal formé : une chaîne est attendue")
    if not isinstance(obj["type"], str):
        raise Rejected("type inconnu : une chaîne est attendue")
    amount = obj["amount"]
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise Rejected(f"montant invalide : {str(amount)[:40]!r} (entier sans décimales attendu)")
    if not isinstance(obj["description"], str):
        raise Rejected("description invalide : une chaîne est attendue")
    return (
        check_id(obj["id"], 0),
        check_type(obj["type"], 0),
        check_amount_int(amount, 0),
        check_description(obj["description"], 0),
    )


def safe(text):
    return text.replace("\x00", "\ufffd")[:2000]


def read_batch(path, offset, line_no, fmt, batch_lines):
    parse = parse_txt if fmt == "txt" else parse_json
    rows = []
    errors = []
    count = 0
    with open(path, "rb") as handle:
        handle.seek(offset)
        pos = offset
        while count < batch_lines:
            start = pos
            raw = handle.readline(MAX_LINE)
            if not raw:
                return rows, errors, pos, line_no, True
            pos += len(raw)
            too_long = False
            if len(raw) == MAX_LINE and not raw.endswith(b"\n"):
                too_long = True
                while True:
                    extra = handle.readline(MAX_LINE)
                    if not extra:
                        break
                    pos += len(extra)
                    if extra.endswith(b"\n"):
                        break
            line_no += 1
            count += 1
            if start == 0 and raw.startswith(BOM):
                raw = raw[3:]
            body = raw.rstrip(b"\r\n")
            if not body.strip():
                continue
            if too_long:
                errors.append((line_no, start, pos, MAX_LINE, safe(body.decode("utf-8", "replace")),
                               f"ligne trop longue (plus de {MAX_LINE} octets)"))
                continue
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                errors.append((line_no, start, pos, exc.start, safe(body.decode("utf-8", "replace")),
                               "encodage invalide : octets non UTF-8 (accent mal encodé)"))
                continue
            try:
                row = parse(text)
            except Rejected as rejected:
                errors.append((line_no, start, pos, rejected.col, safe(text), rejected.reason))
                continue
            rows.append(row + (line_no,))
    return rows, errors, pos, line_no, False


def looks_like_json_array(head):
    if head.startswith(BOM):
        head = head[3:]
    return head.lstrip(b" \t\r\n").startswith(b"[")
