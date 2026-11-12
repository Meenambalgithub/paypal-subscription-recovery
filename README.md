# Subscription recovery agent for PayPal

**PayPal's retries recover what time can fix. This agent recovers what only the customer can fix.**

Demo video (under 3 minutes): **ADD-YOUR-YOUTUBE-LINK**

## The problem

Subscription businesses on PayPal lose customers who never meant to leave. When a renewal
fails, PayPal retries on a schedule. That works when time is the fix, such as a temporary error.
It can't work when the customer has to act, for example by updating their payment method,
and the customer often doesn't know the payment failed. Generic "your payment failed" emails
don't say what to do, can't act on the answer, and, done carelessly, look like phishing or
contact customers they shouldn't.

**Who it's for:** small and mid-sized subscription merchants on PayPal (courses, software,
creators) who don't have a payments team.

## What it does

When a renewal fails, PayPal's webhook reaches the agent within seconds, and code decides
the treatment from PayPal's reason code before any AI is involved:

| Failure | Customer experience | Merchant sees |
| --- | --- | --- |
| Customer must act (`PAYMENT_DENIED`, `PAYER_CANNOT_PAY`, ...) | A calm email within a minute: the one fix, the PayPal link, and two one-click replies | "Customer emailed", then "Recovered" or "Paused" |
| PayPal-side error, or no reason | One short "no action needed, PayPal will retry" note | "Waiting for PayPal's retry" |
| Restricted payer, or a problem with the merchant's own account | Never contacted | A case in "Needs your review" |

- **"Already updated it?"** → the agent checks PayPal and charges the outstanding balance now.
- **"Need a break instead?"** → the subscription is paused (up to 30 days), confirmed by email,
  and restarts automatically.
- **Merchant dashboard** (`/dashboard`): money recovered (split into "after the agent emailed"
  vs "by PayPal's retry alone"), every case, a review queue, and a step-by-step trail of each
  decision, including what the rules blocked and why.

## How it works

```
PayPal sandbox ──► webhook receiver ──► Kafka ──► processor ──► PostgreSQL ──► dashboard
 (subscriptions,     (verifies PayPal's      (in order,   (dedupes, classifies,
  renewals)           signature)              no loss)     opens cases; risk → review)
                                                               │ customer must act
                                                               ▼
                    customer email ◄── guardrails ◄── agent + Gemini ◄── scheduler
                    (Mailpit, signed     (rules in code      (chooses words    (every 30 s,
                     reply links)         check every action) and next step)    and on replies)
                          │                    │
                          │ one-click reply    └──► PayPal actions: capture, suspend, activate
                          └──────────► wakes the agent again
```

**The model chooses; code decides.** Every action Gemini asks for is checked in
`recovery/guardrails.py` first. A blocked action's reason goes back to the model, and is logged.

| Rule enforced in code | Prevents |
| --- | --- |
| Risk and merchant-side cases never reach the LLM or the customer | Contacting a restricted account |
| One email per customer per 24 h (claimed atomically in Redis); quiet hours 21:00 to 08:00 | Spam, night-time emails |
| The right message for the case; at most 2 follow-ups | Asking customers to act when no action is needed |
| No links written by the LLM; calm subjects; never mention passwords or card numbers | Phishing-like emails |
| Charge only after "I've fixed it", never more than is owed; pause only on request, up to 30 days | Acting without the customer's consent |
| "Recovered" is set only by a PayPal payment event, never by the model | The AI declaring success |
| Signed, expiring reply links that act only on a button press | Guessed or edited links |
| If the LLM is down, nothing is sent and the case retries in 15 minutes | Half-written messages |

The LLM sees the customer's first name only. Email goes only to a local catcher (Mailpit).
The dashboard refuses requests arriving through the public tunnel.

**PayPal APIs used (sandbox):** Catalog Products and Billing Plans; Subscriptions (create, get,
suspend, activate, capture); Webhooks (register, verify signature, list events, resend).

**Stack:** Python 3.12, FastAPI, Kafka, Redis, PostgreSQL, Mailpit, Gemini Flash (free tier)
through a swappable adapter, Docker Compose. 155 automated tests.

## What's real and what's simulated

