# Approved knowledge base

The bot answers free-text questions **only** from Markdown files in this folder. Anything not
written here, it will not say. Put real, approved Lakeshore content here, one topic per file,
in `patient/`, `staff/` or `clinician/` sub-folders (the folder is just for tidiness; the
`audience` field is what controls who can see it).

Every file starts with this header:

```yaml
---
title: Visiting hours            # shown to users as the source
owner: Front Office Manager      # who is accountable for keeping it correct
audience: [patient, staff]       # patient | staff | clinician
version: 1.0
approved_on: 2026-09-01
review_by: 2027-03-01            # after this date the file is automatically ignored
status: approved                 # anything else (draft, retired) is ignored
---
```

Then write the content in plain language with `## ` headings. Each `## ` section is searched
and cited separately, so keep one fact-cluster per section (e.g. "## General wards",
"## ICU visiting").

Writing tips that reduce wrong answers:

- State facts explicitly, including the obvious: "Visiting is not allowed in the ICU outside
  these hours."
- Put numbers, timings and phone numbers in the text exactly as they should be quoted.
- Use the words people will actually type (e.g. "OP", "outpatient", "consultation").
- One owner per file. When a policy changes, update the file and its version the same day.
- Never paste patient-identifiable information here.

Examples of the format (with fake content) are in `examples/knowledge/`. Don't copy those
values; they aren't real.
