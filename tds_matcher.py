"""
tds_matcher.py  -  content-based "best match" finder for TDS PDFs
===================================================================
Instead of requiring the filename to equal the model number, this reads the
TEXT inside every TDS in the repository (first pages + filename + folder
names) and ranks them against each BOQ line item (Description, Make, Model).

It is a free, offline ranking model (IDF-weighted keyword coverage + model
number detection) - no API key, no per-call cost. It reports a confidence
level for every match so doubtful ones can be reviewed instead of silently
used.

Scoring (per line item, per TDS):
  model signal  : model == filename (100) | model contained in filename (70)
                  | model found inside the PDF text (45) | all model words found (25)
  coverage      : share of the line item's keywords (weighted by rarity across the
                  repository, so common words like "cable" count little) that
                  appear in the TDS            -> up to 50 points
  make bonus    : manufacturer name in filename/folder (+8) or in text (+5)
Confidence: High / Medium / Low (see _confidence()).
"""

import math
import re
from collections import Counter
from pathlib import Path

import pymupdf

TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")

STOP = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "with", "by", "as",
    "is", "are", "be", "shall", "should", "will", "this", "that", "these", "those", "from",
    "per", "including", "include", "includes", "minimum", "maximum", "latest", "version",
    "not", "have", "has", "etc", "any", "all", "each", "such", "if", "than", "then", "into",
    "over", "under", "up", "out", "only", "also", "must", "may", "can", "it", "its",
    "supply", "supplying", "installation", "installing", "testing", "commissioning", "laying",
    "termination", "certified", "fixing", "complete", "work", "works", "tender", "drawings",
    "drawing", "technical", "specification", "specifications", "engineer", "issued", "pdf",
    "datasheet", "data", "sheet", "tds", "type", "floor", "na", "nos", "no",
}
NO_MAKE = {"", "na", "n/a", "-", "none", "nil", "tbd"}

CONF_RANK = {"High": 3, "Medium": 2, "Low": 1}


def stem(t):
    """Very light plural stemming: racks->rack, cables->cable, switches->switch, batteries->battery."""
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith("es") and t[-3] in "sxz":
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss") and not t[-2].isdigit():
        return t[:-1]
    return t


def tokenize(text):
    """Unigrams (noise filtered) + number/word pairs such as '4_core', 'cat_6', '1.0_sqmm'
    so items that differ only by a number are told apart."""
    raw = [stem(t) for t in TOKEN_RE.findall((text or "").lower())]
    out = []
    for t in raw:
        if t in STOP:
            continue
        if t[0].isdigit():
            if len(t) >= 3:
                out.append(t)
        elif len(t) >= 2:
            out.append(t)
    for a, b in zip(raw, raw[1:]):
        a_num, b_num = a[0].isdigit(), b[0].isdigit()
        if a_num != b_num:
            word = b if a_num else a
            if len(word) >= 2 and word not in STOP:
                out.append(a + "_" + b)
    return out


