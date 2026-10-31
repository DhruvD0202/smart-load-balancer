# F2_Team3 — VTOP submission checklist

Project: Smart Load Balancer for Distributing Web Traffic

Slot: F2 | Team: 3

Team: Dhruv Dudeja (24BCE0968), Ayush Bhatt (24BDS0180)

Faculty: Professor Sreethar S

## Assignment uploads

| VTOP assignment | Submit |
| --- | --- |
| 8 — Project Report | [F2_Team3_Report.pdf](F2_Team3_Report.pdf) |
| 9 — Project Presentation | [F2_Team3_PPT.pptx](F2_Team3_PPT.pptx) |
| 10 — GitHub Repository Link | https://github.com/DhruvD0202/smart-load-balancer |

[F2_Team3_Report.docx](F2_Team3_Report.docx) is the editable report; upload the
PDF for Assignment 8. Use the named submission copies, rather than older drafts.
The presentation has white backgrounds, 13 main slides and two hidden backup
slides, with speaker notes.

## Repository contents

- Python implementation: project-root `.py` files.
- Browser dashboard source: [demo_ui](../demo_ui).
- Runtime configuration: [config.json](../config.json).
- Software requirements, installation, execution and expected results:
  [README](../README.md).
- Automated checks: [tests](../tests).
- Dashboard screenshot: [classroom.png](../docs/evidence/browser-demo/classroom.png).
- Saved verification output: [browser verification](../docs/evidence/browser-demo/verification.txt)
  and [Week 3 demo results](../docs/evidence/week3/demo.txt).
- Recorded health, recovery and failure logs: [Week 3 evidence](../docs/evidence/week3).
- Report and presentation: the submission copies linked above.

Packet Tracer `.pkt` files are not applicable: this is a Python application
using real TCP/HTTP connections over loopback, not a Packet Tracer simulation.
No database is used. Python source filenames remain unchanged so imports and
documented execution commands continue to work.

## Before submitting

- Open the PDF and PPTX and confirm the team names and contents.
- Start `python demo_app.py` from the repository root and rehearse the
  [browser demo](../docs/browser-demo.md).
- If rechecking the implementation, run `python -m unittest discover -s tests -v`
  and `python demo_week3.py`. Saved evidence records previous successful runs;
  it is not a claim that these checks were rerun during submission packaging.
- Confirm GitHub contains the latest report, presentation and source files.
- The repository is private. Confirm the faculty's GitHub account has access
  and that any invitation has been accepted before submitting Assignment 10.
- Upload the PDF to Assignment 8, the PPTX to Assignment 9, and the repository
  URL to Assignment 10. Verify each upload and complete VTOP's submission step.

Preparing these files does not submit them to VTOP or grant repository access.
