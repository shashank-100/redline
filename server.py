"""Redline: reviews an MSA draft against the playbook in ./playbook/.

Stdlib only, so ./start needs no install step.
Pipeline: segment draft -> Stage A (LLM locates relevant segments per rule, windowed)
-> Stage B (LLM decides each rule on its effective terms) -> validated JSON/markdown.
"""
import glob
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "cache")
BASE_URL = os.environ.get("LITMUS_AI_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("LITMUS_AI_API_KEY", "")
PREFERRED_MODELS = [
    "anthropic/claude-sonnet-5.5",
    "anthropic/claude-sonnet-5",
    "openai/gpt-4.1",
    "gpt-4.1",
    "openai/gpt-4o",
    "gpt-4o",
]
MODEL = os.environ.get("LITMUS_AI_MODEL", "")
SEED = 7
STATUSES = ["accepted", "fallback", "not accepted", "not applicable"]
REQUEST_BUDGET = 270
WINDOW_CHARS = 60000
MAX_SEG_CHARS = 6000
RULE_CONTEXT_CHARS = 90000

_mem_cache = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- LLM client

def _http_json(method, url, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {API_KEY}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def pick_model():
    global MODEL
    if MODEL:
        return MODEL
    try:
        ids = {m.get("id") for m in _http_json("GET", BASE_URL + "/models", timeout=30).get("data", [])}
    except Exception as e:  # noqa: BLE001
        print(f"model listing failed: {e}", file=sys.stderr)
        ids = set()
    for m in PREFERRED_MODELS:
        if m in ids:
            MODEL = m
            break
    else:
        MODEL = sorted(ids)[0] if ids else PREFERRED_MODELS[0]
    print(f"using model {MODEL}", file=sys.stderr)
    return MODEL


def _extract_json(text):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


def chat_json(system, user, deadline, max_tokens=4000):
    body = {
        "model": pick_model(),
        "temperature": 0,
        "top_p": 1,
        "seed": SEED,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    last = None
    for attempt in range(3):
        remaining = deadline - time.time()
        if remaining < 5:
            break
        try:
            out = _http_json("POST", BASE_URL + "/chat/completions", body, timeout=min(150, remaining))
            content = out["choices"][0]["message"]["content"] or ""
            return _extract_json(content)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:300]!r}"
            if e.code == 400 and "response_format" in body:
                body.pop("response_format")
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(1 + attempt)
    raise RuntimeError(f"LLM call failed: {last}")


# ---------------------------------------------------------------- playbook

def load_playbook():
    files = sorted(glob.glob(os.path.join(ROOT, "playbook", "*.json")))
    if not files:
        raise RuntimeError("no playbook JSON found in ./playbook/")
    with open(files[0]) as f:
        pb = json.load(f)
    rules = pb.get("rules") if isinstance(pb, dict) else pb
    meta = {k: v for k, v in pb.items() if k != "rules"} if isinstance(pb, dict) else {}
    return meta, rules, os.path.basename(files[0])


def rule_id(rule, idx):
    for k in ("rule", "id", "number", "rule_id"):
        if k in rule:
            return rule[k]
    return idx + 1


def rule_heading(rule):
    for k in ("heading", "title", "name", "topic"):
        if k in rule:
            return rule[k]
    return ""


def rule_fallbacks(rule):
    fb = rule.get("fallbacks") or rule.get("fallback_positions") or rule.get("alternates") or []
    return fb if isinstance(fb, list) else [fb]


# ---------------------------------------------------------------- segmentation

HEADING_RE = re.compile(
    r"^\s*(?:"
    r"(?:ARTICLE|Article|SECTION|Section|EXHIBIT|Exhibit|SCHEDULE|Schedule|ANNEX|Annex|APPENDIX|Appendix|"
    r"ATTACHMENT|Attachment|ORDER FORM|Order Form|AMENDMENT|Amendment|ADDENDUM|Addendum|"
    r"STATEMENT OF WORK|Statement of Work|RIDER|Rider)\b"
    r"|\d+(?:\.\d+)*[.)]\s+\S"
    r"|[A-Z]{1,3}-\d+(?:\.\d+)*[.)]?\s"
    r")"
)
CONTAINER_RE = re.compile(
    r"^\s*(EXHIBIT|SCHEDULE|ANNEX|APPENDIX|ATTACHMENT|ORDER FORM|AMENDMENT|ADDENDUM|STATEMENT OF WORK|RIDER)\b",
    re.I,
)


def segment(text):
    lines = text.splitlines()
    raw = []  # (start_line_idx, end_line_idx_exclusive)
    start = 0
    for i, line in enumerate(lines):
        if i > start and HEADING_RE.match(line):
            raw.append((start, i))
            start = i
    raw.append((start, len(lines)))

    segs = []
    container = ""
    for s, e in raw:
        chunk_lines = lines[s:e]
        first = next((l.strip() for l in chunk_lines if l.strip()), "")
        if not first:
            continue
        if CONTAINER_RE.match(first):
            # container title may span two heading lines (EXHIBIT A / ORDER FORM No. 1)
            if segs and segs[-1]["is_container"] and segs[-1]["end"] == s and len(segs[-1]["text"]) < 80:
                container = segs[-1]["heading"] + " > " + first
            else:
                container = first
                sub = [l.strip() for l in chunk_lines if l.strip()][1:2]
                if sub and len(sub[0]) <= 120 and not HEADING_RE.match(sub[0]):
                    container = f"{first} — {sub[0]}"
            is_container = True
        else:
            is_container = False
            # a top-level numbered heading after exhibits starts new body-like scope only if inside no container
        # split oversized segments by line groups
        buf, buf_start = [], s
        for j, l in enumerate(chunk_lines):
            buf.append(l)
            if sum(len(x) + 1 for x in buf) >= MAX_SEG_CHARS:
                segs.append(_mkseg(len(segs), buf, buf_start, first, container, is_container))
                buf, buf_start = [], s + j + 1
        if any(x.strip() for x in buf):
            segs.append(_mkseg(len(segs), buf, buf_start, first, container, is_container))
    return segs


def _mkseg(n, buf, start, heading, container, is_container):
    return {
        "id": f"S{n + 1}",
        "start": start,
        "end": start + len(buf),
        "heading": heading[:120],
        "container": container,
        "is_container": is_container,
        "text": "\n".join(buf).strip(),
    }


def seg_label(seg):
    where = f"{seg['container']} > " if seg["container"] and not seg["is_container"] else ""
    return f"{where}{seg['heading']}"


def render_seg(seg):
    return f"[{seg['id']} | lines {seg['start'] + 1}-{seg['end']} | {seg_label(seg)[:160]}]\n{seg['text']}"


# ---------------------------------------------------------------- stage A

STAGE_A_SYSTEM = (
    "You are a contract analyst locating clauses. You are given a list of playbook rules and a window of "
    "segments from a contract draft (each tagged with an id). For each rule, list the ids of segments in THIS "
    "window that state, restate, modify, override, extend, condition, or carve out the subject matter of that "
    "rule. Include body clauses, order-form terms, exhibit terms, amendments, side letters, and 'notwithstanding' "
    "or precedence language that changes that subject. Include segments that create the kind of term a rule "
    "forbids or says should be absent. Do not include segments merely because they share a word; do not include "
    "unrelated boilerplate. Respond with JSON only: {\"matches\": {\"<rule id>\": [\"S1\", ...], ...}}. "
    "Omit rules with no matches."
)


def rules_brief(rules):
    out = []
    for i, r in enumerate(rules):
        out.append({
            "rule": rule_id(r, i),
            "heading": rule_heading(r),
            "starting_position": r.get("starting_position", ""),
            "fallbacks": rule_fallbacks(r),
            "not_acceptable": r.get("not_acceptable", ""),
        })
    return out


def windows(segs):
    wins, cur, size = [], [], 0
    for s in segs:
        r = render_seg(s)
        if cur and size + len(r) > WINDOW_CHARS:
            wins.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += len(r) + 2
    if cur:
        wins.append(cur)
    return wins


def stage_a(rules, segs, deadline):
    brief = json.dumps(rules_brief(rules), indent=1)
    valid_ids = {s["id"] for s in segs}
    rule_keys = [str(rule_id(r, i)) for i, r in enumerate(rules)]
    hits = {k: set() for k in rule_keys}

    def run(win):
        user = "RULES:\n" + brief + "\n\nDRAFT SEGMENTS:\n" + "\n\n".join(render_seg(s) for s in win)
        try:
            res = chat_json(STAGE_A_SYSTEM, user, deadline, max_tokens=2000)
        except Exception as e:  # noqa: BLE001
            print(f"stage A window failed: {e}", file=sys.stderr)
            return None
        return res.get("matches", res) if isinstance(res, dict) else None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(run, windows(segs)))
    failed = any(r is None for r in results)
    for res in results:
        if not isinstance(res, dict):
            continue
        for k, ids in res.items():
            k = str(k).strip()
            if k in hits and isinstance(ids, list):
                hits[k].update(i for i in ids if isinstance(i, str) and i in valid_ids)
    return hits, failed


