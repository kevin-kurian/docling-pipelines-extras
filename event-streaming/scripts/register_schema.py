import argparse
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def register_schema(*, topic: str, schema_file: str) -> None:
    base_url = os.environ["SCHEMA_REGISTRY_URL"].rstrip("/")
    subject = quote(f"{topic}-value", safe="")
    schema = Path(schema_file).read_text(encoding="utf-8")
    request = Request(
        f"{base_url}/subjects/{subject}/versions",
        data=json.dumps({"schema": schema, "schemaType": "JSON"}).encode("utf-8"),
        headers={"Content-Type": "application/vnd.schemaregistry.v1+json"},
        method="POST",
    )

    with urlopen(request, timeout=5) as response:
        result = json.load(response)
    logger.info("Registered %s for %s (id=%s)", schema_file, subject, result["id"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Register JSON schemas with Schema Registry.")
    parser.add_argument(
        "--schema", action="append", nargs=2, required=True, metavar=("TOPIC", "SCHEMA_FILE"),
        help="Topic and JSON schema file to register; repeat for each schema.",
    )
    args = parser.parse_args()
    for topic, schema_file in args.schema:
        register_schema(topic=topic, schema_file=schema_file)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
