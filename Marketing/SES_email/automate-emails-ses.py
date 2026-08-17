import csv
import os
import time
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Load credentials/config from .env (never commit .env to git)
load_dotenv()

# ─────────────────────────────────────────────────────────
# Amazon SES configuration (from environment / .env)
# ─────────────────────────────────────────────────────────
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "shlomi.cohen@getquickscribe.com")

# boto3 automatically picks up AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
# from the environment once load_dotenv() has set them.
ses_client = boto3.client("ses", region_name=AWS_REGION)

# Throttle between sends (seconds). SES sandbox default is low; once you're
# out of the sandbox with a good sending rate, you can lower this a lot.
SEND_DELAY_SECONDS = 1

# ─────────────────────────────────────────────────────────
# Email content configuration
# ─────────────────────────────────────────────────────────
SUBJECT = "\u200F\u202A{name}\u202C, תודה שנרשמת ל־\u202AQuickScribe\u202C"

# HTML body lives in its own file so it can be edited without touching this script.
# Must contain a {name} placeholder, e.g. "היי {name},"
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_template.html")


def load_template(path: str) -> str:
    with open(path, mode="r", encoding="utf-8") as f:
        return f.read()


def send_email(to_email: str, name: str, html_template: str) -> bool:
    """Send a single email via SES. Returns True on success, False on failure."""
    try:
        ses_client.send_email(
            Source=SENDER_EMAIL,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": SUBJECT.format(name=name), "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_template.format(name=name), "Charset": "UTF-8"}
                },
            },
        )
        return True
    except ClientError as e:
        print(f"  FAILED to send to {to_email}: {e.response['Error']['Message']}")
        return False


def open_csv_with_fallback(filename: str):
    """Try common encodings in order, since Excel-exported Hebrew CSVs are
    often Windows-1255 rather than UTF-8."""
    for encoding in ("utf-8-sig", "windows-1255", "cp1255"):
        try:
            f = open(filename, mode="r", encoding=encoding)
            f.readline()  # force a decode attempt now, not lazily later
            f.seek(0)
            print(f"  (reading CSV as {encoding})")
            return f
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(
        "unknown", b"", 0, 1,
        f"Could not decode {filename} with utf-8-sig, windows-1255, or cp1255"
    )


def main():
    html_template = load_template(TEMPLATE_PATH)

    filename = input("input file name (example: simulation_users.csv): ").strip('"\' ')

    sent_count = 0
    failed_count = 0

    with open_csv_with_fallback(filename) as file:
        reader = csv.DictReader(file)

        for row in reader:
            user_name = row["user_name"]
            user_email = row["user_email"]

            if send_email(user_email, user_name, html_template):
                sent_count += 1
                print(f"Successfully sent to {user_name} ({user_email})")
            else:
                failed_count += 1

            time.sleep(SEND_DELAY_SECONDS)

    print(f"\nDone. Sent: {sent_count}, Failed: {failed_count}")


if __name__ == "__main__":
    main()