STOP = set("""a an the of and or to in on for with by from at as is are be that this any its their our we
our not no all each per such than then within before after under upon may will must which who whom one
party parties agreement section rule position language days day months month year years written notice""".split())


def keyword_hits(rules, segs):
    """Cheap backstop so a missed Stage A window does not silently drop a clause."""
    out = {}
    for i, r in enumerate(rules):
        words = re.findall(r"[a-z][a-z\-]{3,}", (rule_heading(r) + " " + rule_heading(r)).lower())
        words = [w for w in words if w not in STOP]
        ids = set()
        for s in segs:
            head = s["heading"].lower()
            if words and any(w in head for w in words):
                ids.add(s["id"])
        out[str(rule_id(r, i))] = ids
    return out


# ---------------------------------------------------------------- stage B

STAGE_B_SYSTEM = """You are a senior contracts lawyer reviewing an inbound draft against ONE rule of your company's playbook.

Decide the rule's status on the EFFECTIVE terms of the draft: read the body clause together with every order form, exhibit, schedule, amendment, and precedence or "notwithstanding" clause provided. A later or controlling document that changes the term changes the effective term. If any controlling document pushes the term outside the approved positions, the effective term is outside them.
Only operative documents count. Material the draft itself marks as superseded, terminated, expired, of no force or effect, a prior agreement, a draft, or attached for reference only does NOT form part of the effective terms; ignore it for the status (you may mention it). Likewise a provision limited by its own terms to a narrow scope (e.g. one exhibit's subject) still counts for that scope.

Statuses (exactly one):
- "accepted": the effective terms conform to the rule's starting position (substance matters, not wording; equivalent phrasing is fine; terms more favorable to our company on every axis the rule addresses also conform).
- "fallback": the effective terms do not meet the starting position but conform to one of the approved fallbacks. Give its 1-based index and text.
- "not accepted": the effective terms fall outside the starting position and every fallback, or hit the not-acceptable language. Propose concrete changes (replacement language, and deletion/amendment of any overriding order-form/exhibit/amendment term) such that, once made, the rule would be "accepted" against the starting position.
- "not applicable": nothing in the draft addresses the rule's subject at all. Exception: if the rule's starting position is that the subject should be absent / our paper is silent, then absence means "accepted", and presence of the forbidden term means evaluate normally.

Clauses about other subjects are irrelevant to this rule; do not let them drive the status. Fallbacks that consist of dropping the clause apply only if the clause is actually absent.

Citations: cite every segment you relied on by its id, with a short verbatim quote copied exactly from that segment (max ~40 words). For "not applicable" citations may be empty.

Respond with JSON only:
{"status": "...", "fallback_index": null or integer, "fallback_text": null or string,
 "effective_term": "one or two sentences stating the term as it stands after all documents",
 "reasoning": "concise analysis comparing the effective term to the starting position, each fallback in order, and the not-acceptable language",
 "citations": [{"segment": "S12", "quote": "..."}],
 "proposed_changes": null or "specific redline language (required when not accepted)"}"""

