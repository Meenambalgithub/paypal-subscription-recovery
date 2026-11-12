"""Try the pause (suspend) and resume (activate) calls on a real sandbox subscription.

Run:  python try_paypal_actions.py I-30W3598ERHD1      (use a spare test subscription)

The agent uses these same calls when a real customer asks to pause. A real failure
can't be made in the sandbox, so this is how to prove the calls work against PayPal.
Capture can't be tried here: it needs an outstanding balance, which only a real
failed payment creates.
"""
import sys
import time

from paypal_client import PayPalClient, PayPalError


def status(client, sid):
    return client.get_subscription(sid).get("status")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python try_paypal_actions.py SUBSCRIPTION_ID")
    sid, client = sys.argv[1], PayPalClient()
    print(f"{sid} is {status(client, sid)}")
    try:
        client.suspend_subscription(sid, "Testing pause from try_paypal_actions.py")
        time.sleep(2)
        print(f"after suspend:  {status(client, sid)}")
        client.activate_subscription(sid, "Testing resume from try_paypal_actions.py")
        time.sleep(2)
        print(f"after activate: {status(client, sid)}")
    except PayPalError as e:
        sys.exit(f"PayPal refused: {e}")
    print("\nBoth calls work. Expect BILLING.SUBSCRIPTION.SUSPENDED and .ACTIVATED webhooks in window 1;"
          "\nthe processor stores them and opens no case (nothing failed).")


if __name__ == "__main__":
    main()
