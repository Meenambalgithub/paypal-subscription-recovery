# Build log: setting it up and testing it, week by week (macOS)

This is the full step-by-step guide used to build and verify the project, with a **Check**
after every step. For a quick start, see the [README](../README.md).

PayPal's retries recover what time can fix. This agent recovers what only the
customer can fix. Design: [HLD.md](HLD.md) and [LLD.md](LLD.md).

All commands run in the **Terminal** app (Cmd + Space, type "Terminal").
Each step has a **Check**. Don't move on until it passes.

## Step 1: PayPal sandbox accounts and app (United States)

Use **US-based** sandbox accounts. Default accounts are created in your own
country, and India-based sandbox accounts fail at subscription approval with
"We're sorry, things don't appear to be working at the moment."

1. Sign in at https://developer.paypal.com and open **Testing tools > Sandbox accounts**.
2. Click **Create account**: type **Business**, country **United States** (the merchant).
3. Click **Create account** again: type **Personal**, country **United States** (the test customer).
   Open it and note the email and password (use **Edit** to set your own password).
4. Open **Apps & Credentials** with the **Sandbox** toggle on, click **Create App**,
   type **Merchant**, and choose the **US Business** account as the owner.
5. Copy the **Client ID** and **Secret**.

## Step 2: Python 3.12 and project setup

Don't use the macOS built-in Python 3.9. Install **Python 3.12.x** from
https://www.python.org/downloads/macos/, then quit Terminal (Cmd + Q) and reopen it.

```bash
python3.12 --version                    # should print Python 3.12.x
cd ~/Downloads/paypal-recovery          # or wherever you unzipped the project
python3.12 -m venv .venv
source .venv/bin/activate               # prompt now starts with (.venv)
pip install -r requirements.txt
cp .env.example .env
nano .env                               # paste Client ID and Secret, no quotes or spaces
```
In nano: save with **Control + O**, then **Enter**; exit with **Control + X**.
Don't edit `.env` in TextEdit (it can add hidden formatting). VS Code is fine.

Finder hides files starting with a dot. Press **Cmd + Shift + .** in Finder to show them.

Run `source .venv/bin/activate` again in **every new Terminal window**.

**Check:** `python check_sandbox.py` prints "Credentials OK".

## Step 3: Create the product and plans

```bash
python setup_catalog.py
```
Creates Pipeline School with a Pro ($20) and a Lite ($10) plan, billed daily
so renewals show up fast. IDs are saved to `paypal_ids.json`.

If you ever switch to a new app or merchant account, run `rm paypal_ids.json`
first. Plans belong to the merchant that created them.

**Check:** `python check_sandbox.py` lists both plans as ACTIVE.

## Step 4: Subscribe as the test customer

```bash
uvicorn app:app --reload --port 8000
```
1. Open a **Chrome Incognito window** (Cmd + Shift + N) so no old PayPal login is reused.
2. Go to **http://localhost:8000**. Don't double-click `index.html`; opening the
   file directly causes "Failed to fetch".
3. Click Subscribe and sign in with the **US Personal** sandbox account.

**Check:** the page shows "You're subscribed" with status ACTIVE.
(Week 1 saved it to `subscribers.json`; from Week 2 it goes to Postgres, so Docker must be running.)

## Step 5: Webhooks through a tunnel

PayPal can't reach `localhost`, so expose it with ngrok.

**One-time install:**
```bash
brew install ngrok                       # or download from https://ngrok.com/download
ngrok config add-authtoken YOUR_TOKEN    # token from your free ngrok dashboard
```

**Every session, three Terminal windows (or tabs with Cmd + T):**

Window 1, the app:
```bash
cd ~/Downloads/paypal-recovery && source .venv/bin/activate
uvicorn app:app --reload --port 8000
```

Window 2, the tunnel:
```bash
ngrok http 8000                          # copy the https://....ngrok-free.app address
```

Window 3, register the address:
```bash
cd ~/Downloads/paypal-recovery && source .venv/bin/activate
python setup_webhook.py https://YOUR-ADDRESS.ngrok-free.app
nano .env                                # add the printed PAYPAL_WEBHOOK_ID=... line
```