PRECEDENCE_RE = re.compile(r"precedence|controls?\b|prevail|supersed|notwithstanding|amend", re.I)


def stage_b(meta, rule, idx, cand, prec, deadline):
    ordered = sorted(cand, key=lambda s: s["start"])
    ctx, size = [], 0
    for s in ordered:
        r = render_seg(s)
        if size + len(r) > RULE_CONTEXT_CHARS:
            break
        ctx.append(r)
        size += len(r)
    cand_ids = {s["id"] for s in ordered}
    prec_ctx, psize = [], 0
    for s in prec:
        if s["id"] in cand_ids:
            continue
        r = render_seg(s)
        if psize + len(r) > 15000:
            break
        prec_ctx.append(r)
        psize += len(r)

    user = (
        "PLAYBOOK CONTEXT:\n" + json.dumps({k: v for k, v in meta.items() if k in ("name", "preamble", "scope")}, indent=1)
        + "\n\nRULE UNDER REVIEW:\n" + json.dumps(rule, indent=1)
        + "\n\nRELEVANT DRAFT SEGMENTS (located across the whole draft, in document order):\n"
        + ("\n\n".join(ctx) if ctx else "(none found anywhere in the draft)")
        + "\n\nPRECEDENCE / AMENDMENT CLAUSES ELSEWHERE IN THE DRAFT:\n"
        + ("\n\n".join(prec_ctx) if prec_ctx else "(none)")
    )
    return chat_json(STAGE_B_SYSTEM, user, deadline, max_tokens=3000)


