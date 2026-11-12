"""Settings from .env, in one place. See docs/LLD.md section 9."""
import os

from dotenv import load_dotenv

load_dotenv()

PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://recovery:recovery@localhost:5432/recovery")
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))

TOPIC_EVENTS = "subscription-events"
TOPIC_REPLIES = "customer-replies"
TOPIC_DLQ = "subscription-events-dlq"
PROCESSOR_GROUP = "processor"

MAX_ATTEMPTS = 3          # processor tries an event this many times, then sends it to the DLQ

# ---------------------------------------------------------------- Week 3: agent
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()     # gemini | fake
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3.6-flash").strip()
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "gemini-2.5-flash").strip()
AGENT_MAX_STEPS = 6                 # tool-calling rounds per agent run
LLM_RETRY_MINUTES = 15              # if the LLM is unavailable, try the case again this much later

MERCHANT_NAME = os.getenv("MERCHANT_NAME", "Pipeline School")
MAIL_FROM = os.getenv("MAIL_FROM", "Pipeline School <billing@pipeline-school.test>")
# Where customers fix their payment method. Code adds this link; the LLM never writes links.
FIX_URL = os.getenv("FIX_URL", "https://www.sandbox.paypal.com/myaccount/autopay/")
# Quiet hours are in the customer's time zone. Default to yours so testing matches your clock.
CUSTOMER_TZ = os.getenv("CUSTOMER_TZ", "Asia/Kolkata")
QUIET_HOURS = os.getenv("QUIET_HOURS", "on").strip().lower()             # on | off
SCHEDULER_INTERVAL_S = 30

# ---------------------------------------------------------------- Week 4: replies and dashboard
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
REPLY_SECRET = os.getenv("REPLY_SECRET", "").strip()      # signs reply links; generated if empty
REPLY_LINK_DAYS = 14
