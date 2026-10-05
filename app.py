import json
import os
import tempfile
from datetime import datetime

from flask import Flask, render_template_string, request

from notifier import FUND_BUSINESS_DAYS, build

app = Flask(__name__)
OWNERS = json.load(open(os.path.join(os.path.dirname(__file__), "owners.json")))["owners"]

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
SAVE_FILE = os.path.join(DATA_DIR, "latest.json")


def load_saved():
    try:
        with open(SAVE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_latest(as_of, results, warnings):
    """Keep the latest set until the next rent roll + ePay upload replaces it."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SAVE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dict(saved_at=datetime.now().strftime("%m/%d/%Y %I:%M %p"),
                       as_of=as_of.strftime("%m/%d/%Y"), results=results, warnings=warnings), f)
    os.replace(tmp, SAVE_FILE)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ford Managed Property Rent Received Notifications</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:720px;margin:24px auto;padding:0 16px;color:#1d1d1f}
 h1{font-size:22px} label{display:block;margin:14px 0 4px;font-weight:600}
 button{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:15px;cursor:pointer}
 button.alt{background:#e5e7eb;color:#111} textarea{width:100%;height:300px;font:14px/1.4 ui-monospace,Menlo,monospace;padding:10px;border:1px solid #ccc;border-radius:8px;box-sizing:border-box}
 .warn{background:#fff7e6;border:1px solid #f5c26b;border-radius:8px;padding:10px 14px;margin:16px 0}
 .card{border:1px solid #e5e7eb;border-radius:12px;padding:14px;margin:18px 0} .row{display:flex;gap:8px;margin-top:8px}
 .muted{color:#666;font-size:13px}
</style></head><body>
<h1>Ford Managed Property Rent Received Notifications</h1>
<p class="muted">Upload the rent roll and ePay activity PDFs. Funded date = {{ n }}th business day after the ePay date. The PDFs themselves are not stored; the texts stay saved until you upload new files.</p>
<form method="post" enctype="multipart/form-data">
 <label>Rent roll (PDF)</label><input type="file" name="rent_roll" accept=".pdf" required>
 <label>ePay activity (PDF)</label><input type="file" name="epay" accept=".pdf" required>
 <p><button type="submit">Create texts</button></p>
</form>
{% if saved_at %}<p class="muted">Showing saved texts from rent roll as of {{ as_of }} (created {{ saved_at }}). Uploading new files replaces them.</p>{% endif %}
{% if error %}<div class="warn"><b>Problem:</b> {{ error }}</div>{% endif %}
{% if results %}
 {% if warnings %}<div class="warn"><b>Please check before sending:</b><ul>{% for w in warnings %}<li>{{ w }}</li>{% endfor %}</ul></div>{% endif %}
 {% for r in results %}
 <div class="card"><b>{{ r.owner }}</b>
  <textarea id="t{{ loop.index }}">{{ r.text }}</textarea>
  <div class="row"><button onclick="copyIt('t{{ loop.index }}',this)">Copy</button>
  <button class="alt" onclick="dl('t{{ loop.index }}','{{ r.filename }}')">Download .txt</button></div></div>
 {% endfor %}
{% endif %}
<script>
function copyIt(id,btn){const t=document.getElementById(id);t.select();navigator.clipboard.writeText(t.value);btn.textContent='Copied';setTimeout(()=>btn.textContent='Copy',1500)}
function dl(id,name){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([document.getElementById(id).value],{type:'text/plain'}));a.download=name;a.click()}
</script></body></html>"""


@app.route("/", methods=["GET", "POST"])
def index():
    ctx = dict(n=FUND_BUSINESS_DAYS, results=None, warnings=[], error=None, saved_at=None, as_of=None)
    saved = load_saved()
    if saved:
        ctx.update(results=saved["results"], warnings=saved["warnings"],
                   saved_at=saved["saved_at"], as_of=saved["as_of"])
    if request.method == "POST":
        with tempfile.TemporaryDirectory() as tmp:
            paths = {}
            for key in ("rent_roll", "epay"):
                paths[key] = os.path.join(tmp, key + ".pdf")
                request.files[key].save(paths[key])
            try:
                as_of, results, warnings = build(paths["rent_roll"], paths["epay"], OWNERS)
                save_latest(as_of, results, warnings)
                saved = load_saved()
                ctx.update(results=results, warnings=warnings,
                           saved_at=saved["saved_at"], as_of=saved["as_of"])
            except Exception as e:  # show a readable message instead of a stack trace
                ctx["error"] = str(e)
    return render_template_string(PAGE, **ctx)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