def compact(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def extract_text(path, max_pages=8, max_chars=150_000):
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return ""
    parts, n = [], 0
    try:
        for page in doc[:max_pages]:
            t = page.get_text()
            parts.append(t)
            n += len(t)
            if n >= max_chars:
                break
    finally:
        doc.close()
    return "".join(parts)[:max_chars]


class Doc:
    __slots__ = ("path", "name", "stem", "folder", "name_set", "text_set",
                 "stem_c", "text_c", "has_text", "tf", "length", "title_set")

    def __init__(self, path, name, folder, text):
        self.path = Path(path)
        self.name = name
        self.stem = name.rsplit(".", 1)[0].strip()
        self.folder = folder
        self.name_set = set(tokenize(self.stem + " " + folder))
        toks = tokenize(text)
        self.text_set = set(toks)
        self.tf = Counter(toks)
        self.length = max(len(toks), 1)
        self.title_set = set(tokenize(text[:300]))
        self.stem_c = compact(self.stem)
        self.text_c = compact(text)
        self.has_text = len(text.strip()) >= 50


class MatchResult:
    def __init__(self, doc, score, confidence, reason, alternatives):
        self.doc, self.score, self.confidence = doc, score, confidence
        self.reason, self.alternatives = reason, alternatives  # [(name, score), ...]

    @property
    def path(self):
        return self.doc.path if self.doc else None


class TDSLibrary:
    def __init__(self):
        self.docs = []
        self.idf = {}

    # ---- building ---------------------------------------------------------
    def add(self, path, name=None, folder=""):
        path = Path(path)
        self.docs.append(Doc(path, name or path.name, folder, extract_text(path)))

    @classmethod
    def from_dir(cls, root, progress_cb=None):
        lib = cls()
        root = Path(root)
        pdfs = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"]
        for i, p in enumerate(pdfs, 1):
            lib.add(p, p.name, str(p.parent.relative_to(root)).replace("\\", " ").replace("/", " "))
            if progress_cb and i % 10 == 0:
                progress_cb(f"Reading TDS files... {i}/{len(pdfs)}")
        lib.finalize()
        return lib

    def finalize(self):
        n = len(self.docs)
        df = Counter()
        for d in self.docs:
            df.update(d.name_set | d.text_set)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self._n = n
        self.avglen = (sum(d.length for d in self.docs) / n) if n else 1.0

    @property
    def count(self):
        return len(self.docs)

    # ---- matching ---------------------------------------------------------
    def _idf(self, tok):
        # unseen tokens are not in any doc, so they can never match; weight irrelevant
        return self.idf.get(tok, math.log(1 + self._n + 0.5))

    def prepare(self, row):
        """Tokenize a BOQ line item once (not once per TDS)."""
        model = (row.get("Model no.") or row.get("Model") or "").strip()
        make = (row.get("Make") or "").strip()
        desc = row.get("Description") or ""
        weights, head = {}, set()
        # BOQ descriptions name the product first ("Supply ... of Duct type Temperature Sensor
        # with ...") and give specs afterwards, so the opening words count extra.
        head_words = " ".join(desc.split()[:14])
        head = set(tokenize(head_words))
        for t in tokenize(desc):
            w = 1.5 if "_" in t else 1.0
            if t in head:
                w *= 2.5
            weights[t] = max(weights.get(t, 0), w)
        for t in tokenize(model):
            weights[t] = max(weights.get(t, 0), 3.0)
        has_make = make.lower() not in NO_MAKE
        make_tokens = set(tokenize(make)) if has_make else set()
        for t in make_tokens:
            weights[t] = max(weights.get(t, 0), 1.5)
        return {"model": model, "model_c": compact(model), "model_tokens": set(tokenize(model)),
                "weights": weights, "head": head, "make_tokens": make_tokens}

    def score(self, doc, q):
        reasons = []
        model_c = q["model_c"]
        msig = 0
        if len(model_c) >= 3:
            why = ""
            if model_c == doc.stem_c:
                msig, why = 100, "model = filename"
            elif len(model_c) >= 4 and len(doc.stem_c) >= 4 and (model_c in doc.stem_c or doc.stem_c in model_c):
                msig, why = 70, "model ~ filename"
            elif len(model_c) >= 4 and model_c in doc.text_c:
                msig, why = 45, "model found in TDS text"
            elif len(q["model_tokens"]) >= 2 and q["model_tokens"] <= (doc.name_set | doc.text_set):
                msig, why = 25, "model words found in TDS"
            if why:
                reasons.append(why)

        total = hit = 0.0
        matched = 0
        len_norm = 0.25 + 0.75 * (doc.length / self.avglen)
        for t, w in q["weights"].items():
            if t not in self.idf:  # word appears in no TDS at all -> ignore, don't penalise
                continue
            idf = self.idf[t]
            if t in q["head"]:
                idf = max(idf, 1.0)   # the product name must count even if the word is common
            val = w * idf
            total += val
            tf = doc.tf.get(t, 0)
            if t in doc.title_set:
                tf += 3          # words in the datasheet heading are the strongest evidence
            if t in doc.name_set:
                tf += 3          # ...as are words in the filename / folder name
            if tf:
                hit += val * (tf / (tf + 1.2 * len_norm))
                matched += 1
        coverage = hit / total if total else 0.0
        if coverage:
            reasons.append(f"{coverage:.0%} keyword coverage")

        make_bonus = 0
        mk = q["make_tokens"]
        if mk:
            if mk & doc.name_set:
                make_bonus = 8
            elif mk & doc.text_set:
                make_bonus = 5
            if make_bonus:
                reasons.append("make matches")

        score = msig + 50 * coverage + make_bonus
        return score, msig, coverage, matched, "; ".join(reasons)

    @staticmethod
    def _confidence(msig, coverage, margin, n_matched):
        if msig >= 70:
            return "High"
        if msig >= 45 and coverage >= 0.20:
            return "High"
        if msig >= 45:
            return "Medium"
        # Description/make only (no model evidence): never "High" - a human should confirm.
        if coverage >= 0.50 and margin >= 0.05 and n_matched >= 4:
            return "Medium"
        return "Low"

    def match(self, row, top_k=3):
        if not self.docs:
            return MatchResult(None, 0, "Low", "repository is empty", [])
        scored = []
        q = self.prepare(row)
        for d in self.docs:
            s, msig, cov, nm, why = self.score(d, q)
            scored.append((s, msig, cov, why, d, nm))
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0]
        second_cov = scored[1][2] if len(scored) > 1 else 0.0
        conf = self._confidence(best[1], best[2], best[2] - second_cov, best[5])
        alts = [(x[4].name, round(x[0], 1)) for x in scored[1:top_k]]
        return MatchResult(best[4], round(best[0], 1), conf, best[3], alts)


class AIMatchProvider:
    """Plugs into tds_automation.run(): .match_row(row) -> (Path|None, info dict)."""

    def __init__(self, library, min_confidence="Medium"):
        self.library = library
        self.min_rank = CONF_RANK[min_confidence]

    @property
    def count(self):
        return self.library.count

    def find(self, model_value):  # legacy exact lookup (not used when match_row exists)
        return None

    def match_row(self, row):
        m = self.library.match(row)
        usable = m.doc is not None and CONF_RANK[m.confidence] >= self.min_rank
        info = {
            "Matched TDS": m.doc.name if m.doc else "",
            "Confidence": m.confidence,
            "Score": m.score,
            "Used": "Yes" if usable else "No",
            "Why": m.reason,
            "Other Candidates": " | ".join(f"{n} ({s})" for n, s in m.alternatives),
            "TDS has text layer": "Yes" if (m.doc and m.doc.has_text) else "No",
        }
        return (m.path if usable else None), info
