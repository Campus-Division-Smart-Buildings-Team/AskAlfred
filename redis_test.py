from dotenv import load_dotenv

from clients import get_redis


def main() -> int:
    """Run a manual Redis smoke test."""
    load_dotenv()
    client = get_redis()
    try:
        client.set("foo", "bar")
        print(client.get("foo"))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