The free ngrok address changes every time ngrok restarts. When it does,
re-run `setup_webhook.py` with the new address (it updates the existing webhook).
If your Mac sleeps, the tunnel stops. Keep it awake while waiting for renewals.

**Check:** `python check_sandbox.py` lists the webhook with your ngrok address.

## Step 6: Watch real events arrive

With the app and tunnel running:
- Subscribe again in Incognito. Subscriptions created before the webhook existed
  never sent their activation event.
- Wait for the daily renewal (see "Next payment" on the page; the time is UTC,
  so add 5:30 for IST).
- Optionally, send sample events from the **Webhooks simulator** in the developer dashboard.

```bash
cat webhook_events.log                   # every event received
tail -f webhook_events.log               # watch new events live (Control + C to stop)
```
**Check:** you see `BILLING.SUBSCRIPTION.ACTIVATED`, and later a renewal payment event.

## Step 7: Study the payloads (most important this week)

Open `webhook_events.log` and write down the answers. The LLD depends on them:
1. Where is the subscription ID in each event type?
2. Does the payment-failed event include a failure reason? Which field?
3. What does a renewal payment event look like?
4. Can sandbox negative testing make a subscription payment fail? Which test values worked?

## Step 8: Local infrastructure (Docker Desktop)

Install **Docker Desktop for Mac** from https://www.docker.com/products/docker-desktop/
(pick Apple Silicon or Intel to match your Mac) and start it. Then:

```bash
docker compose -f infra/docker-compose.yml up -d
./infra/create_topics.sh
```
**Check:**
- The topic script lists the three topics.
- `docker exec -it recovery-redis redis-cli ping` returns PONG.
- `docker exec -it recovery-postgres psql -U recovery -c "\dt"` lists the tables.
- http://localhost:8025 opens the Mailpit inbox.

Stop everything with `docker compose -f infra/docker-compose.yml down`.

---

# Week 2: verified receiver, Kafka, processor, Postgres

What's new: the webhook receiver checks PayPal's signature and puts each event on
Kafka; a processor reads them and keeps subscribers and recovery cases in Postgres.
What the Week 1 payloads taught us is in [FINDINGS-week1.md](FINDINGS-week1.md).

```
PayPal ──► ngrok ──► app.py (receiver) ──► Kafka ──► processor ──► Postgres
                      checks signature      waiting    applies       saved
                      replies 200           line       the rules     records
```

Think of a restaurant: the **receiver** is the waiter (checks the order is genuine,
pins the ticket, says "got it"), **Kafka** is the ticket rail (tickets wait there in
order), and the **processor** is the cook (handles tickets one at a time and updates
the records). `[webhook] queued ...` in the app window means "genuine, and safely on the rail".

## Helper tools

| Command | What it does |
|---|---|
| `python show_state.py` | Prints what's in Postgres: subscribers, number of events, recovery cases, merchant approvals, cases waiting for the agent |
| `python check_events.py` | Lists events PayPal sent in the last 3 days; `MISSING` = not stored in Postgres |
| `python check_events.py --resend` | Asks PayPal to send the `MISSING` events again (safe: duplicates are ignored) |
| `python check_events.py --days 14 --resend` | Same, looking back 14 days instead of 3 (try fewer days if PayPal refuses) |
| `python check_events.py SUBSCRIPTION-ID` | Also shows that subscription's payments and current status |
| `python replay_events.py --type EVENT_TYPE` | Puts a logged sample event straight onto Kafka, labelled `simulator` |
| `curl http://localhost:8000/api/subscriptions/SUBSCRIPTION-ID` | Reads a subscription from PayPal and saves its current status |

Run every `python` command with `(.venv)` showing in the prompt
(`source .venv/bin/activate` in each new window).

## Step 9: Update the project

Unzip the Week 2 zip **over** your existing folder. Your `.env`, `paypal_ids.json`
and `webhook_events.log` aren't in the zip, so they're kept.

