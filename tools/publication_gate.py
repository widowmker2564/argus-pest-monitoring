# -*- coding: utf-8 -*-
"""Publication gate.

Scans every text file that would ship and reports anything matching a
publication-risk pattern. Run before every push. Exit code 1 = do not push.
"""
import io, os, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKIP_DIRS = {".git", "_private", "node_modules", "__pycache__", ".venv"}
TEXT_EXT = {".md", ".py", ".json", ".txt", ".sh", ".ps1", ".yml", ".yaml",
            ".js", ".html", ".css", ".manifest", ".cfg", ".ini", ".service",
            ".spec", ".drawio"}

# (label, regex, allowlist regex applied to the matching LINE)
CHECKS = [
    ("aws-access-key-id", r"A(?:KIA|SIA)[0-9A-Z]{16}", None),
    ("aws-secret-key-literal",
     r"(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{40}", None),
    ("github-token", r"(?:ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})", None),
    ("private-key-block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", None),

    # password segment must not be a placeholder (<...>) or a shell expansion
    ("rtsp-inline-password", r"rtsp://[^\s:<$]+:(?![<$])[^\s@]+@",
     r"<REDACTED>|<SET_ON_VM>|\bRTSP_PASS\b|example\.com|\buser:pass@"),

    # --- the content rule: no site imagery in a training set ---
    ("training-set-leak",
     r"(?i)\bcagfeed|\bcag_cag_|_v9r_burned|burn(?:ed|-in)\s+(?:image|into\s+TRAIN)"
     r"|burned\s+\d+\s+images|\bBURNS\s+batch|all\s+CAG\s+in\s+TRAIN"
     r"|CAG\s+images?\s+(?:hand-)?(?:labelled\s+)?(?:burned|in\s+(?:the\s+)?train)"
     r"|in\s+the\s+v9r\s+train\s+set|v9r-burned|\d+\s+burned\s+images"
     r"|holdout\s+image[s]?\s+(?:entered|in)\s+(?:a\s+)?train"
     r"|entered\s+(?:a|any)\s+training\s+set|images?\s+in\s+(?:the\s+)?training\s+set",
     r"never burned into|burned into an image|burned \d+ s\b"),
]


def main():
    findings = []
    scanned = 0
    for root, dirs, fs in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in fs:
            if os.path.splitext(f)[1].lower() not in TEXT_EXT:
                continue
            p = os.path.join(root, f).replace(os.sep, "/")
            if os.path.abspath(p) == os.path.abspath(__file__):
                continue  # the gate's own patterns are not findings
            try:
                lines = io.open(p, encoding="utf-8", errors="ignore").read().split("\n")
            except IOError:
                continue
            scanned += 1
            for label, pat, allow in CHECKS:
                rx = re.compile(pat)
                ax = re.compile(allow) if allow else None
                for i, line in enumerate(lines, 1):
                    if not rx.search(line):
                        continue
                    if ax and ax.search(line):
                        continue
                    findings.append((label, p, i, line.strip()[:150]))

    print("scanned %d text files\n" % scanned)
    if not findings:
        print("GATE PASS - nothing found.")
        return 0
    by = {}
    for label, p, i, line in findings:
        by.setdefault(label, []).append((p, i, line))
    for label in sorted(by):
        print("[%s] %d hit(s)" % (label, len(by[label])))
        for p, i, line in by[label][:25]:
            print("   %s:%d" % (p, i))
            print("      %s" % line)
        if len(by[label]) > 25:
            print("   ... %d more" % (len(by[label]) - 25))
        print()
    print("GATE FAIL - %d finding(s). Do not push." % len(findings))
    return 1


sys.exit(main())