def normalize_status(s):
    s = (s or "").strip().lower().replace("_", " ").replace("-", " ")
    if s in STATUSES:
        return s
    if "not" in s and "appl" in s:
        return "not applicable"
    if "not" in s and "accept" in s or "reject" in s:
        return "not accepted"
    if "fallback" in s:
        return "fallback"
    if "accept" in s:
        return "accepted"
    return "not accepted"


def norm_ws(t):
    return re.sub(r"\s+", " ", t or "").strip().lower()


def finalize(rule, idx, res, seg_by_id, text_norm):
    fbs = rule_fallbacks(rule)
    status = normalize_status(res.get("status"))
    fb_index = res.get("fallback_index")
    fallback = None
    if status == "fallback":
        try:
            fb_index = int(fb_index)
        except (TypeError, ValueError):
            fb_index = None
        if fb_index and 1 <= fb_index <= len(fbs):
            fallback = {"index": fb_index, "position": fbs[fb_index - 1]}
        elif fbs:
            fallback = {"index": None, "position": res.get("fallback_text") or fbs[0]}
        else:
            status = "not accepted"
    cites = []
    for c in res.get("citations") or []:
        if not isinstance(c, dict):
            continue
        seg = seg_by_id.get(str(c.get("segment", "")).strip())
        quote = (c.get("quote") or "").strip()
        if not seg:
            continue
        verified = norm_ws(quote) in norm_ws(seg["text"]) if quote else False
        cites.append({
            "section": seg_label(seg),
            "lines": f"{seg['start'] + 1}-{seg['end']}",
            "quote": quote if verified else seg["text"][:300],
            "quote_verified": verified,
        })
    proposed = res.get("proposed_changes") if status == "not accepted" else None
    if status == "not accepted" and not proposed:
        proposed = "Replace the clause with the playbook starting position: " + (
            rule.get("example_language") or rule.get("starting_position", ""))
    return {
        "rule": rule_id(rule, idx),
        "heading": rule_heading(rule),
        "status": status,
        "fallback_used": fallback["position"] if fallback else None,
        "fallback_index": fallback["index"] if fallback else None,
        "effective_term": res.get("effective_term"),
        "reasoning": res.get("reasoning"),
        "citations": cites,
        "proposed_changes": proposed,
    }


# ---------------------------------------------------------------- review