Everything runs on PayPal's **sandbox**: no real money, no real people.

- **Real:** the product, plans, subscriptions and daily renewals; webhooks with signature
  verification; recovering missed webhooks from PayPal's event list; suspend and activate.
- **Simulated (and labelled `simulator` everywhere):** failed payments, because the sandbox
  can't make a renewal fail. `simulate_failure.py` creates failure events in PayPal's real
  payload shape. Charging a simulated customer sends a labelled payment event through the normal pipeline.
- **Not tried end to end:** capturing an outstanding balance from a real subscription, which
  needs a real failed payment. The call follows PayPal's API and is tested with a fake client.

What real payloads taught us, and four bugs they exposed, are in
[docs/FINDINGS-week1.md](docs/FINDINGS-week1.md) and [docs/LLD.md](docs/LLD.md) (sections 11 to 13).

## Run it yourself

Developed and tested on macOS (Apple Silicon) with Python 3.12 and Docker Desktop.
The full guide with a check after every step is [docs/BUILD_LOG.md](docs/BUILD_LOG.md).

**You need:** a free PayPal developer account, Python 3.12, Docker Desktop, ngrok (free), and a
free Gemini API key from https://aistudio.google.com.

**1. PayPal sandbox.** At https://developer.paypal.com (Sandbox), create a **US Business** account
and a **US Personal** account, then an app owned by the Business account. Copy its Client ID and Secret.
(Sandbox accounts from some other countries fail at subscription approval.)

**2. Install and configure.**
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, GEMINI_API_KEY
```

**3. Start the infrastructure** (Kafka, Redis, PostgreSQL, Mailpit):
```bash
docker compose -f infra/docker-compose.yml up -d
./infra/create_topics.sh
python check_sandbox.py     # Credentials OK
python check_llm.py         # LLM ready.
python setup_catalog.py     # creates the product and plans
```

**4. Run it** (one Terminal window each, each with `source .venv/bin/activate`):
```bash
uvicorn app:app --port 8000                              # 1: app and webhook receiver
ngrok http 8000                                          # 2: public tunnel for PayPal
python setup_webhook.py https://YOUR-ADDRESS.ngrok-free.app   # once; add the printed PAYPAL_WEBHOOK_ID to .env, restart window 1
python -m recovery.processor                             # 3: event processor
python -m recovery.scheduler                             # 4: wakes the agent
```

**5. Try it.**
1. Open http://localhost:8000, subscribe with the US Personal sandbox account (use an incognito window).
2. Run `python simulate_failure.py --name Priya`.
3. Within a minute, Priya's email is at http://localhost:8025 (Mailpit).
4. In the email, click "Already updated it?" and press the button.
5. On http://localhost:8000/dashboard, Priya turns **Recovered**.

Also try `--reason INTERNAL_SERVER_ERROR` (heads-up only) and `--reason PAYER_ACCOUNT_RESTRICTED`
(no email; goes to "Needs your review").

**Tests** (Docker running; they use a separate test database):
```bash
python -m pytest            # 155 passed
```

## Repository layout

| Path | Contents |
| --- | --- |
| `app.py` | Subscribe page, webhook receiver, reply pages and dashboard routes |
| `recovery/` | Pipeline (events, classifier, state machine, processor) and agent (llm, guardrails, tools, agent, scheduler, replies, web) |
| `static/` | Subscribe page and merchant dashboard |
| `infra/` | Docker Compose, PostgreSQL schema and migrations, Kafka topics |
| `tests/` | 155 tests; fixtures are real sandbox payloads |
| `docs/` | Design (HLD, LLD), week 1 findings, full build log |
| `*.py` at the top | Setup and helper scripts: `check_sandbox`, `setup_catalog`, `setup_webhook`, `check_llm`, `simulate_failure`, `run_agent`, `show_state`, `check_events`, `try_paypal_actions`, `replay_events` |

## Safety

- Sandbox credentials only; `check_sandbox.py` warns if the URL isn't the sandbox.
- `.env`, `paypal_ids.json`, `.reply_secret` and `webhook_events.log` are git-ignored. Never commit secrets.
- No employer data, code or designs anywhere in this repository.

## License

[MIT](LICENSE)
