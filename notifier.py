"""Core logic: parse Buildium rent roll + ePay activity PDFs and build owner texts."""
import re
from collections import OrderedDict
from datetime import date, timedelta

import pdfplumber

FUND_BUSINESS_DAYS = 4
MONEY = r"-?[\d,]+\.\d{2}"
DATE = r"\d{1,2}/\d{1,2}/\d{4}"


# ---------- dates ----------
def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def bank_holidays(year):
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4),
             date(year, 11, 11), date(year, 12, 25)]
    hol = {d + timedelta(days=1) if d.weekday() == 6 else d for d in fixed}  # Sunday -> Monday
    hol |= {_nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3),
            _last_weekday(year, 5, 0), _nth_weekday(year, 9, 0, 1),
            _nth_weekday(year, 10, 0, 2), _nth_weekday(year, 11, 3, 4)}
    return hol


def add_business_days(start, n):
    d, count = start, 0
    while count < n:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in bank_holidays(d.year) | bank_holidays(d.year + 1):
            count += 1
    return d


def parse_date(s):
    m, d, y = (int(x) for x in s.split("/"))
    return date(y, m, d)


def fmt(d):
    return d.strftime("%m/%d")


def ordinal(n):
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def money(x):
    return f"${x:,.2f}"


def num(s):
    return float(s.replace(",", ""))


def clean_name(s):
    return re.sub(r"\s+_$", "", s.strip())


# ---------- pdf text ----------
def _doubled(tok):
    return len(tok) >= 2 and len(tok) % 2 == 0 and tok[0::2] == tok[1::2]


def _undouble_line(line):
    """Bold headings extract with every character doubled ("TToottaall"); undo that."""
    toks = line.split()
    first_alpha = next((t for t in toks if any(c.isalpha() for c in t)), None)
    if first_alpha and _doubled(first_alpha):
        return " ".join(t[0::2] if _doubled(t) else t for t in toks)
    return line


def pdf_lines(path):
    lines = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            lines += (page.extract_text() or "").splitlines()
    return [_undouble_line(l.strip()) for l in lines if l.strip()]


# ---------- ePay ----------
EPAY_RE = re.compile(
    rf"^({DATE})\s+(?:{DATE}\s+)?(.+?) - (\S+) (.+) by (.+?) ({MONEY}) ({MONEY})$")


def parse_epay(path):
    """Returns list of dicts: property, unit, payer, date, amount."""
    out = []
    for line in pdf_lines(path):
        m = EPAY_RE.match(line)
        if m:
            out.append(dict(date=parse_date(m.group(1)), property=m.group(2).strip(),
                            unit=m.group(3), payer=clean_name(m.group(5)),
                            amount=num(m.group(6))))
    return out


# ---------- rent roll ----------
def parse_rent_roll(path, property_names):
    lines = pdf_lines(path)
    as_of = None
    for l in lines:
        m = re.search(rf"As of ({DATE})", l)
        if m:
            as_of = parse_date(m.group(1))
            break

    units = {}  # property -> list of dict(unit, names, rent, balance)
    for prop in property_names:
        units[prop] = []
        try:
            start = lines.index(prop) + 1
            end = next(i for i in range(start, len(lines)) if lines[i].startswith(f"Total for {prop}"))
        except (ValueError, StopIteration):
            continue
        for l in lines[start:end]:
            m = re.match(r"^(\d+[A-Za-z]?)\s+(.*)$", l)
            dm = re.search(rf"({DATE})\s+(at-will|{DATE})", l)
            if m and dm:  # a tenant row
                head = l[:dm.start()].split(None, 1)
                monies = [num(x) for x in re.findall(MONEY, l[dm.end():])]
                if len(monies) >= 7:
                    units[prop].append(dict(unit=head[0], rent=monies[0], balance=monies[-1],
                                            name=head[1].strip() if len(head) > 1 else ""))
            elif units[prop] and not re.search(MONEY, l):  # wrapped part of the tenant name
                units[prop][-1]["name"] += " " + l
        for u in units[prop]:
            u["names"] = [clean_name(n) for n in u.pop("name").split(",")]
    return as_of, units