```bash
cd ~/Downloads
unzip -o paypal-recovery-week2.zip       # overwrites code files only
cd paypal-recovery && source .venv/bin/activate
pip install -r requirements.txt
```
**Check:** `python -c "import confluent_kafka, redis, psycopg; print('ok')"` prints ok.

## Step 10: Update the database

Start Docker Desktop first, then:
```bash
docker compose -f infra/docker-compose.yml up -d
./infra/create_topics.sh
docker exec -i recovery-postgres psql -U recovery < infra/migrations/002_week2.sql
```
Docker may print "Learn more at https://docs.docker.com/go/debug-cli/". That's a tip, not an error.

**Check:** `docker exec -it recovery-postgres psql -U recovery -c "\d recovery_cases"` lists `paypal_retry_at`.

## Step 11: Run the tests

```bash
python -m pytest
```
The processor tests use a separate `recovery_test` database and Redis db 15, so your
data is safe. If Docker isn't running they're **skipped**, not failed.
A `StarletteDeprecationWarning` about httpx is harmless.

**Check:** `89 passed`, none skipped.

## Step 12: Start the pipeline (four Terminal windows)

Window 1, the app:
```bash
cd ~/Downloads/paypal-recovery && source .venv/bin/activate
uvicorn app:app --reload --port 8000
```
Window 2, the tunnel:
```bash
ngrok http 8000
```
Window 3, the processor:
```bash
cd ~/Downloads/paypal-recovery && source .venv/bin/activate
python -m recovery.processor
```
Window 4, for commands:
```bash
cd ~/Downloads/paypal-recovery && source .venv/bin/activate
python check_sandbox.py
```
**Check:** the webhook URL `check_sandbox.py` lists is the address in Window 2. If not,
ngrok got a new address: run `python setup_webhook.py https://NEW-ADDRESS.ngrok-free.app`.

Tip: claim the free static domain in your ngrok dashboard and start ngrok with
`ngrok http --url=YOUR-NAME.ngrok-free.app 8000`. The address then never changes.

## Step 13: Real signature test

Subscribe again in a Chrome Incognito window at http://localhost:8000. Note the subscription ID.

