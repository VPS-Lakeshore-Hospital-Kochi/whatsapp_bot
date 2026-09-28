# Lakeshore WhatsApp Assistant

One official WhatsApp number for VPS Lakeshore that serves **patients**, **staff** and
**clinicians**. It is designed so that when it isn't sure, it hands the person to a human
rather than guessing.

## How it answers

| Kind of message | What handles it | AI involved? |
|---|---|---|
| Emergency words ("chest pain", "unconscious", നെഞ്ചുവേദന…) from a patient | Fixed reply with the emergency number, plus an alert ticket | No |
| Medical questions from patients (dose, symptoms, "what does my report mean") | Passed to a nurse/doctor callback | No |
| Appointments, report status, doctors & OP days | Menus backed by the HIS (Elider) | No |
| "Talk to a person" / anything the bot can't answer | Handoff ticket | No |
| Everything else (timings, directions, policies, SOPs, protocols) | Claude, answering **only** from approved documents, with the source named | Yes |

Controls against wrong answers in the AI path (`app/answer.py`):

1. **Approved documents only.** The bot reads Markdown files in `knowledge/`. Each has an
   owner and a review date. Drafts and anything past its review date are ignored automatically.
2. **Role-filtered retrieval.** Patients can't retrieve staff or clinician documents, and the
   model never sees them for a patient question.
3. **No match, no model call.** If nothing relevant is found, the bot says so.
4. **Must cite.** Claude returns structured output listing the sources it used. An answer
   citing nothing, or citing a source it wasn't given, is thrown away.
5. **Second check.** Another Claude call confirms every claim in the draft appears in the cited
   text. If any claim doesn't, the draft is thrown away.
6. **Sources shown to the user.** Every answer ends with the document title and version.
7. **Test set before every release.** `evals/questions.yaml` holds questions with expected
   outcomes; `python -m evals.run` gates changes.

Staff and clinicians log in by typing `login` and then their employee ID. The WhatsApp number
must match the HR staff directory, and the employee ID is the second check. Sessions last 12 hours.
Three wrong attempts lock the number for 30 minutes. Patient lookups need **UHID + date of
birth** every 30 minutes, since families often share one phone.

Everything is written to an audit table, with phone numbers hashed and identifiers redacted.

## Handoff desk (`/desk`)

Everything the bot passes to a person lands on the handoff desk: emergencies, medical
questions, booking requests, "talk to a person", and questions it won't guess at.

- **Queue:** emergencies first, then medical questions, then the rest, with unclaimed and oldest
  first within each group. Tiles show how many people are waiting, open emergencies, the longest
  wait and how many tickets were resolved today. The page refreshes itself when something changes.
- **Ticket:** the person's full message thread, their number (tap to call), and actions:
  *I'll handle this*, reply on WhatsApp, *Mark resolved* (with a note), *Reopen*.
- **Conversation:** once staff reply, the person's next messages go into the ticket instead of to
  the bot, until the ticket is resolved or they type *menu*. Emergency words are still caught first.
- **WhatsApp's 24-hour rule:** typed replies only work within 24 hours of the person's last
  message. After that the desk shows a call prompt instead of the reply box.

**Signing in.** There are no passwords. Mark desk staff with `desk=yes` in the staff directory
CSV. They message the bot `login`, enter their employee ID, then type `desk`. The bot replies with
a one-time link that is valid for 5 minutes and starts a 12-hour session. Also:
- Set `PUBLIC_BASE_URL` to the bot's https address.
- Optionally set `DESK_ALLOWED_CIDRS` to the hospital's office IP ranges, so the desk only opens
  from inside the hospital.

**Security.**
- Sessions are held server-side, with an HttpOnly, Secure, SameSite=Strict cookie.
- Every action needs a CSRF token.
- Pages send a strict Content-Security-Policy and aren't cached. Patient text is always
  HTML-escaped.
- Every desk action is written to the audit log with the employee ID.

The queue view masks phone numbers. The full thread (unredacted, because staff need it to help)
is only on the ticket page. Agree its retention period with the DPO.

## Project layout

```
app/
  main.py            FastAPI webhook (Meta signature check, dedupe, background handling)
  router.py          Decides what happens to each message (order of checks is documented there)
  safety.py          Emergency / clinical-question detection, log redaction
  identity.py        Staff directory + employee-ID login
  answer.py          Cited, verified answers from the knowledge base (Claude API or Bedrock)
  knowledge/         Document loader (owner, audience, expiry) and BM25 search
  his/               Hospital system interface: mock data now, Elider adapter stub
  whatsapp/          Cloud API sender, webhook parsing, signature verification
  audit.py           Audit log
  handoffs.py        Handoff tickets and their message threads
  desk/              Handoff dashboard: routes, sign-in, templates, Lakeshore-branded CSS
knowledge/           PUT REAL APPROVED CONTENT HERE (see knowledge/README.md)
examples/knowledge/  Fake sample documents for development and tests
evals/               Release-gate question set and runner
tests/               Unit and flow tests (no network, no API key needed)
```

