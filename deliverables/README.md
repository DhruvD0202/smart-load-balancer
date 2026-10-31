# Project deliverables

## Submission report

- [PDF for submission](F2_Team3_Report.pdf)
- [Editable Word report](F2_Team3_Report.docx)

The report includes the architecture, networking concepts, HTTP and asyncio
design, scheduling algorithms, health checks, failure handling, recorded test
results, a dashboard screenshot, references and a demonstration checklist.
It uses white pages and includes the team and faculty details without an
institution name.

The measurements come from the saved project evidence. They demonstrate
functional behavior on loopback; they are not a production capacity benchmark.

## Final review presentation

Open [Smart Load Balancer — Final Review](F2_Team3_PPT.pptx)
in PowerPoint. The deck contains 13 main slides, followed by two hidden backup
slides for the code walkthrough and review questions.

The current edition uses white slide and table backgrounds.

The title slide includes Dhruv Dudeja (24BCE0968), Ayush Bhatt (24BDS0180), and
Professor Sreethar S. No institution is listed.

## Rehearse with the notes

Read each slide's Notes pane before rehearsing. Notes explain the concepts,
design choices, source references and expected demo outcomes. Presenter View
can display these notes during the talk.

Allow about 10 minutes for explanation plus five minutes for the demonstration.
Slide 10 contains the demo sequence; its notes contain the full checklist.
The two backup slides are skipped in a normal slideshow and remain available
in the slide list when a reviewer asks for code or more detail.

Start the app from the project folder:

```powershell
python demo_app.py
```

Keep that terminal running and open http://127.0.0.1:8090. Before presenting,
practice round-robin, weighted selection, least-connections, path routing,
backend failure/recovery, and `/truncate`.

For the unhealthy-server demonstration, choose round-robin first, Stop
backend-2, wait for `unhealthy`, then use manual **Send traffic** with `/hello`.
Start the backend again and wait for `healthy`. Guided experiments, algorithm
changes, and **Start fresh** restore all backends, so avoid those controls
during this failure experiment.

The screenshot and chart show recorded project results. If a live demonstration
cannot run, identify them as recorded evidence. These results establish
functional behavior and are not a production throughput benchmark.