**Check (Window 1):** three lines, in any order. PayPal often sends the payment before ACTIVATED:
```
[webhook] queued BILLING.SUBSCRIPTION.CREATED for I-...
[webhook] queued PAYMENT.SALE.COMPLETED for I-...
[webhook] queued BILLING.SUBSCRIPTION.ACTIVATED for I-...
```
`REJECTED unverified` instead means `PAYPAL_WEBHOOK_ID` in `.env` doesn't match the
webhook. Fix it and restart uvicorn (`--reload` doesn't watch `.env`).

**Check (Window 3):** three lines ending in `{'status': 'processed', ...}`.

**Check (Window 4):** `python show_state.py` lists the subscription as `ACTIVE`, source
`paypal`, with a `last_payment_at` from just now, and no recovery case.

## Step 14: Catch up on missed webhooks

Webhooks sent while ngrok, the app or your Mac was off are never delivered.
```bash
python check_events.py --resend
python show_state.py
```
**Check:** every `MISSING` event gets `resend requested`, Window 1 prints `queued` for each
within a minute or two, and running `python check_events.py` again shows none `MISSING`.

## Step 15: Test A, the processor is down (nothing is lost)

1. Window 3: stop the processor (Control + C).
2. Window 4: `python show_state.py` and note **Events stored**.
3. Make new events arrive: subscribe again in Incognito (or `check_events.py --resend`
   if anything is `MISSING`). Window 1 prints `queued` for each.
4. Window 4: `python show_state.py`. **Events stored is unchanged**: the events are waiting in Kafka.
5. Window 3: start the processor again (`python -m recovery.processor`).

**Check:** within seconds Window 3 processes every waiting event, and
`show_state.py` shows Events stored increased by the number Window 1 queued.

## Step 16: Test B, the same event twice is saved once

The sandbox can't make a renewal fail, so this uses the failure sample from PayPal's
Webhooks simulator, labelled `simulator` (its customer `I-BW452GLLEP1G` is fake).
```bash
python replay_events.py --type BILLING.SUBSCRIPTION.PAYMENT.FAILED
python show_state.py
```
**Check:** one recovery case for `I-BW452GLLEP1G`: category `CUSTOMER_ACTION`, reason
`PAYMENT_DENIED`, state `OPEN`, amount `500.00`, listed under "Agent schedule". A negative
`due_in` just means it's waiting for the Week 3 scheduler.

Run both commands again.

**Check:** nothing changes (same Events stored, still one case), and Window 3 logs
`{'status': 'duplicate', 'via': 'redis'}`.

Only replay with `--type`. Replaying the whole log re-adds Week 1 events labelled `simulator`.

## Step 17: Test C, Kafka is down (PayPal is told to retry)

1. Window 4: `docker stop recovery-kafka`
2. Subscribe again in Incognito. The page still says "You're subscribed" (that part needs
   only PayPal and Postgres).
3. **Check (Window 1):** for each event, after about 10 seconds:
   `[webhook] NOT queued ... Kafka unavailable, returned 500 so PayPal will retry`
4. Window 4: `docker start recovery-kafka`, then wait about 30 seconds.
5. Window 4: `python check_events.py --resend`. PayPal retries by itself too, but that can take a while.

**Check:** the three new events show as `MISSING` and are resent, Window 1 prints `queued`
for each, Window 3 processes them, and `show_state.py` lists the new subscription as `ACTIVE`.
If PayPal's own retries arrive later, Window 3 logs them as `duplicate`. That's correct.

## Step 18: Watch a real renewal

Leave everything running (and the Mac awake) until the next daily renewal.
Sandbox renewals arrive **hours after** `next_billing_time`, so don't wait for the exact time.

**Check:** `show_state.py` shows a newer `last_payment_at`. If it doesn't,
`python check_events.py --resend` recovers it.

## Week 2 is done when

- [ ] Step 11: 89 tests pass, none skipped
- [ ] Step 13: a real subscription's events are `queued` (signature verification works)
- [ ] Step 14: no `MISSING` events
- [ ] Step 15: events wait in Kafka while the processor is down
- [ ] Step 16: a replayed failure opens one case, and a second replay changes nothing
- [ ] Step 17: with Kafka down the receiver returns 500, and the events are recovered afterwards

## Week 2 troubleshooting

| Problem | Fix |
|---|---|
| Real events say `REJECTED unverified` | `PAYPAL_WEBHOOK_ID` in `.env` must match the ID `check_sandbox.py` lists. Restart uvicorn after editing `.env` |
| Nothing at all in Window 1 after subscribing | ngrok address changed: compare with `check_sandbox.py`, re-run `setup_webhook.py`, then `check_events.py --resend` |
| `NOT queued` / `Kafka publish failed` | Docker Desktop isn't running, Kafka is stopped, or run `./infra/create_topics.sh` |
| `ModuleNotFoundError` on a `python` command | The prompt has no `(.venv)`: run `source .venv/bin/activate` |
| `psycopg.OperationalError` | `docker compose -f infra/docker-compose.yml up -d` |
| `column "paypal_retry_at" does not exist` | Run the Step 10 migration |
| Status shows `APPROVAL_PENDING` for an active subscription | `curl http://localhost:8000/api/subscriptions/ID` refreshes it from PayPal |
| A resent event is still `MISSING` | PayPal sends resends one by one; wait two or three minutes and check again |
| Tests show `skipped` | Docker isn't running |
| Processor sends events to the DLQ | Read them: `docker exec -it recovery-kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic subscription-events-dlq --from-beginning` |

# Week 3: the agent, emails and the scheduler

What's new: when a case needs the customer, the **scheduler** wakes the **agent**. The
agent reads the case, Gemini decides what to say, and every action it asks for goes
through the **guardrails** (rules in code) before anything happens. Emails go to
**Mailpit** (http://localhost:8025), a local inbox that catches everything, so no real
person is ever emailed.

```
processor ──► schedule:followups ──► scheduler ──► agent ──► Gemini
 (opens case)   (Redis, due times)   (every 30 s)    │  ▲       (chooses wording
                                                     ▼  │        and next step)
                                               guardrails ── blocked: reason goes back to Gemini
                                                     │
                                                allowed ──► email (Mailpit), messages row,
                                                            next follow-up scheduled
```

**What the guardrails enforce** (`recovery/guardrails.py`):

| Rule | Example of what gets blocked |
|---|---|
| Risk and merchant-side cases never reach the customer (or the LLM) | `PAYER_ACCOUNT_RESTRICTED` |
| At most one email per customer per 24 hours | a second email the same day |
| Quiet hours, 21:00–08:00 in the customer's time zone | an email at 23:00 |
| The right kind of message for the case | "fix your payment" while PayPal is still retrying |
| At most 2 follow-ups, and only after the first message | a third reminder |
| No links written by the LLM (code adds the real PayPal link) | `https://paypa1-secure.example.com` |
| Never ask for passwords, codes or card numbers | "reply with your CVV" |
| Follow-ups between 12 hours and 7 days | "check again in 1 hour" |
| Close as LOST only after every follow-up | giving up after one email |

The LLM sees the customer's **first name only**: no email address, postal address or payer ID.
If Gemini is down or out of quota, nothing is sent and the case is retried in 15 minutes.

## Step 19: Update the project

```bash
cd ~/Downloads && unzip -o paypal-recovery-week3.zip && cd paypal-recovery
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest
```
**Check:** `140 passed`, none skipped (Docker must be running).

## Step 20: Configure the agent

Open `.env` (`nano .env`) and make sure it has these lines. Your `GEMINI_API_KEY` line
is already there from before; don't paste the key anywhere else.
```
LLM_PROVIDER=gemini
LLM_MODEL=gemini-3.6-flash
LLM_FALLBACK_MODEL=gemini-2.5-flash
CUSTOMER_TZ=Asia/Kolkata
QUIET_HOURS=on
```
`CUSTOMER_TZ=Asia/Kolkata` makes quiet hours (21:00–08:00) match your own clock while testing.

```bash
python check_llm.py
```
**Check:** both lines say `OK` and the last line says `LLM ready.`

## Step 21: First agent run, by hand

Case 1 (the simulated customer John, `PAYMENT_DENIED`, $500) has been waiting since Week 2.
Run the agent on it once, without the scheduler, so you can watch it:
```bash
python run_agent.py 1
```
**Check:**
- The output lists the tools the agent used, each `ALLOWED` or `BLOCKED` (with the reason).
  Expect at least `ALLOWED send_message` and `ALLOWED schedule_followup`. Gemini writes
  the wording, so it differs each time.
- `Agent:` gives a one-sentence summary.
- http://localhost:8025 shows an email to `customer@example.com` greeting John, with the
  "Update your payment method in PayPal" link and the safety line at the bottom.
- `python show_state.py`: case 1 is `CONTACTED`, one `FIX_PAYMENT` message, and the
  schedule says `in 1d 23h ...` (about 48 hours).

If it says `BLOCKED send_message (quiet hours ...)`, it's 21:00–08:00 now. That's the guardrail
working: run it again after 08:00, or set `QUIET_HOURS=off` in `.env` for now.

## Step 22: The guardrail stops a second email

```bash
python run_agent.py 1
```
**Check:** still only one email in Mailpit. If the agent tried to email again, the output shows
`BLOCKED send_message (the customer was already messaged in the last 24 hours)` or
`(... use FOLLOWUP)`, and the agent adapts by scheduling a follow-up instead.

## Step 23: Start the scheduler (fifth window)

From now on the agent runs by itself. Windows: 1 app, 2 ngrok, 3 processor, **4 scheduler**, 5 commands.
```bash
cd ~/Downloads/paypal-recovery && source .venv/bin/activate
python -m recovery.scheduler
```
**Check:** it prints `scheduler started (every 30s, model gemini-3.6-flash, quiet hours on in Asia/Kolkata)`.

## Step 24: A new failure, end to end

```bash
python simulate_failure.py --name Asha
```
**Check (within about a minute):**
- the processor window logs the event: `CUSTOMER_ACTION`, `OPEN`, `'schedule': True`;
- the scheduler window logs `case N -> {'status': 'ran', ...}`;
- Mailpit has a new email to `asha@customer.example.com`;
- `show_state.py` shows the new case `CONTACTED` with a follow-up in about 2 days.

## Step 25: Each kind of failure gets the right treatment

```bash
python simulate_failure.py --reason INTERNAL_SERVER_ERROR --name Ravi
python simulate_failure.py --reason PAYER_ACCOUNT_RESTRICTED --name Meera
```
**Check:**
- **Ravi** (PayPal-side error): case `WAITING_PAYPAL`, and his email is a short "no action
  needed, PayPal will retry" note with **no** payment link.
- **Meera** (risk): case `ESCALATED`, a row under "Merchant approvals", and **no email at all**.
  The scheduler never even picks her case up.

## Step 26: Quiet hours

Make it night for the customer: if it's daytime for you, set `CUSTOMER_TZ=America/Los_Angeles`
in `.env` (night there during most of the Indian day). Restart the scheduler (Control + C,
then `python -m recovery.scheduler`), then:
```bash
python simulate_failure.py --name Kiran
```
**Check:** no email for Kiran. `show_state.py` shows his case still `OPEN`, scheduled for 08:00
Los Angeles time, and "Agent actions" lists a `BLOCKED` `send_message`.
Set `CUSTOMER_TZ=Asia/Kolkata` back and restart the scheduler afterwards.

## Step 27: Gemini is down

In `.env`, temporarily change one character of `GEMINI_API_KEY`. Restart the scheduler, then:
```bash
python simulate_failure.py --name Dev
```
**Check:** no email for Dev, the scheduler window logs `llm_unavailable`, and `show_state.py`
shows his case scheduled again in about 15 minutes. Fix the key, restart the scheduler, and
within 15 minutes Dev's email arrives.

## Week 3 is done when

- [ ] Step 19: 140 tests pass
- [ ] Step 20: `check_llm.py` says `LLM ready.`
- [ ] Step 21: the agent emails John, and the case is `CONTACTED` with a follow-up scheduled
- [ ] Step 22: a second run sends no second email
- [ ] Step 24: a new failure becomes an email within a minute, with no manual step
- [ ] Step 25: a PayPal-side error gets a heads-up only, and a risk case gets no email
- [ ] Step 26: quiet hours hold the email until morning
- [ ] Step 27: with Gemini down nothing is sent, and the case retries by itself

## Week 3 troubleshooting

| Problem | Fix |
|---|---|
| `GEMINI_API_KEY is not set` | Add it to `.env`, then restart the scheduler |
| `check_llm.py` shows `429` | Free quota used up for now; wait, or try another model from `python find_model.py` |
| `check_llm.py` shows `404` | The model name is wrong; run `python find_model.py` and copy a `WORKS` name |
| Every run says `llm_unavailable` | Check the key and run `check_llm.py`; the case retries every 15 minutes on its own |
| Nothing in Mailpit | Docker must be running (`docker ps` lists `recovery-mailpit`); open http://localhost:8025 |
| `SMTP_HOST=... is not local` | `.env` must say `SMTP_HOST=localhost`; the project never uses a real mail server |
| Always `BLOCKED ... quiet hours` | It's 21:00–08:00 in `CUSTOMER_TZ`; wait, or set `QUIET_HOURS=off` while testing |
| Scheduler does nothing | `show_state.py` shows when cases are due; `python run_agent.py CASE_ID` runs one now |
| `.env` changes ignored | Restart the scheduler (and uvicorn) after editing `.env` |

# Week 4: customer replies and the merchant dashboard

What's new:
- **Customer replies.** Every fix-payment and follow-up email now ends with two one-click links:
  "Already updated it? Tell us and we'll retry now" and "Need a break instead? Pause for 30 days".
  A link opens a confirmation page; pressing the button wakes the agent, which retries the
  payment or pauses the subscription. Guardrails: it can only charge after the customer says
  they fixed it (and never more than is owed), and only pause when they asked (at most 30 days).
- **Merchant dashboard** at http://localhost:8000/dashboard: where the money went, every case,
  the review queue, and each case's step-by-step trail, including what the rules blocked.

Links are signed, so they can't be guessed or edited, and expire after 14 days. The dashboard
only opens on your own Mac: through the ngrok address it answers 403.

Simulated customers never touch PayPal: when one is "charged", a labelled simulated payment
event goes through the normal pipeline. For real subscriptions the same tools call PayPal's
sandbox (capture, suspend, activate).

## Step 28: Update the project

```bash
cd ~/Downloads && unzip -o paypal-recovery-week4.zip && cd paypal-recovery
source .venv/bin/activate
pip install -r requirements.txt
docker exec -i recovery-postgres psql -U recovery < infra/migrations/003_week4.sql
python -m pytest
```
**Check:** `153 passed`. Then restart the app (window 1), processor (3) and scheduler (4).

## Step 29: Open the dashboard

Open http://localhost:8000/dashboard.

**Check:**
- The headline says how much was recovered and how much is still at risk.
- The bar and its legend split the money: recovered after the agent emailed, recovered by
  PayPal's retry alone, paused, still at risk, lost.
- Every case from Week 3 is listed, marked "simulated" where it is.
- Meera is under "Needs your review" (the agent didn't contact her).
- Click a case: its trail lists PayPal events, emails (click "Read the email") and agent
  decisions. Kiran's shows "Blocked: Email to customer" with the quiet-hours reason.

Also open the dashboard through your ngrok address (`https://....ngrok-free.app/dashboard`).
**Check:** it refuses (403). Customer data stays on your Mac.

## Step 30: The customer says "I've fixed it"

```bash
python simulate_failure.py --name Priya
```
1. Wait for Priya's email in Mailpit (http://localhost:8025), then open it.
2. Click the link after **"Already updated it?"** (or copy it into the browser).
3. **Check:** a page greets Priya and shows **"Yes, retry my payment"**. Nothing has happened yet.
4. Press the button. **Check:** "Thank you".
5. Within about a minute, **check**:
   - the scheduler window shows the agent ran (`capture_outstanding` allowed);
   - the processor window logs a simulated `PAYMENT.SALE.COMPLETED` and `RECOVERED`;
   - the dashboard shows Priya **Recovered**, and "Recovered after the agent emailed" went up by $20.
6. Click Priya's row: the trail reads failure, email, "Customer: Says they've updated their
   payment method", "Retried the payment", then the payment event.

## Step 31: The customer asks for a pause

```bash
python simulate_failure.py --name Arjun
```
In Arjun's email, click the link after **"Need a break instead?"**, then press **"Yes, pause it"**.

**Check (within about a minute):**
- Mailpit has a second email to Arjun: "Your Pipeline School subscription is paused", with the
  restart date. It arrives even though he was emailed minutes ago: confirmations of something the
  customer asked for are exempt from the 24-hour rule (and are written by code, not the LLM).
- The dashboard shows Arjun **Paused at the customer's request**.
- Press the pause button again from the same email: the page says it's already paused.

## Step 32: Links can't be tampered with

Copy any reply link, change one character near the end, and open it.
**Check:** "Sorry. This link isn't valid." Nothing is recorded.

## Step 33: Merchant review

On the dashboard, press **"Mark as reviewed"** next to Meera.
**Check:** she leaves the review list, and her case shows **Closed**.

## Step 34: Pause and resume a real sandbox subscription

The agent's pause uses PayPal's suspend and activate calls. A real failure can't be made in the
sandbox, so prove the calls directly on a spare subscription:
```bash
python try_paypal_actions.py I-30W3598ERHD1
```
**Check:** `ACTIVE`, then `after suspend: SUSPENDED`, then `after activate: ACTIVE`. Window 1 then
shows `queued BILLING.SUBSCRIPTION.SUSPENDED` and `queued BILLING.SUBSCRIPTION.ACTIVATED`, and the
processor stores both without opening a case.

## Week 4 is done when

- [ ] Step 28: 153 tests pass
- [ ] Step 29: the dashboard shows the money, the cases and the review queue; through ngrok it's refused
- [ ] Step 30: "I've fixed it" leads to a retried payment and a Recovered case, with no manual step
- [ ] Step 31: "Pause" pauses the case and sends a confirmation
- [ ] Step 32: an edited link is refused
- [ ] Step 33: marking a review closes the case
- [ ] Step 34: suspend and activate work on a real sandbox subscription

## Week 4 troubleshooting

| Problem | Fix |
|---|---|
| `column "handled_at" does not exist` | Run the Step 28 migration line |
| Clicking a link does nothing | The app (window 1) must be running; links point to http://localhost:8000 |
| "This link has expired" | Links last 14 days; make a new failure with `simulate_failure.py` |
| Dashboard says 403 | Use http://localhost:8000/dashboard, not the ngrok address |
| Dashboard can't load the numbers | Docker must be running (Postgres and Redis) |
| Pressing the button works but nothing happens | The scheduler (window 4) must be running; it picks replies up within 30 s |
| Reply links stopped working after reinstalling | They're signed with `.reply_secret`; old emails' links stop working if it's deleted. Send a new failure |
| `try_paypal_actions.py` says PayPal refused | Use a subscription that's `ACTIVE` (see `show_state.py`) |

## Troubleshooting

| Problem | Fix |
|---|---|
| Page says "Failed to fetch" | Open http://localhost:8000 (not the file), and make sure uvicorn is running |
| PayPal window says "We're sorry, things don't appear to be working" | Use US sandbox accounts and an app owned by the US Business account (Step 1), then `rm paypal_ids.json` and redo Step 3 |
| `python3 --version` shows 3.9 | Use `python3.12` explicitly to create `.venv` |
| `ModuleNotFoundError` | Run `source .venv/bin/activate`, then `pip install -r requirements.txt` |
| `Set PAYPAL_CLIENT_ID ...` error | `.env` is missing or empty; it must be in the project folder |
| `Address already in use` | Use `--port 8001` and open http://localhost:8001 (and `ngrok http 8001`) |
| No webhook events | Check ngrok is running, its address matches `check_sandbox.py`, and the Mac didn't sleep |

## Files
| File | Purpose |
|---|---|
| `check_sandbox.py` | Verifies credentials, plans, webhooks |
| `setup_catalog.py` | Creates product and plans (run once per merchant) |
| `setup_webhook.py` | Registers or updates the webhook address |
| `app.py` | Demo app + webhook receiver (verifies signature, publishes to Kafka) |
| `paypal_client.py` | PayPal REST client, including signature verification |
| `recovery/` | Pipeline (events, classifier, state machine, db, Kafka, processor) and agent (llm, guardrails, tools, agent, scheduler, mailer, policy) and web (reply links, dashboard API) |
| `replay_events.py` | Puts logged events on Kafka, labelled `simulator` |
| `show_state.py` | Prints subscribers, cases, approvals, agent schedule |
| `check_events.py` | Compares PayPal's records with Postgres to find missed webhooks |
| `check_llm.py` | Checks the Gemini key and models in `.env` |
| `run_agent.py` | Runs the agent on one case now and prints each tool call and decision |
| `simulate_failure.py` | Makes a new, labelled simulated failure (choose the reason) |
| `try_paypal_actions.py` | Tries PayPal's suspend and activate on a real sandbox subscription |
| `static/dashboard.html` | The merchant dashboard (http://localhost:8000/dashboard) |
| `tests/` | Unit and integration tests; fixtures are your real Week 1 payloads |
| `infra/` | Docker Compose, Postgres schema + migrations, Kafka topics |
| `docs/` | HLD, LLD, Week 1 findings |

## Safety
- `.env`, `paypal_ids.json` and `.reply_secret` are git-ignored. Never commit secrets.
- Use only sandbox credentials. `check_sandbox.py` warns if the URL isn't the sandbox.
- No employer data, code, or designs anywhere in this repo.