# ---------- build ----------
def build(rent_roll_pdf, epay_pdf, owners):
    prop_names = [p["name"] for o in owners for p in o["properties"]]
    as_of, units = parse_rent_roll(rent_roll_pdf, prop_names)
    epay = parse_epay(epay_pdf)
    warnings = []
    if not as_of:
        raise ValueError("Could not read the 'As of' date from the rent roll.")
    month = as_of.strftime("%b")

    results = []
    for owner in owners:
        body = [f"Hi {owner['greeting']},", "",
                f"Here are the current rents paid as of {as_of.strftime('%B')} {ordinal(as_of.day)}:", ""]
        unpaid = 0
        blocks = []
        for prop in owner["properties"]:
            name = prop["name"]
            if not units.get(name):
                warnings.append(f"{name}: not found in the rent roll.")
                continue
            lines, received = [], 0.0
            for u in units[name]:
                pays = sorted((e for e in epay if e["property"] == name and e["unit"] == u["unit"]),
                              key=lambda e: e["date"], reverse=True)
                picked, total = [], 0.0
                for e in pays:  # newest first until the unit's rent is covered
                    if total >= u["rent"] - 0.005:
                        break
                    picked.append(e)
                    total += e["amount"]
                if len(pays) > len(picked):
                    warnings.append(f"{name} unit {u['unit']}: ignored {len(pays) - len(picked)} older ePay(s) beyond this month's rent.")
                if not picked:
                    who = ", ".join(u["names"])
                    if u["balance"] > 0.005:
                        lines.append(f"{who}: Not paid yet")
                        unpaid += 1
                    else:
                        lines.append(f"{who}: paid, no ePay found")
                        warnings.append(f"{name} unit {u['unit']} ({who}): balance is $0 but no ePay found (check/other payment?). Amount not counted.")
                    continue
                received += total
                if total < u["rent"] - 0.005:
                    warnings.append(f"{name} unit {u['unit']}: ePays total {money(total)} vs rent {money(u['rent'])}.")
                # one line per payer within the unit
                by_payer = OrderedDict()
                order = [n.lower() for n in u["names"]]

                def rank(e):  # keep the rent roll's tenant order, then date
                    p = e["payer"].lower()
                    return (next((i for i, n in enumerate(order) if n == p), len(order)), e["date"])

                for e in sorted(picked, key=rank):
                    by_payer.setdefault(e["payer"], []).append(e)
                for payer, es in by_payer.items():
                    parts, seen = [], set()
                    for e in es:
                        f = add_business_days(e["date"], FUND_BUSINESS_DAYS)
                        key = (e["date"], f)
                        if key not in seen:
                            seen.add(key)
                            parts.append(f"Epay {fmt(e['date'])} funded {fmt(f)}")
                    line = f"{payer}: " + " and ".join(parts)
                    p_total = sum(e["amount"] for e in es)
                    if len(by_payer) == 1 and p_total > u["rent"] + 0.005:
                        extra = p_total - u["rent"]
                        line += f" ({month} rent {money(u['rent'])} plus {money(extra)} additional = {money(p_total)})"
                        warnings.append(f"{name} {payer}: paid {money(extra)} more than rent. I added a note; edit the wording if needed.")
                    lines.append(line)
            blocks += [f"{prop['label']}: {money(received)} received"] + lines + [""]
        words = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
        full_month = as_of.strftime("%B")
        if unpaid == 0:
            body += [f"All {full_month} rents received", ""]
        else:
            count = words[unpaid] if unpaid < len(words) else str(unpaid)
            body += [f"{count} {full_month} rent still outstanding", ""]
        body += blocks
        results.append(dict(owner=owner["greeting"], filename=owner["filename"],
                            text="\n".join(body).rstrip() + "\n"))
    return as_of, results, warnings