## Run it locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                   # no keys needed

cp .env.example .env                     # leave WA_ACCESS_TOKEN empty = dry run
export KNOWLEDGE_DIR=examples/knowledge REDIS_URL= DATABASE_URL=sqlite:///data/audit.db
uvicorn app.main:app --reload
```

With an `ANTHROPIC_API_KEY` set, `python -m evals.run --knowledge examples/knowledge` runs the
question set against the real model.

## Going live

### 1. WhatsApp number (Meta WhatsApp Cloud API, used directly)

Use Meta's Cloud API directly rather than a reseller (Gupshup, Interakt, AiSensy and similar).
It is the cheapest option, and resellers add a markup and another party handling patient messages.

1. Verify **VPS Lakeshore** in Meta Business Manager (business.facebook.com) with GST/incorporation documents.
2. Create a WhatsApp Business app and add a **dedicated number** that isn't already on the
   WhatsApp app (a new SIM or a landline that can receive an OTP call).
3. Create a **System User** and generate a permanent token with `whatsapp_business_messaging`.
   Set `WA_ACCESS_TOKEN`, `WA_PHONE_NUMBER_ID` and `WA_APP_SECRET`.
4. Set the webhook URL to `https://<your-domain>/webhook`, set the verify token to
   `WA_VERIFY_TOKEN`, and subscribe to `messages`.
5. Apply for the verified business badge once the display name is approved.
6. For bot-initiated messages (appointment reminders, "your report is ready"), get **message
   templates** approved. Replies to patients who message first are free-form within 24 hours.

Pricing: Meta charges per template message by category (utility, authentication, marketing).
Replies within the 24-hour customer-service window are currently free. Check Meta's current
India rates before budgeting.

### 2. Hosting: AWS Mumbai (`ap-south-1`)

This is the cheapest reliable option for a Kochi hospital: low latency, data held in India,
and mature managed services.

- **Pilot:** one small EC2 instance (e.g. `t4g.small`) running `docker compose up -d`, with a
  load balancer or Caddy/nginx for HTTPS, daily EBS snapshots, and CloudWatch alarms on
  `/health`. Roughly US$15–30/month plus AI usage.
- **Production:** ECS Fargate (2 tasks), RDS Postgres, ElastiCache Redis, an ALB with AWS WAF,
  and Secrets Manager for tokens.

### 3. AI model: Claude

- `LLM_PROVIDER=anthropic`: Claude API with an API key. This is the simplest option and
  includes server-side refusal fallbacks.
- `LLM_PROVIDER=bedrock`: Claude through AWS Bedrock, using the server's IAM role. Choose this if
  your DPO wants inference kept inside your AWS account. **Check which Claude models Bedrock
  serves in, or routes from, `ap-south-1`**, since cross-region inference may process data outside India.

The default is `claude-opus-5` at `low` effort. You can switch to `claude-sonnet-5` or
`claude-haiku-4-5` with `LLM_MODEL` if the eval results hold up. Each answered question costs
two model calls (answer plus verification).

### 4. Elider / Datamate integration

`app/his/elider.py` lists exactly what to request from Datamate: a read-only API, HL7/FHIR
feeds, or read-only reporting views. It covers patient verification (UHID + DOB), upcoming
appointments, report status, departments and doctors' OP schedules. Until the adapter is built,
`HIS_ADAPTER=mock` serves fake data so the rest can be piloted.

### 5. Before patients use it

- [ ] DPO sign-off: privacy notice URL, consent text, retention period for the audit and handoff tables
- [ ] ED/Quality review of the emergency and clinical word lists in `app/safety.py`,
      including Malayalam and Manglish phrasing
- [ ] Real `EMERGENCY_PHONE`, `FRONT_DESK_PHONE`, `REPORT_PICKUP_NOTE` set
- [ ] Desk staff marked `desk=yes` in the staff directory, a screen at the front office running
      `/desk` during OP hours, and a named owner for after hours
- [ ] Approved documents in `knowledge/`, each with a department owner
- [ ] Evals: 200+ questions per audience, 100% on emergency/handoff cases, and no wrong
      answers (declining is acceptable)
- [ ] Penetration test of the public webhook

## Suggested rollout

1. **Staff pilot** (about 100 staff): HR, IT and policy questions. Low risk, and it builds the eval set from real questions.
2. **Patients**: appointments, report status, directions, visiting hours.
3. **Clinicians**: protocols and formulary.
4. **Later**: booking and cancelling with an explicit confirmation step, outbound reminders via
   templates, emergency alerts to the duty phone, and embedding search for paraphrased or Malayalam questions.

## Not built yet

- Elider adapter (waiting on the vendor spec)
- Alerts for new tickets outside the desk page (e.g. SMS/phone to the duty nurse for emergencies);
  today someone has to have `/desk` open
- Loading the staff directory from HRMS automatically (it is a CSV export for now)
- Outbound template messages
- Retention and purge job for audit data