def review(contract):
    meta, rules, pb_name = load_playbook()
    key = hashlib.sha256((json.dumps(rules, sort_keys=True) + json.dumps(meta, sort_keys=True) + "\0" + contract).encode()).hexdigest()
    with _cache_lock:
        if key in _mem_cache:
            return _mem_cache[key]
    path = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(path):
        with open(path) as f:
            out = json.load(f)
        with _cache_lock:
            _mem_cache[key] = out
        return out

    deadline = time.time() + REQUEST_BUDGET
    segs = segment(contract)
    seg_by_id = {s["id"]: s for s in segs}
    hits, a_failed = stage_a(rules, segs, deadline - 150)
    kw = keyword_hits(rules, segs)
    prec = [s for s in segs if PRECEDENCE_RE.search(s["text"])]
    intro = {}  # container label -> id of its opening segment (carries status like "superseded")
    for s_ in segs:
        if s_["container"] and s_["container"] not in intro:
            intro[s_["container"]] = s_["id"]
    text_norm = norm_ws(contract)

    def run(i):
        r = rules[i]
        k = str(rule_id(r, i))
        cand_ids = hits.get(k, set()) | kw.get(k, set())
        cand = [seg_by_id[c] for c in cand_ids]
        cand_ids = set(cand_ids) | {intro[c["container"]] for c in cand if c["container"] in intro}
        cand = [seg_by_id[c] for c in cand_ids]
        try:
            res = stage_b(meta, r, i, cand, prec, deadline)
            return finalize(r, i, res, seg_by_id, text_norm), True
        except Exception as e:  # noqa: BLE001
            print(f"stage B rule {k} failed: {e}", file=sys.stderr)
            fb = {
                "status": "not accepted" if cand else "not applicable",
                "reasoning": "Automated review could not complete for this rule; "
                + ("clauses on this subject were located and require manual review against the starting position."
                   if cand else "no clause on this subject was located in the draft."),
                "citations": [{"segment": c["id"], "quote": ""} for c in sorted(cand, key=lambda s: s["start"])[:5]],
            }
            return finalize(r, i, fb, seg_by_id, text_norm), False

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(run, range(len(rules))))
    out_rules = [r for r, _ in results]
    complete = all(ok for _, ok in results) and not a_failed
    counts = {s: sum(1 for r in out_rules if r["status"] == s) for s in STATUSES}
    out = {
        "playbook": meta.get("name") or pb_name,
        "model": MODEL,
        "summary": counts,
        "results": out_rules,
    }
    if complete:  # only cache full reviews so a transient failure is retried
        with _cache_lock:
            _mem_cache[key] = out
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "w") as f:
                json.dump(out, f, indent=1)
        except OSError:
            pass
    return out


def to_markdown(rv):
    lines = [f"# Playbook review — {rv['playbook']}", ""]
    lines.append("| Rule | Heading | Status |")
    lines.append("|---|---|---|")
    for r in rv["results"]:
        st = r["status"] + (f" (fallback {r['fallback_index']})" if r.get("fallback_used") else "")
        lines.append(f"| {r['rule']} | {r['heading']} | **{st}** |")
    for r in rv["results"]:
        lines += ["", f"## Rule {r['rule']}: {r['heading']} — {r['status'].upper()}"]
        if r.get("fallback_used"):
            lines.append(f"Fallback {r['fallback_index']}: {r['fallback_used']}")
        if r.get("effective_term"):
            lines.append(f"**Effective term:** {r['effective_term']}")
        lines.append(f"**Reasoning:** {r.get('reasoning')}")
        for c in r["citations"]:
            lines.append(f"- {c['section']} (lines {c['lines']}): \"{c['quote']}\"")
        if r.get("proposed_changes"):
            lines.append(f"**Proposed changes:** {r['proposed_changes']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urlparse(self.path).path in ("/", "/health"):
            self._send(200, json.dumps({"status": "ok"}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/review":
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            contract = payload["contract"]
            assert isinstance(contract, str) and contract.strip()
        except Exception:  # noqa: BLE001
            return self._send(400, json.dumps({"error": 'body must be JSON {"contract": "<text>"}'}))
        try:
            rv = review(contract)
        except Exception as e:  # noqa: BLE001
            return self._send(500, json.dumps({"error": str(e)}))
        if parse_qs(u.query).get("format", [""])[0] == "md":
            return self._send(200, to_markdown(rv), "text/markdown")
        self._send(200, json.dumps(rv, indent=1))

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main():
    if not BASE_URL or not API_KEY:
        print("WARN: LITMUS_AI_BASE_URL / LITMUS_AI_API_KEY not set", file=sys.stderr)
    threading.Thread(target=pick_model, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"listening on {port}", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--segments":
        for s in segment(open(sys.argv[2]).read()):
            print(s["id"], s["start"] + 1, s["end"], seg_label(s)[:100])
    else:
        main()
