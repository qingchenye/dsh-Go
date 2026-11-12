import sys
import glob
import xml.etree.ElementTree as ET

# Temporary helper for reverse-0NN. usage: junit.py <label> <dir> <ClassSimpleName>...
# Prints per-class counts and every method's result; failing cases get the first 25 stack lines.
label, d = sys.argv[1:3]
classes = sys.argv[3:]
files = sorted(glob.glob(d + "/TEST-*.xml"))
print("%s: junit files=%d" % (label, len(files)))
byclass = {}
for f in files:
    root = ET.parse(f).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for s in suites:
        for c in s.iter("testcase"):
            cls = c.get("classname") or ""
            state = "pass"
            body = ""
            for kind in ("failure", "error", "skipped"):
                n = c.find(kind)
                if n is not None:
                    state = kind
                    body = (n.get("message") or "") + "\n" + (n.text or "")
                    break
            byclass.setdefault(cls, []).append((c.get("name"), state, c.get("time"), body))
bad = 0
for want in classes:
    matched = [k for k in byclass if k.split(".")[-1] == want]
    if not matched:
        print("%s: CLASS %s NOT FOUND in XML" % (label, want))
        bad += 1
        continue
    for k in matched:
        cases = byclass[k]
        cnt = {"pass": 0, "failure": 0, "error": 0, "skipped": 0}
        for _, st, _, _ in cases:
            cnt[st] += 1
        print("%s: CLASS %s tests=%d pass=%d failures=%d errors=%d skipped=%d" % (
            label, k, len(cases), cnt["pass"], cnt["failure"], cnt["error"], cnt["skipped"]))
        bad += cnt["failure"] + cnt["error"] + cnt["skipped"]
        for name, st, t, body in cases:
            print("%s: case %s.%s state=%s time=%s" % (label, want, name, st, t))
            if st in ("failure", "error"):
                for line in body.splitlines()[:25]:
                    print("%s: stack| %s" % (label, line))
total = sum(len(v) for v in byclass.values())
print("%s: all XML cases=%d, requested-class problems=%d" % (label, total, bad))
if bad:
    sys.exit(3)
