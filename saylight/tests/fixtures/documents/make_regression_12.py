"""Write regression_12.json: what Saylight 1.2 sent to the voice (sentences and chunks) for a corpus.

    python make_regression_12.py <repository>   (reads 1.2's text model from the v1.2.0 tag with git show)
"""
import json
import os
import subprocess
import sys
import tempfile

repo = sys.argv[1]
tmp = tempfile.mkdtemp(prefix="sl12_")
pkg = os.path.join(tmp, "saylight")
os.makedirs(pkg)
open(os.path.join(pkg, "__init__.py"), "w").close()
for name in ("academic_text.py", "text_model.py"):
    data = subprocess.run(["git", "-C", repo, "show", f"v1.2.0:saylight/{name}"], capture_output=True, check=True).stdout
    open(os.path.join(pkg, name), "wb").write(data)
sys.path.insert(0, tmp)
from saylight import text_model as tm  # noqa: E402  (1.2)

SAMPLES = [
    "Dyslexia affects reading (Smith et al., 2019; Jones, 2020). fMRI shows it, as shown in [3].",
    "Smith et al. (2019) showed that phonological skills (see also Brown, 2004, p. 12) predict reading.",
    "The DSM criteria (DSM-5; APA, 2013) are used [1], [2]-[4]. Values lie in the range [1, 10].",
    "Code is available at https://github.com/x/y. Data (https://osf.io/abc) are open.",
    "Children with dyslexia read slowly.12 However, accuracy improves (Snowling, 2020).",
    "(Smith, 2019)",
    "1 Introduction\nDyslexia is common (Peters et al.,\n2018a; Rad-\nford et al., 2018). It affects 7%\nof children.",
    "The effect was large [12, 15]. A 95% CI [12, 15] was reported. See Ref. [4] and [SB18].",
    "Results¹ were replicated²,³ in three labs. The area was 4 m².",
    "• First bullet (Lee, 2001)\n• Second bullet [7]\nPlain paragraph after the list.",
    "Some tasks, e.g. Stroop and flanker, i.e. conflict tasks, were used (see Fig. 2 and Eq. 3). It took approx. "
    "20 min, as reported on pp. 45–67 of the 2019–2020 report, with ~40 children per group.",
    "Figs. 3 and 4 show the effect; Eqs. (1) and (2) define it. E.g. the first model fits. I.e. it works.",
    "Abstract\n\nWe study reading in children with dyslexia (Snowling, 2000; Shaywitz et al., 2003). Our results "
    "suggest that phono-\nlogical training helps.\n\n2 Method\n\nParticipants were 120 children aged 7–11 "
    "(M = 9.2, SD = 1.1). Data are at https://doi.org/10.1000/xyz123 and the code at www.example.org/code.",
    "Journal of Reading Research 12 (2019) 1–20\nThe first page starts here and runs on to the next line\n"
    "without a break. It ends here.\nJournal of Reading Research 12 (2019) 1–20\n2\nThe second page continues "
    "the argument (Brown & Lee, 2018, pp. 3–9).",
    "A sentence with a footnote URL.\n1https://github.com/tensorflow/tensor2tensor\nWe primarily report results.",
    "Without TriviaQA fine-\n\nTable 2: SQuAD 1.1 results.\n\ntuning data, we only lose 0.1 F1.",
    "This is one very long sentence, " * 60 + "and it finally ends.",
    "Reading list \U0001f4da for the week, with 漢字 and Ünïcödé on one line. "
    "Done; next (cf. Smith, 2019, ch. 4).",
    "Spaced    out\ttext   here.  Second one!\r\n\r\nA new   paragraph? Yes…  Done.\n\n\n"
    "Third paragraph with a long tail",
    "1. Recruit participants\n2. Run the task (see Table 1)\n3. Analyse the data [5–7]\n\nThat was the plan.",
    "Introduction\n\nThe study begins here. It continues.",
    "Results were strong (Smith, 2019). See https://example.org/page for more.",
]

out = []
for text in SAMPLES:
    for sc, su in ((True, True), (False, False), (True, False)):
        doc = tm.Document.from_raw(text, skip_citations=sc, skip_urls=su)
        plans = tm.plan_chunks(doc, 0, first_chunk_id=0) if doc.sentences else []
        out.append({"raw": text, "skip_citations": sc, "skip_urls": su,
                    "sentences": [s.text for s in doc.sentences],
                    "chunks": [[p.text, p.offsets, p.ends] for p in plans]})
dest = os.path.join(repo, "saylight", "tests", "fixtures", "documents", "regression_12.json")
os.makedirs(os.path.dirname(dest), exist_ok=True)
with open(dest, "w", encoding="utf-8", newline="\n") as f:
    json.dump({"made_with": "Saylight 1.2.0 (text_model.Document.from_raw, plan_chunks)", "cases": out}, f,
              ensure_ascii=True, indent=0)
print(len(out), "cases ->", dest)
